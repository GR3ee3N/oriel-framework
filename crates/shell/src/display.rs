//! Send WebKit dma-bufs to vfs.oriel.display for drawing in Kodi's GUI frame.
//! Kodi owns plane commits. Messages use the oriel-display-protocol wire format.
//!
//! Keep WebKit one frame ahead of Kodi. After the add-on draws a frame,
//! report completion to WebKit and submit the pending frame. Return buffers
//! to WebKit only after the add-on releases them.
//!
//! The add-on reads submitted buffers immediately. main disables explicit
//! sync so WebKit waits for each rendering fence before calling render_buffer.
//!
//! Exit with status 4 if the add-on disconnects or refuses the protocol version.

use glib::ffi::{GTRUE, gboolean};
use glib::gobject_ffi;
use oriel_display_protocol::{self as protocol, Msg, kind};
use std::cell::{Cell, RefCell};
use std::collections::{HashMap, HashSet};
use std::ffi::{c_int, c_uint, c_void};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use wpe_sys as wpe;

pub const GONE: i32 = 4;

struct Display {
    _socket: OwnedFd,
    view: *mut wpe::WPEView,
    next_id: u32,
    /// Submitted buffers awaiting release; each entry owns a reference.
    held: HashMap<u32, *mut wpe::WPEBuffer>,
    /// Buffer ids imported by the add-on; cleared on reset.
    sent: HashSet<u32>,
    /// A submitted frame is awaiting draw confirmation.
    in_flight: bool,
    /// Rendered frame waiting for the submitted frame to be drawn.
    waiting: Option<u32>,
    stats: Stats,
}

#[derive(Default)]
struct Stats {
    since: f64,
    frames: u32,
    drawn: u32,
    sent_at: HashMap<u32, f64>,
    latency: f64,
    latency_max: f64,
}

thread_local! {
    static DISPLAY: RefCell<Option<Display>> = const { RefCell::new(None) };
    /// Defer buffer destruction notifications that can run while DISPLAY is borrowed.
    static SOCKET: Cell<c_int> = const { Cell::new(-1) };
}

/// Key for the protocol id stored on each buffer.
const KEY: &std::ffi::CStr = c"oriel-display-id";

unsafe extern "C" {
    // Call the GLib API directly because the crate does not expose it.
    fn g_unix_fd_add(
        fd: c_int,
        condition: c_uint,
        function: unsafe extern "C" fn(c_int, c_uint, *mut c_void) -> gboolean,
        data: *mut c_void,
    ) -> c_uint;
}

fn send(msg: &Msg, fds: &[c_int]) -> std::io::Result<()> {
    let bytes = msg.to_bytes();
    let mut iov = libc::iovec { iov_base: bytes.as_ptr().cast_mut().cast(), iov_len: bytes.len() };
    let mut control = [0u64; 8];
    let mut header: libc::msghdr = unsafe { std::mem::zeroed() };
    header.msg_iov = &mut iov;
    header.msg_iovlen = 1;
    if !fds.is_empty() {
        let len = size_of_val(fds) as u32;
        header.msg_control = control.as_mut_ptr().cast();
        header.msg_controllen = unsafe { libc::CMSG_SPACE(len) } as usize;
        unsafe {
            let c = libc::CMSG_FIRSTHDR(&header);
            (*c).cmsg_level = libc::SOL_SOCKET;
            (*c).cmsg_type = libc::SCM_RIGHTS;
            (*c).cmsg_len = libc::CMSG_LEN(len) as usize;
            std::ptr::copy_nonoverlapping(fds.as_ptr(), libc::CMSG_DATA(c).cast::<c_int>(), fds.len());
        }
    }
    if unsafe { libc::sendmsg(SOCKET.get(), &header, libc::MSG_NOSIGNAL) } < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

pub fn install(view: *mut wpe::WPEView) -> std::io::Result<()> {
    let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC, 0) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    let socket = unsafe { OwnedFd::from_raw_fd(fd) };
    let mut addr: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    addr.sun_family = libc::AF_UNIX as libc::sa_family_t;
    // A leading NUL selects the Linux abstract socket namespace.
    for (i, b) in protocol::SOCKET.iter().enumerate() {
        addr.sun_path[i + 1] = *b as libc::c_char;
    }
    let len = (std::mem::offset_of!(libc::sockaddr_un, sun_path) + 1 + protocol::SOCKET.len()) as libc::socklen_t;
    if unsafe { libc::connect(fd, (&raw const addr).cast(), len) } < 0 {
        return Err(std::io::Error::last_os_error());
    }
    SOCKET.set(socket.as_raw_fd());
    send(&Msg::new(kind::HELLO, protocol::VERSION), &[])?;
    unsafe {
        let class = gobject_ffi::g_type_class_ref(wpe::wpe_view_headless_get_type()).cast::<wpe::WPEViewClass>();
        (*class).render_buffer = Some(render_buffer);
        g_unix_fd_add(fd, 1, on_readable, std::ptr::null_mut());
    }
    DISPLAY.set(Some(Display {
        _socket: socket,
        view,
        next_id: 1,
        held: HashMap::new(),
        sent: HashSet::new(),
        in_flight: false,
        waiting: None,
        stats: Stats { since: crate::monotonic(), ..Stats::default() },
    }));
    println!("oriel-shell: drawing through vfs.oriel.display");
    Ok(())
}

