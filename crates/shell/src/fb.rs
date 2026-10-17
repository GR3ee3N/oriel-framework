//! Shows the page on a spare OSD plane through fbdev while Kodi stays DRM master.
//!
//! On meson, /dev/fb1 is osd1 above Kodi's osd0, and panning it commits the
//! plane without DRM master. Frames are copied into one of three buffers and
//! panned on a thread of their own, since pans wait for vblank: a frame is
//! copied while the previous one waits.
//!
//! osd1 can come up at 3840x2160, where WebKit's buffers don't fit in memory,
//! so it is set to 1920x1080 while we run.

use crate::gl::{Blitter, DMA_BUF_IOCTL_SYNC, DMA_BUF_SYNC_END, DMA_BUF_SYNC_READ, Transfer};
use glib::ffi::{GTRUE, gboolean, gpointer};
use glib::gobject_ffi;
use glib::translate::FromGlibPtrFull;
use std::cell::RefCell;
use std::collections::VecDeque;
use std::ffi::{c_int, c_uint, c_void};
use std::os::fd::{AsRawFd, OwnedFd};
use std::sync::mpsc;
use wpe_sys as wpe;

const FBIOGET_VSCREENINFO: libc::c_ulong = 0x4600;
const FBIOPUT_VSCREENINFO: libc::c_ulong = 0x4601;
const FBIOGET_FSCREENINFO: libc::c_ulong = 0x4602;
const FBIOPAN_DISPLAY: libc::c_ulong = 0x4606;
const FBIOBLANK: libc::c_ulong = 0x4611;
const FB_BLANK_UNBLANK: libc::c_ulong = 0;
const FB_BLANK_POWERDOWN: libc::c_ulong = 4;
const DRM_FORMAT_MOD_LINEAR: u64 = 0;
const DRM_FORMAT_MOD_INVALID: u64 = 0x00ff_ffff_ffff_ffff;

#[derive(Clone, Copy, PartialEq)]
pub struct Rect {
    pub x: usize,
    pub y: usize,
    pub w: usize,
    pub h: usize,
}

impl Rect {
    fn contains(&self, o: &Rect) -> bool {
        o.x >= self.x && o.y >= self.y && o.x + o.w <= self.x + self.w && o.y + o.h <= self.y + self.h
    }
}

/// `rects` without those inside another one (the first of equal ones stays).
fn without_covered(rects: Vec<Rect>) -> Vec<Rect> {
    rects
        .iter()
        .enumerate()
        .filter(|&(i, r)| !rects.iter().enumerate().any(|(j, o)| j != i && o.contains(r) && (o != r || j < i)))
        .map(|(_, r)| *r)
        .collect()
}

struct Fb {
    file: OwnedFd,
    map: *mut u8,
    line: usize,
    height: usize,
    view_width: usize,
    view_height: usize,
    scale: usize,
    row: Vec<u64>,
    /// Geometry to restore on exit.
    original: Option<[u8; 160]>,
    buffers: usize,
    front: usize,
    /// Damage of the frames since the back buffer was last copied to.
    recent_damage: VecDeque<Vec<Rect>>,
    /// Frames to copy whole.
    full: usize,
    blanked: bool,
    hidden: bool,
    view: *mut wpe::WPEView,
    blitter: Option<Blitter>,
    pans: Option<mpsc::Sender<Pan>>,
    pending: *mut wpe::WPEBuffer,
    /// Copied and already reported rendered, waiting for their pans.
    /// Reporting after the pan missed every other vsync at 60 Hz.
    panning: VecDeque<*mut wpe::WPEBuffer>,
    /// The pending frame was already reported rendered, after its GPU blit.
    reported: bool,
    committed: *mut wpe::WPEBuffer,
    /// Pans queued. With two buffers a copy had to wait for the last pan, so
    /// only frames arriving soon after vblank made the next one.
    in_flight: usize,
    deferred: bool,
    /// The frame on screen was converted for the previous HDR mode.
    stale: bool,
    /// The output mode (`/sys/class/display/mode`) last seen.
    mode: String,
    transfer: Transfer,
    trace: bool,
    stats: Stats,
}

struct Pan {
    yoffset: u32,
    unblank: bool,
    copied: f64,
}

#[derive(Default)]
struct Stats {
    since: f64,
    frames: u32,
    pixels: u64,
    copy: f64,
    pan: f64,
}

