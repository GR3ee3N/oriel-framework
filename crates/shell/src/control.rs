//! Commands from the add-on, one per line on a FIFO. Kodi owns the remote,
//! so keys come through here too.
//!
//!     key <evdev code> [sent]   sent: the add-on's CLOCK_MONOTONIC time
//!     hide | show
//!     hdr-white <nits>
//!     quit
//!
//! Opened read-write, so it doesn't hit EOF when the add-on's writer closes.

use crate::fb;
use std::cell::RefCell;
use std::ffi::{c_int, c_uint, c_void};
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::fs::OpenOptionsExt;
use wpe_sys as wpe;

struct Control {
    view: *mut wpe::WPEView,
    file: OwnedFd,
    line: Vec<u8>,
}

thread_local! {
    static CONTROL: RefCell<Option<Control>> = const { RefCell::new(None) };
}

unsafe extern "C" {
    // Not wrapped by the glib crate.
    fn g_unix_fd_add(
        fd: c_int,
        condition: c_uint,
        function: unsafe extern "C" fn(c_int, c_uint, *mut c_void) -> glib::ffi::gboolean,
        data: *mut c_void,
    ) -> c_uint;
}

pub fn install(view: *mut wpe::WPEView, path: &str) -> std::io::Result<()> {
    let c_path = std::ffi::CString::new(path)?;
    if unsafe { libc::mkfifo(c_path.as_ptr(), 0o600) } < 0 {
        let e = std::io::Error::last_os_error();
        if e.kind() != std::io::ErrorKind::AlreadyExists {
            return Err(e);
        }
    }
    let file: OwnedFd = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)?
        .into();
    let fd = file.as_raw_fd();
    CONTROL.set(Some(Control { view, file, line: Vec::new() }));
    unsafe { g_unix_fd_add(fd, 1, on_readable, std::ptr::null_mut()) };
    println!("oriel-shell: control {path}");
    Ok(())
}

unsafe extern "C" fn on_readable(_: c_int, _: c_uint, _: *mut c_void) -> glib::ffi::gboolean {
    let mut lines = Vec::new();
    let mut view = std::ptr::null_mut();
    CONTROL.with_borrow_mut(|control| {
        let Some(c) = control.as_mut() else { return };
        view = c.view;
        let mut buf = [0u8; 4096];
        loop {
            let n = unsafe { libc::read(c.file.as_raw_fd(), buf.as_mut_ptr().cast(), buf.len()) };
            if n <= 0 {
                break;
            }
            c.line.extend_from_slice(&buf[..n as usize]);
        }
        while let Some(end) = c.line.iter().position(|&b| b == b'\n') {
            let line: Vec<u8> = c.line.drain(..=end).collect();
            lines.push(String::from_utf8_lossy(&line).trim().to_owned());
        }
    });
    // Outside the borrow: commands call back into other modules.
    for line in lines {
        command(view, &line);
    }
    glib::ffi::GTRUE
}

fn command(view: *mut wpe::WPEView, line: &str) {
    let mut words = line.split_whitespace();
    match (words.next(), words.next().and_then(|w| w.parse::<u16>().ok())) {
        (Some("key"), Some(code)) => {
            let now = fb::monotonic();
            let sent = words.next().and_then(|w| w.parse::<f64>().ok());
            let late = sent.map(|s| format!(" relay={:.2}ms", (now - s) * 1e3)).unwrap_or_default();
            println!("oriel-shell: key code={code} t={now:.6}{late}");
            press(view, code);
        }
        (Some("hide"), _) => fb::set_hidden(true),
        (Some("show"), _) => fb::set_hidden(false),
        (Some("quit"), _) => fb::quit(),
        (Some("hdr-white"), Some(nits)) => fb::set_hdr_white(f32::from(nits)),
        _ => eprintln!("oriel-shell: control: unknown command {line:?}"),
    }
}

/// `code` is an evdev code, mapped by addon/xkb.
fn press(view: *mut wpe::WPEView, code: u16) {
    let time = (fb::monotonic() * 1000.0) as u32;
    let keycode = c_uint::from(code) + 8;
    for kind in [wpe::WPE_EVENT_KEYBOARD_KEY_DOWN, wpe::WPE_EVENT_KEYBOARD_KEY_UP] {
        unsafe {
            let keymap = wpe::wpe_display_get_keymap(wpe::wpe_view_get_display(view));
            let mut keyval = 0;
            wpe::wpe_keymap_translate_keyboard_state(
                keymap,
                keycode,
                0,
                0,
                &mut keyval,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            );
            let event = wpe::wpe_event_keyboard_new(kind, view, wpe::WPE_INPUT_SOURCE_KEYBOARD, time, 0, keycode, keyval);
            wpe::wpe_view_event(view, event);
            wpe::wpe_event_unref(event);
        }
    }
}