unsafe extern "C" fn forget(data: *mut c_void) {
    let _ = send(&Msg::new(kind::FORGET, data as usize as u32), &[]);
}

impl Display {
    /// Assign a stable protocol id on first use of a buffer.
    unsafe fn id(&mut self, buffer: *mut wpe::WPEBuffer) -> u32 {
        let object = buffer.cast::<gobject_ffi::GObject>();
        let id = unsafe { gobject_ffi::g_object_get_data(object, KEY.as_ptr()) } as usize as u32;
        if id != 0 {
            return id;
        }
        let id = self.next_id;
        self.next_id += 1;
        unsafe { gobject_ffi::g_object_set_data_full(object, KEY.as_ptr(), id as usize as *mut c_void, Some(forget)) };
        id
    }

    /// Send plane descriptors only when the add-on has not imported this buffer.
    unsafe fn share(&mut self, id: u32, buffer: *mut wpe::WPEBuffer) -> Result<(), String> {
        if self.sent.contains(&id) {
            return Ok(());
        }
        let instance = buffer.cast::<gobject_ffi::GTypeInstance>();
        if unsafe { gobject_ffi::g_type_check_instance_is_a(instance, wpe::wpe_buffer_dma_buf_get_type()) } == 0 {
            return Err("not a dma-buf".into());
        }
        let planes = unsafe { wpe::wpe_buffer_dma_buf_get_n_planes(buffer) }.min(4);
        let mut msg = Msg {
            kind: kind::BUFFER,
            id,
            width: unsafe { wpe::wpe_buffer_get_width(buffer) },
            height: unsafe { wpe::wpe_buffer_get_height(buffer) },
            format: unsafe { wpe::wpe_buffer_dma_buf_get_format(buffer) },
            planes,
            modifier: unsafe { wpe::wpe_buffer_dma_buf_get_modifier(buffer) },
            ..Msg::default()
        };
        let mut fds = Vec::new();
        for p in 0..planes {
            msg.offsets[p as usize] = unsafe { wpe::wpe_buffer_dma_buf_get_offset(buffer, p) };
            msg.strides[p as usize] = unsafe { wpe::wpe_buffer_dma_buf_get_stride(buffer, p) };
            fds.push(unsafe { wpe::wpe_buffer_dma_buf_get_fd(buffer, p) });
        }
        send(&msg, &fds).map_err(|e| e.to_string())?;
        self.sent.insert(id);
        println!(
            "oriel-shell: buffer {id} {}x{} fourcc {:#x} modifier {:#x} planes {planes}",
            msg.width, msg.height, msg.format, msg.modifier
        );
        Ok(())
    }

    fn show(&mut self, id: u32) {
        let Some(&buffer) = self.held.get(&id) else { return };
        // Reimport if the add-on reset its buffers while this frame was pending.
        if let Err(e) = unsafe { self.share(id, buffer) } {
            eprintln!("oriel-shell: buffer {id}: {e}");
        }
        self.in_flight = true;
        self.stats.sent_at.insert(id, crate::monotonic());
        if let Err(e) = send(&Msg::new(kind::FRAME, id), &[]) {
            eprintln!("oriel-shell: frame {id}: {e}");
        }
    }
}