thread_local! {
    static FB: RefCell<Option<Fb>> = const { RefCell::new(None) };
}

const PAGE: (usize, usize) = (1920, 1080);

/// Returns the size to render the view at.
pub fn open(path: &str) -> std::io::Result<(i32, i32)> {
    let file: OwnedFd = std::fs::OpenOptions::new().read(true).write(true).open(path)?.into();
    let fd = file.as_raw_fd();
    let mut var = [0u8; 160];
    let mut fix = [0u8; 80];
    ioctl(fd, FBIOGET_VSCREENINFO, var.as_mut_ptr().cast())?;
    let field = |b: &[u8], at: usize| u32::from_ne_bytes(b[at..at + 4].try_into().unwrap()) as usize;
    let mut original = None;
    let (width, height) = match (field(&var, 0), field(&var, 4)) {
        (w, h) if w > PAGE.0 || h > PAGE.1 => PAGE,
        size => size,
    };
    if (width, height, height * 3) != (field(&var, 0), field(&var, 4), field(&var, 12)) {
        // xres, yres, xres_virtual, yres_virtual (three buffers), xoffset, yoffset.
        let mut page = var;
        for (at, value) in [(0, width), (4, height), (8, width), (12, height * 3), (16, 0), (20, 0)] {
            page[at..at + 4].copy_from_slice(&(value as u32).to_ne_bytes());
        }
        let was = format!("{}x{} ({} lines)", field(&var, 0), field(&var, 4), field(&var, 12));
        match ioctl(fd, FBIOPUT_VSCREENINFO, page.as_mut_ptr().cast()) {
            Ok(()) => {
                eprintln!("oriel-shell: {path} was {was}, set to {width}x{height} with three buffers while we run");
                original = Some(var);
                ioctl(fd, FBIOGET_VSCREENINFO, var.as_mut_ptr().cast())?;
            }
            Err(e) => eprintln!("oriel-shell: {path} is {was} and stays so: {e}"),
        }
    }
    ioctl(fd, FBIOGET_FSCREENINFO, fix.as_mut_ptr().cast())?;
    let (width, height, vheight, bpp) = (field(&var, 0), field(&var, 4), field(&var, 12), field(&var, 24));
    let line = field(&fix, 48);
    if bpp != 32 {
        return Err(std::io::Error::other(format!("{path}: {bpp} bpp, need 32")));
    }
    let len = line * vheight;
    let map = unsafe { libc::mmap(std::ptr::null_mut(), len, libc::PROT_READ | libc::PROT_WRITE, libc::MAP_SHARED, fd, 0) };
    if map == libc::MAP_FAILED {
        return Err(std::io::Error::last_os_error());
    }
    let buffers = (vheight / height).clamp(1, 3);
    eprintln!("oriel-shell: {path} {width}x{height}, {buffers} buffer(s), stride {line}");
    let scale = match (width % PAGE.0, height % PAGE.1) {
        (0, 0) if width / PAGE.0 == height / PAGE.1 && width > PAGE.0 => width / PAGE.0,
        _ => 1,
    };
    let (view_width, view_height) = (width / scale, height / scale);
    if scale > 1 {
        eprintln!("oriel-shell: rendering {view_width}x{view_height}, scaled up {scale}x");
    }
    unsafe { std::ptr::write_bytes(map.cast::<u8>(), 0, line * height * buffers) };
    let _ = ioctl(fd, FBIOBLANK, FB_BLANK_POWERDOWN as *mut c_void);

    let (pans, rx) = mpsc::channel::<Pan>();
    let pan_fd = file.try_clone()?;
    std::thread::Builder::new().name("fb-pan".into()).spawn(move || pan_thread(pan_fd, var, rx))?;

    FB.set(Some(Fb {
        file,
        map: map.cast(),
        line,
        height,
        view_width,
        view_height,
        scale,
        row: Vec::new(),
        original,
        buffers,
        front: 0,
        recent_damage: VecDeque::new(),
        full: buffers,
        blanked: true,
        hidden: false,
        view: std::ptr::null_mut(),
        blitter: None,
        pans: Some(pans),
        pending: std::ptr::null_mut(),
        panning: VecDeque::new(),
        reported: false,
        committed: std::ptr::null_mut(),
        in_flight: 0,
        deferred: false,
        stale: false,
        mode: output_mode(),
        transfer: Transfer::Sdr,
        trace: std::env::var_os("ORIEL_FB_TRACE").is_some(),
        stats: Stats { since: monotonic(), ..Default::default() },
    }));
    Ok((view_width as i32, view_height as i32))
}

pub fn install(view: *mut wpe::WPEView) {
    unsafe {
        let class = gobject_ffi::g_type_class_ref(wpe::wpe_view_headless_get_type()).cast::<wpe::WPEViewClass>();
        (*class).render_buffer = Some(render_buffer);
        FB.with_borrow_mut(|fb| {
            let fb = fb.as_mut().expect("fb::open first");
            fb.view = view;
            let display = wpe::wpe_view_get_display(view);
            match Blitter::new(display, fb.view_width, fb.view_height) {
                Ok(b) => fb.blitter = Some(b),
                Err(e) => eprintln!("oriel-shell: no GPU staging, reading WebKit's buffers directly: {e}"),
            }
        });
        poll_transfer();
        glib::timeout_add_local(std::time::Duration::from_millis(250), || {
            poll_transfer();
            poll_mode();
            glib::ControlFlow::Continue
        });
        add_signal(libc::SIGTERM, on_term);
        add_signal(libc::SIGINT, on_term);
    }
}

fn pan_thread(file: OwnedFd, mut var: [u8; 160], rx: mpsc::Receiver<Pan>) {
    let fd = file.as_raw_fd();
    for pan in rx {
        var[16..20].copy_from_slice(&0u32.to_ne_bytes());
        var[20..24].copy_from_slice(&pan.yoffset.to_ne_bytes());
        let result = ioctl(fd, FBIOPAN_DISPLAY, var.as_mut_ptr().cast());
        let (unblank, copied, done) = (pan.unblank, pan.copied, monotonic());
        // Unblanking is left to the main thread, which may have hidden the plane since.
        glib::MainContext::default().invoke(move || panned(result, unblank, copied, done));
    }
}

unsafe extern "C" fn render_buffer(
    _view: *mut wpe::WPEView,
    buffer: *mut wpe::WPEBuffer,
    rects: *const wpe::WPERectangle,
    n_rects: c_uint,
    _error: *mut *mut glib::ffi::GError,
) -> gboolean {
    let damage = if rects.is_null() { &[][..] } else { unsafe { std::slice::from_raw_parts(rects, n_rects as usize) } };
    FB.with_borrow_mut(|fb| {
        let Some(fb) = fb.as_mut() else { return };
        unsafe { gobject_ffi::g_object_ref(buffer.cast()) };
        fb.pending = buffer;
        if fb.hidden {
            fb.complete();
            return;
        }
        if fb.in_flight >= fb.buffers.saturating_sub(1).max(1) {
            fb.deferred = true;
            return;
        }
        match unsafe { fb.copy(buffer, damage) } {
            Ok(()) => fb.present(),
            Err(e) => {
                eprintln!("oriel-shell: fb frame dropped: {e}");
                fb.complete();
            }
        }
    });
    GTRUE
}

fn panned(result: std::io::Result<()>, unblank: bool, copied: f64, done: f64) {
    FB.with_borrow_mut(|fb| {
        let Some(fb) = fb.as_mut() else { return };
        if let Err(e) = result {
            eprintln!("oriel-shell: pan: {e}");
        }
        fb.stats.pan += done - copied;
        if fb.trace {
            println!("oriel-shell: panned t={done:.6} pan={:.2}ms", (done - copied) * 1e3);
        }
        fb.in_flight -= 1;
        fb.land();
        if unblank && fb.blanked && !fb.hidden {
            match ioctl(fb.file.as_raw_fd(), FBIOBLANK, FB_BLANK_UNBLANK as *mut c_void) {
                Ok(()) => {
                    fb.blanked = false;
                    println!("oriel-shell: plane on t={done:.6}");
                }
                Err(e) => eprintln!("oriel-shell: unblank: {e}"),
            }
        }
        if std::mem::take(&mut fb.deferred) {
            // Its damage may be relative to a frame we never showed.
            match unsafe { fb.copy(fb.pending, &[]) } {
                Ok(()) => fb.present(),
                Err(e) => {
                    eprintln!("oriel-shell: fb frame dropped: {e}");
                    fb.complete();
                }
            }
            return;
        }
        if std::mem::take(&mut fb.stale) {
            fb.refresh();
        }
    });
}