unsafe extern "C" fn render_buffer(
    _view: *mut wpe::WPEView,
    buffer: *mut wpe::WPEBuffer,
    _rects: *const wpe::WPERectangle,
    _n_rects: c_uint,
    _error: *mut *mut glib::ffi::GError,
) -> gboolean {
    let mut rendered = None;
    DISPLAY.with_borrow_mut(|d| {
        let Some(d) = d.as_mut() else { return };
        let id = unsafe { d.id(buffer) };
        if let Err(e) = unsafe { d.share(id, buffer) } {
            eprintln!("oriel-shell: buffer {id}: {e}");
        }
        if !d.held.contains_key(&id) {
            unsafe { gobject_ffi::g_object_ref(buffer.cast()) };
            d.held.insert(id, buffer);
        }
        d.stats.frames += 1;
        if d.in_flight {
            d.waiting = Some(id);
            return;
        }
        d.show(id);
        rendered = Some(d.view);
    });
    // Release the borrow before notifying WebKit, which can submit another frame.
    if let Some(view) = rendered {
        unsafe { wpe::wpe_view_buffer_rendered(view, buffer) };
    }
    GTRUE
}

unsafe extern "C" fn on_readable(fd: c_int, _: c_uint, _: *mut c_void) -> gboolean {
    let mut rendered = Vec::new();
    let mut released = Vec::new();
    let mut visible = None;
    let mut view = std::ptr::null_mut();
    DISPLAY.with_borrow_mut(|d| {
        let Some(d) = d.as_mut() else { return };
        view = d.view;
        loop {
            let mut bytes = [0u8; protocol::SIZE];
            let n =
                unsafe { libc::recv(fd, bytes.as_mut_ptr().cast(), bytes.len(), libc::MSG_DONTWAIT | libc::MSG_TRUNC) };
            if n == 0 {
                eprintln!("oriel-shell: vfs.oriel.display closed the connection");
                std::process::exit(GONE);
            }
            if n < 0 {
                if std::io::Error::last_os_error().kind() == std::io::ErrorKind::WouldBlock {
                    break;
                }
                eprintln!("oriel-shell: vfs.oriel.display: {}", std::io::Error::last_os_error());
                std::process::exit(GONE);
            }
            if n as usize != protocol::SIZE {
                eprintln!("oriel-shell: invalid display packet size {n}");
                std::process::exit(GONE);
            }
            let msg = Msg::from_bytes(&bytes);
            match msg.kind {
                kind::HELLO if msg.id == protocol::VERSION => {
                    println!("oriel-shell: vfs.oriel.display speaks version {}", msg.id)
                }
                kind::HELLO | kind::REFUSED => {
                    eprintln!("oriel-shell: vfs.oriel.display speaks version {}, not {}", msg.id, protocol::VERSION);
                    std::process::exit(GONE);
                }
                kind::DRAWN => {
                    d.in_flight = false;
                    if let Some(next) = d.waiting.take() {
                        d.show(next);
                        if let Some(&b) = d.held.get(&next) {
                            rendered.push(b);
                        }
                    }
                    if let Some(t) = d.stats.sent_at.remove(&msg.id) {
                        let latency = crate::monotonic() - t;
                        d.stats.latency += latency;
                        d.stats.latency_max = d.stats.latency_max.max(latency);
                    }
                    d.stats.drawn += 1;
                }
                kind::RELEASED => {
                    if let Some(b) = d.held.remove(&msg.id) {
                        released.push(b);
                    }
                }
                kind::HIDDEN | kind::SHOWN => visible = Some(msg.kind == kind::SHOWN),
                kind::RESET => d.sent.clear(),
                _ => {}
            }
        }
        let now = crate::monotonic();
        let s = &mut d.stats;
        if now - s.since >= 5.0 && s.frames > 0 {
            println!(
                "oriel-shell: fps={:.1} drawn={} sent-to-drawn mean={:.2}ms max={:.2}ms",
                f64::from(s.frames) / (now - s.since),
                s.drawn,
                s.latency / f64::from(s.drawn.max(1)) * 1e3,
                s.latency_max * 1e3
            );
            *s = Stats { since: now, ..Stats::default() };
        }
    });
    unsafe {
        if let Some(visible) = visible {
            println!("oriel-shell: page {}", if visible { "shown" } else { "hidden" });
            wpe::wpe_view_set_visible(view, i32::from(visible));
        }
        for b in rendered {
            wpe::wpe_view_buffer_rendered(view, b);
        }
        for b in released {
            wpe::wpe_view_buffer_released(view, b);
            gobject_ffi::g_object_unref(b.cast());
        }
    }
    GTRUE
}