thread_local! {
    /// Graphics white in PQ output, in nits.
    static HDR_WHITE: std::cell::Cell<f32> = std::cell::Cell::new(
        std::env::var("ORIEL_HDR_WHITE").ok().and_then(|v| v.parse().ok()).unwrap_or(203.0),
    );
}

pub fn set_hdr_white(nits: f32) {
    HDR_WHITE.set(nits);
    poll_transfer();
}

fn output_transfer() -> (Transfer, String) {
    let white = || HDR_WHITE.get().clamp(80.0, 1000.0);
    let parse = |s: &str| {
        let mut words = s.split_whitespace();
        match words.next() {
            Some("sdr") => Some(Transfer::Sdr),
            Some("hlg") => Some(Transfer::Hlg),
            Some("pq") => Some(Transfer::Pq { white: words.next().and_then(|w| w.parse().ok()).unwrap_or_else(white) }),
            _ => None,
        }
    };
    // For calibration: "sdr", "hlg" or "pq [nits]".
    if let Some(path) = std::env::var_os("ORIEL_TRANSFER_OVERRIDE")
        && let Some(t) = std::fs::read_to_string(&path).ok().as_deref().and_then(parse)
    {
        return (t, format!("override {}", std::path::Path::new(&path).display()));
    }
    let status = std::fs::read_to_string("/sys/class/amhdmitx/amhdmitx0/hdmi_hdr_status").unwrap_or_default();
    let status = status.trim().to_string();
    let transfer = if status.is_empty() || status.starts_with("SDR") {
        Transfer::Sdr
    } else if status.contains("HLG") {
        Transfer::Hlg
    } else {
        // HDR10, HDR10+ and Dolby Vision all treat osd1 as PQ.
        Transfer::Pq { white: white() }
    };
    (transfer, status)
}

fn poll_transfer() {
    let (transfer, why) = output_transfer();
    FB.with_borrow_mut(|fb| {
        let Some(fb) = fb.as_mut() else { return };
        if fb.transfer == transfer {
            return;
        }
        fb.transfer = transfer;
        let converts = match fb.blitter.as_mut() {
            Some(b) => b.set_transfer(transfer),
            None => transfer == Transfer::Sdr,
        };
        println!("oriel-shell: transfer {transfer:?} ({why}) t={:.6}", monotonic());
        if !converts {
            eprintln!("oriel-shell: can't convert for {transfer:?}; the page goes out as SDR");
        }
        fb.refresh();
    });
}

fn output_mode() -> String {
    std::fs::read_to_string("/sys/class/display/mode").unwrap_or_default()
}

/// A pan sizes osd1 for the output mode of the time, so a page shown just
/// before playback switches 2160p back to 1080p stays at twice the screen's
/// size until its next frame: show the frame again for the new mode.
fn poll_mode() {
    let mode = output_mode();
    FB.with_borrow_mut(|fb| {
        let Some(fb) = fb.as_mut() else { return };
        if fb.mode == mode {
            return;
        }
        println!("oriel-shell: output {} t={:.6}", mode.trim(), monotonic());
        fb.mode = mode;
        fb.refresh();
    });
}

impl Fb {
    unsafe fn copy(&mut self, buffer: *mut wpe::WPEBuffer, damage: &[wpe::WPERectangle]) -> Result<(), String> {
        let t0 = monotonic();
        let (bw, bh) = unsafe { (wpe::wpe_buffer_get_width(buffer) as usize, wpe::wpe_buffer_get_height(buffer) as usize) };
        let (w, h) = (bw.min(self.view_width), bh.min(self.view_height));
        let whole = vec![Rect { x: 0, y: 0, w, h }];
        let clip = |r: &wpe::WPERectangle| {
            let (x, y) = (r.x.max(0) as usize, r.y.max(0) as usize);
            let (x2, y2) = (((r.x + r.width).max(0) as usize).min(w), ((r.y + r.height).max(0) as usize).min(h));
            (x2 > x && y2 > y).then_some(Rect { x, y, w: x2 - x, h: y2 - y })
        };
        let frame: Vec<Rect> = if damage.is_empty() { whole.clone() } else { damage.iter().filter_map(clip).collect() };
        // The back buffer is `buffers` frames old: it needs this frame's damage and the ones since.
        let copy: Vec<Rect> = if self.full > 0 {
            self.full -= 1;
            whole
        } else if self.buffers == 1 {
            frame.clone()
        } else {
            without_covered(frame.iter().chain(self.recent_damage.iter().flatten()).copied().collect())
        };
        let back = (self.front + 1) % self.buffers;
        let pixels: u64 = copy.iter().map(|r| (r.w * r.h) as u64).sum();

        let base = unsafe { self.map.add(back * self.height * self.line) };
        let (line, scale) = (self.line, self.scale);
        let mut scaled = std::mem::take(&mut self.row);
        let mut copy_rows = |data: *const u8, stride: usize| {
            for r in &copy {
                for row in r.y..r.y + r.h {
                    let src = unsafe { data.add(row * stride + r.x * 4) };
                    if scale == 1 {
                        unsafe { std::ptr::copy_nonoverlapping(src, base.add(row * line + r.x * 4), r.w * 4) };
                        continue;
                    }
                    // The framebuffer is uncached: build the row in memory, only write it there.
                    let pixels = unsafe { std::slice::from_raw_parts(src.cast::<u32>(), r.w) };
                    scaled.clear();
                    if scale == 2 {
                        scaled.extend(pixels.iter().map(|&p| u64::from(p) * 0x1_0000_0001));
                    } else {
                        let mut out = pixels.iter().flat_map(|&p| std::iter::repeat_n(u64::from(p), scale));
                        while let Some(a) = out.next() {
                            scaled.push(a | out.next().unwrap_or(0) << 32);
                        }
                    }
                    let bytes = r.w * scale * 4;
                    for dy in 0..scale {
                        unsafe {
                            let dst = base.add((row * scale + dy) * line + r.x * scale * 4);
                            std::ptr::copy_nonoverlapping(scaled.as_ptr().cast::<u8>(), dst, bytes);
                        }
                    }
                }
            }
        };
        let mut gpu = 0.0;
        let kind = if let Some(blitter) = self.blitter.as_ref() {
            let data = blitter.blit(buffer, &copy)?;
            gpu = monotonic() - t0;
            // WebKit's buffer is no longer read: it renders the next frame
            // while the CPU copies from staging.
            if buffer == self.pending {
                unsafe { wpe::wpe_view_buffer_rendered(self.view, buffer) };
                self.reported = true;
            }
            copy_rows(data, blitter.stride);
            blitter.end_read();
            "gpu+cpu"
        } else {
            let src = unsafe { Pixels::get(buffer)? };
            copy_rows(src.data, src.stride);
            src.kind
        };
        self.row = scaled;
        let t1 = monotonic();
        let unblank = self.blanked;
        self.pans
            .as_ref()
            .ok_or("no pan thread")?
            .send(Pan { yoffset: (back * self.height) as u32, unblank, copied: t1 })
            .map_err(|e| e.to_string())?;
        self.in_flight += 1;
        self.front = back;
        self.recent_damage.push_back(frame);
        if self.recent_damage.len() >= self.buffers {
            self.recent_damage.pop_front();
        }

        let s = &mut self.stats;
        s.frames += 1;
        s.pixels += pixels;
        s.copy += t1 - t0;
        if self.trace {
            println!(
                "oriel-shell: frame t={t1:.6} {kind} rects={} px={pixels} copy={:.2}ms (gpu {:.2}ms)",
                copy.len(),
                (t1 - t0) * 1e3,
                gpu * 1e3
            );
        }
        if t1 - s.since >= 5.0 {
            let n = f64::from(s.frames);
            println!(
                "oriel-shell: fps={:.1} {kind} copy={:.2}ms pan={:.2}ms px/frame={:.0}",
                n / (t1 - s.since),
                s.copy / n * 1e3,
                s.pan / n * 1e3,
                s.pixels as f64 / n
            );
            *s = Stats { since: t1, ..Default::default() };
        }
        Ok(())
    }

    fn present(&mut self) {
        let buffer = std::mem::replace(&mut self.pending, std::ptr::null_mut());
        if !std::mem::take(&mut self.reported) {
            unsafe { wpe::wpe_view_buffer_rendered(self.view, buffer) };
        }
        self.panning.push_back(buffer);
    }

    fn land(&mut self) {
        let Some(buffer) = self.panning.pop_front() else { return };
        let old = std::mem::replace(&mut self.committed, buffer);
        if !old.is_null() {
            unsafe {
                wpe::wpe_view_buffer_released(self.view, old);
                gobject_ffi::g_object_unref(old.cast());
            }
        }
    }

    /// Hidden or failed to copy: hand the pending buffer back unshown.
    fn complete(&mut self) {
        let buffer = std::mem::replace(&mut self.pending, std::ptr::null_mut());
        if buffer.is_null() {
            return;
        }
        unsafe {
            if !std::mem::take(&mut self.reported) {
                wpe::wpe_view_buffer_rendered(self.view, buffer);
            }
            let old = std::mem::replace(&mut self.committed, buffer);
            if !old.is_null() {
                wpe::wpe_view_buffer_released(self.view, old);
                gobject_ffi::g_object_unref(old.cast());
            }
        }
    }

    fn refresh(&mut self) {
        if self.hidden || self.committed.is_null() {
            return;
        }
        if self.in_flight > 0 || !self.pending.is_null() {
            self.stale = true;
            return;
        }
        self.full = self.buffers;
        let buffer = self.committed;
        if let Err(e) = unsafe { self.copy(buffer, &[]) } {
            eprintln!("oriel-shell: redraw for the new output failed: {e}");
        }
    }

    fn set_hidden(&mut self, hidden: bool) {
        if self.hidden == hidden {
            return;
        }
        self.hidden = hidden;
        let t = monotonic();
        unsafe { wpe::wpe_view_set_visible(self.view, i32::from(!hidden)) };
        if hidden {
            let _ = ioctl(self.file.as_raw_fd(), FBIOBLANK, FB_BLANK_POWERDOWN as *mut c_void);
            self.blanked = true;
            println!("oriel-shell: hidden t={t:.6} took={:.6}", monotonic() - t);
        } else {
            // WebKit's damage can be partial here although both buffers are stale.
            self.full = self.buffers;
            println!("oriel-shell: shown t={t:.6}");
        }
    }

    fn clear(&mut self) {
        let fd = self.file.as_raw_fd();
        let _ = ioctl(fd, FBIOBLANK, FB_BLANK_POWERDOWN as *mut c_void);
        unsafe { std::ptr::write_bytes(self.map, 0, self.line * self.height * self.buffers) };
        if let Some(mut var) = self.original.take()
            && let Err(e) = ioctl(fd, FBIOPUT_VSCREENINFO, var.as_mut_ptr().cast())
        {
            eprintln!("oriel-shell: putting the framebuffer's geometry back: {e}");
        }
    }
}

struct Pixels {
    kind: &'static str,
    data: *const u8,
    stride: usize,
    sync_fd: Option<c_int>,
    bytes: Option<*mut glib::ffi::GBytes>,
}

impl Pixels {
    unsafe fn get(buffer: *mut wpe::WPEBuffer) -> Result<Pixels, String> {
        unsafe {
            let instance = buffer.cast::<gobject_ffi::GTypeInstance>();
            if gobject_ffi::g_type_check_instance_is_a(instance, wpe::wpe_buffer_shm_get_type()) != 0 {
                let bytes = wpe::wpe_buffer_shm_get_data(buffer);
                let data = glib::ffi::g_bytes_get_data(bytes, std::ptr::null_mut());
                let stride = wpe::wpe_buffer_shm_get_stride(buffer) as usize;
                return Ok(Pixels { kind: "shm", data: data.cast(), stride, sync_fd: None, bytes: None });
            }
            if gobject_ffi::g_type_check_instance_is_a(instance, wpe::wpe_buffer_dma_buf_get_type()) == 0 {
                return Err("unknown buffer type".into());
            }
            let modifier = wpe::wpe_buffer_dma_buf_get_modifier(buffer);
            let stride = wpe::wpe_buffer_dma_buf_get_stride(buffer, 0) as usize;
            if modifier == DRM_FORMAT_MOD_LINEAR || modifier == DRM_FORMAT_MOD_INVALID {
                let fd = wpe::wpe_buffer_dma_buf_get_fd(buffer, 0);
                let data = dma_buf_map(buffer, fd, stride)?;
                let _ = ioctl(fd, DMA_BUF_IOCTL_SYNC, (&DMA_BUF_SYNC_READ as *const u64).cast_mut().cast());
                return Ok(Pixels { kind: "cpu", data, stride, sync_fd: Some(fd), bytes: None });
            }
            // Tiled or compressed.
            let mut error = std::ptr::null_mut();
            let bytes = wpe::wpe_buffer_import_to_pixels(buffer, &mut error);
            if bytes.is_null() {
                return Err(format!("import_to_pixels (modifier {modifier:#x}): {}", glib::Error::from_glib_full(error)));
            }
            glib::ffi::g_bytes_ref(bytes);
            let data = glib::ffi::g_bytes_get_data(bytes, std::ptr::null_mut());
            Ok(Pixels { kind: "cpu (gbm map)", data: data.cast(), stride, sync_fd: None, bytes: Some(bytes) })
        }
    }
}

impl Drop for Pixels {
    fn drop(&mut self) {
        if let Some(fd) = self.sync_fd {
            let flags = DMA_BUF_SYNC_READ | DMA_BUF_SYNC_END;
            let _ = ioctl(fd, DMA_BUF_IOCTL_SYNC, (&flags as *const u64).cast_mut().cast());
        }
        if let Some(bytes) = self.bytes {
            unsafe { glib::ffi::g_bytes_unref(bytes) };
        }
    }
}

/// Maps a dma-buf once and caches the mapping on the buffer.
unsafe fn dma_buf_map(buffer: *mut wpe::WPEBuffer, fd: c_int, stride: usize) -> Result<*const u8, String> {
    const KEY: &std::ffi::CStr = c"oriel-fb-map";
    unsafe {
        let object = buffer.cast::<gobject_ffi::GObject>();
        let offset = wpe::wpe_buffer_dma_buf_get_offset(buffer, 0) as usize;
        let existing = gobject_ffi::g_object_get_data(object, KEY.as_ptr());
        if !existing.is_null() {
            return Ok(((*existing.cast::<(usize, usize)>()).0 + offset) as *const u8);
        }
        let len = stride * wpe::wpe_buffer_get_height(buffer) as usize + offset;
        let map = libc::mmap(std::ptr::null_mut(), len, libc::PROT_READ, libc::MAP_SHARED, fd, 0);
        if map == libc::MAP_FAILED {
            return Err(format!("mmap dma-buf: {}", std::io::Error::last_os_error()));
        }
        unsafe extern "C" fn unmap(p: gpointer) {
            let (addr, len) = *unsafe { Box::from_raw(p.cast::<(usize, usize)>()) };
            unsafe { libc::munmap(addr as *mut c_void, len) };
        }
        let entry = Box::into_raw(Box::new((map as usize, len)));
        gobject_ffi::g_object_set_data_full(object, KEY.as_ptr(), entry.cast(), Some(unmap));
        Ok(map.cast::<u8>().add(offset))
    }
}

pub fn set_hidden(hidden: bool) {
    FB.with_borrow_mut(|fb| fb.as_mut().map(|fb| fb.set_hidden(hidden)));
    // Playback switches the output to HDR just before the OSD shows; its
    // first frame must not wait for the next poll to be converted.
    if !hidden {
        poll_transfer();
    }
}

pub fn quit() -> ! {
    FB.with_borrow_mut(|fb| fb.as_mut().map(Fb::clear));
    println!("oriel-shell: exit t={:.6}", monotonic());
    std::process::exit(0);
}

unsafe extern "C" fn on_term(_: *mut c_void) -> gboolean {
    quit()
}

fn ioctl(fd: c_int, request: libc::c_ulong, arg: *mut c_void) -> std::io::Result<()> {
    if unsafe { libc::ioctl(fd, request, arg) } < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

unsafe extern "C" {
    // Not wrapped by the glib crate.
    fn g_unix_signal_add(signum: c_int, handler: unsafe extern "C" fn(*mut c_void) -> gboolean, data: *mut c_void) -> c_uint;
}

fn add_signal(signum: c_int, handler: unsafe extern "C" fn(*mut c_void) -> gboolean) {
    unsafe { g_unix_signal_add(signum, handler, std::ptr::null_mut()) };
}

/// Same clock as the add-on's key timestamps.
pub fn monotonic() -> f64 {
    let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
    ts.tv_sec as f64 + ts.tv_nsec as f64 / 1e9
}
