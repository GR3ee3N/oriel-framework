//! GPU staging for fb.rs.
//!
//! The CPU reads WebKit's Mali buffers uncached at about 90 MB/s, and Mali
//! can't import the fbdev buffer (trying locked up the kernel). So the GPU
//! copies the damage into a buffer from the cached `system` dma-buf heap,
//! which the CPU reads at about 550 MB/s.
//!
//! In HDR, CoreELEC converts osd0 but not osd1, so the copy converts our
//! pixels in a shader instead.

use crate::fb::Rect;
use glib::gobject_ffi;
use std::cell::RefCell;
use std::ffi::{c_char, c_int, c_uint, c_void};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use wpe_sys as wpe;

type EGLDisplay = *mut c_void;
type EGLContext = *mut c_void;
type EGLConfig = *mut c_void;
type EGLSurface = *mut c_void;
type EGLImage = *mut c_void;

const EGL_NONE: i32 = 0x3038;
const EGL_WIDTH: i32 = 0x3057;
const EGL_HEIGHT: i32 = 0x3056;
const EGL_SURFACE_TYPE: i32 = 0x3033;
const EGL_PBUFFER_BIT: i32 = 0x0001;
const EGL_RENDERABLE_TYPE: i32 = 0x3040;
const EGL_OPENGL_ES3_BIT: i32 = 0x40;
const EGL_OPENGL_ES_API: c_uint = 0x30A0;
const EGL_CONTEXT_MAJOR_VERSION: i32 = 0x3098;
const EGL_LINUX_DMA_BUF_EXT: c_uint = 0x3270;
const EGL_LINUX_DRM_FOURCC_EXT: i32 = 0x3271;
const EGL_DMA_BUF_PLANE0_FD_EXT: i32 = 0x3272;
const EGL_DMA_BUF_PLANE0_OFFSET_EXT: i32 = 0x3273;
const EGL_DMA_BUF_PLANE0_PITCH_EXT: i32 = 0x3274;
const DRM_FORMAT_ARGB8888: i32 = 0x3432_5241;

const GL_TEXTURE_2D: c_uint = 0x0DE1;
const GL_FRAMEBUFFER: c_uint = 0x8D40;
const GL_RENDERBUFFER: c_uint = 0x8D41;
const GL_READ_FRAMEBUFFER: c_uint = 0x8CA8;
const GL_DRAW_FRAMEBUFFER: c_uint = 0x8CA9;
const GL_COLOR_ATTACHMENT0: c_uint = 0x8CE0;
const GL_FRAMEBUFFER_COMPLETE: c_uint = 0x8CD5;
const GL_COLOR_BUFFER_BIT: c_uint = 0x4000;
const GL_NEAREST: c_uint = 0x2600;
const GL_TEXTURE_MIN_FILTER: c_uint = 0x2801;
const GL_TEXTURE_MAG_FILTER: c_uint = 0x2800;
const GL_TEXTURE0: c_uint = 0x84C0;
const GL_TRIANGLES: c_uint = 0x0004;
const GL_SCISSOR_TEST: c_uint = 0x0C11;
const GL_VERTEX_SHADER: c_uint = 0x8B31;
const GL_FRAGMENT_SHADER: c_uint = 0x8B30;
const GL_COMPILE_STATUS: c_uint = 0x8B81;
const GL_LINK_STATUS: c_uint = 0x8B82;

#[link(name = "EGL")]
unsafe extern "C" {
    fn eglBindAPI(api: c_uint) -> c_uint;
    fn eglChooseConfig(dpy: EGLDisplay, attribs: *const i32, configs: *mut EGLConfig, size: i32, n: *mut i32) -> c_uint;
    fn eglCreateContext(dpy: EGLDisplay, config: EGLConfig, share: EGLContext, attribs: *const i32) -> EGLContext;
    fn eglCreatePbufferSurface(dpy: EGLDisplay, config: EGLConfig, attribs: *const i32) -> EGLSurface;
    fn eglMakeCurrent(dpy: EGLDisplay, draw: EGLSurface, read: EGLSurface, ctx: EGLContext) -> c_uint;
    fn eglGetError() -> i32;
    fn eglGetProcAddress(name: *const c_char) -> *mut c_void;
}

#[link(name = "GLESv2")]
unsafe extern "C" {
    fn glGenFramebuffers(n: i32, ids: *mut c_uint);
    fn glGenRenderbuffers(n: i32, ids: *mut c_uint);
    fn glGenTextures(n: i32, ids: *mut c_uint);
    fn glDeleteTextures(n: i32, ids: *const c_uint);
    fn glBindFramebuffer(target: c_uint, id: c_uint);
    fn glBindRenderbuffer(target: c_uint, id: c_uint);
    fn glBindTexture(target: c_uint, id: c_uint);
    fn glFramebufferRenderbuffer(target: c_uint, attachment: c_uint, rbtarget: c_uint, rb: c_uint);
    fn glFramebufferTexture2D(target: c_uint, attachment: c_uint, textarget: c_uint, tex: c_uint, level: i32);
    fn glCheckFramebufferStatus(target: c_uint) -> c_uint;
    #[allow(clippy::too_many_arguments)]
    fn glBlitFramebuffer(sx0: i32, sy0: i32, sx1: i32, sy1: i32, dx0: i32, dy0: i32, dx1: i32, dy1: i32, mask: c_uint, filter: c_uint);
    fn glFinish();
    fn glGetError() -> c_uint;
    fn glTexParameteri(target: c_uint, name: c_uint, value: i32);
    fn glCreateShader(kind: c_uint) -> c_uint;
    fn glShaderSource(shader: c_uint, count: i32, strings: *const *const c_char, lengths: *const i32);
    fn glCompileShader(shader: c_uint);
    fn glGetShaderiv(shader: c_uint, name: c_uint, value: *mut i32);
    fn glGetShaderInfoLog(shader: c_uint, size: i32, len: *mut i32, log: *mut c_char);
    fn glCreateProgram() -> c_uint;
    fn glAttachShader(program: c_uint, shader: c_uint);
    fn glLinkProgram(program: c_uint);
    fn glGetProgramiv(program: c_uint, name: c_uint, value: *mut i32);
    fn glGetProgramInfoLog(program: c_uint, size: i32, len: *mut i32, log: *mut c_char);
    fn glUseProgram(program: c_uint);
    fn glGetUniformLocation(program: c_uint, name: *const c_char) -> i32;
    fn glUniform1i(location: i32, value: i32);
    fn glUniform1f(location: i32, value: f32);
    fn glGenVertexArrays(n: i32, ids: *mut c_uint);
    fn glBindVertexArray(id: c_uint);
    fn glActiveTexture(unit: c_uint);
    fn glViewport(x: i32, y: i32, w: i32, h: i32);
    fn glScissor(x: i32, y: i32, w: i32, h: i32);
    fn glEnable(cap: c_uint);
    fn glDisable(cap: c_uint);
    fn glDrawArrays(mode: c_uint, first: i32, count: i32);
}

type CreateImage = unsafe extern "C" fn(EGLDisplay, EGLContext, c_uint, *mut c_void, *const i32) -> EGLImage;
type ImageTarget = unsafe extern "C" fn(c_uint, EGLImage);

/// What the HDMI output does with `osd1`'s pixels.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Transfer {
    Sdr,
    /// HDR10 and Dolby Vision; graphics white at `white` nits.
    Pq { white: f32 },
    /// Graphics white at 75 % signal (BT.2408).
    Hlg,
}

/// Fullscreen triangle, scissored to each damage rectangle.
const VERTEX: &str = "#version 300 es
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
";

/// Premultiplied sRGB in, premultiplied PQ or HLG out.
const FRAGMENT: &str = "#version 300 es
precision highp float;
uniform sampler2D page;
uniform int hlg;
uniform float white;
out vec4 color;

vec3 linear(vec3 c) {
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
}

// SMPTE ST 2084, from units of 10000 nits.
vec3 pq(vec3 l) {
    const float m1 = 0.1593017578125, m2 = 78.84375;
    const float c1 = 0.8359375, c2 = 18.8515625, c3 = 18.6875;
    vec3 p = pow(max(l, 0.0), vec3(m1));
    return pow((c1 + c2 * p) / (1.0 + c3 * p), vec3(m2));
}

// ARIB STD-B67, from scene light 0..1.
vec3 hlg_oetf(vec3 e) {
    const float a = 0.17883277, b = 0.28466892, c = 0.55991073;
    return mix(sqrt(3.0 * e), a * log(max(12.0 * e - b, 1e-6)) + c, step(1.0 / 12.0, e));
}

void main() {
    vec4 c = texelFetch(page, ivec2(gl_FragCoord.xy), 0);
    if (c.a <= 0.0) {
        color = vec4(0.0);
        return;
    }
    // BT.709 to BT.2020 primaries (columns).
    const mat3 to2020 = mat3(0.6274, 0.0691, 0.0164, 0.3293, 0.9195, 0.0880, 0.0433, 0.0114, 0.8956);
    vec3 rgb = to2020 * linear(clamp(c.rgb / c.a, 0.0, 1.0));
    // 0.2650 puts graphics white at 75 % HLG signal.
    vec3 e = hlg == 1 ? hlg_oetf(rgb * 0.2650) : pq(rgb * (white / 10000.0));
    color = vec4(e * c.a, c.a);
}
";

struct Convert {
    program: c_uint,
    vao: c_uint,
    hlg: i32,
    white: i32,
}

pub struct Blitter {
    read_fbo: c_uint,
    draw_fbo: c_uint,
    texture_target: ImageTarget,
    staging: OwnedFd,
    map: *const u8,
    pub stride: usize,
    width: i32,
    height: i32,
    convert: Option<Convert>,
    transfer: Transfer,
}

/// Allocates a buffer from the cached `system` dma-buf heap.
fn alloc_cached(size: usize) -> Result<OwnedFd, String> {
    #[repr(C)]
    struct Alloc {
        len: u64,
        fd: u32,
        fd_flags: u32,
        heap_flags: u64,
    }
    const DMA_HEAP_IOCTL_ALLOC: libc::c_ulong = 0xC018_4800;
    let heap = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open("/dev/dma_heap/system")
        .map_err(|e| format!("/dev/dma_heap/system: {e}"))?;
    let mut req = Alloc { len: size as u64, fd: 0, fd_flags: (libc::O_RDWR | libc::O_CLOEXEC) as u32, heap_flags: 0 };
    if unsafe { libc::ioctl(heap.as_raw_fd(), DMA_HEAP_IOCTL_ALLOC, &mut req) } < 0 {
        return Err(format!("dma-heap alloc: {}", std::io::Error::last_os_error()));
    }
    Ok(unsafe { OwnedFd::from_raw_fd(req.fd as c_int) })
}

fn proc<T>(name: &std::ffi::CStr) -> Result<T, String> {
    let p = unsafe { eglGetProcAddress(name.as_ptr()) };
    if p.is_null() {
        return Err(format!("{name:?} missing"));
    }
    Ok(unsafe { std::mem::transmute_copy(&p) })
}

unsafe fn compile(kind: c_uint, source: &str) -> Result<c_uint, String> {
    unsafe {
        let shader = glCreateShader(kind);
        let (ptr, len) = (source.as_ptr().cast::<c_char>(), source.len() as i32);
        glShaderSource(shader, 1, &ptr, &len);
        glCompileShader(shader);
        let mut ok = 0;
        glGetShaderiv(shader, GL_COMPILE_STATUS, &mut ok);
        if ok == 0 {
            let mut log = [0 as c_char; 1024];
            glGetShaderInfoLog(shader, log.len() as i32, std::ptr::null_mut(), log.as_mut_ptr());
            return Err(format!("shader: {}", std::ffi::CStr::from_ptr(log.as_ptr()).to_string_lossy()));
        }
        Ok(shader)
    }
}

impl Convert {
    unsafe fn new() -> Result<Convert, String> {
        unsafe {
            let program = glCreateProgram();
            glAttachShader(program, compile(GL_VERTEX_SHADER, VERTEX)?);
            glAttachShader(program, compile(GL_FRAGMENT_SHADER, FRAGMENT)?);
            glLinkProgram(program);
            let mut ok = 0;
            glGetProgramiv(program, GL_LINK_STATUS, &mut ok);
            if ok == 0 {
                let mut log = [0 as c_char; 1024];
                glGetProgramInfoLog(program, log.len() as i32, std::ptr::null_mut(), log.as_mut_ptr());
                return Err(format!("link: {}", std::ffi::CStr::from_ptr(log.as_ptr()).to_string_lossy()));
            }
            glUseProgram(program);
            glUniform1i(glGetUniformLocation(program, c"page".as_ptr()), 0);
            let mut vao = 0;
            glGenVertexArrays(1, &mut vao);
            Ok(Convert {
                program,
                vao,
                hlg: glGetUniformLocation(program, c"hlg".as_ptr()),
                white: glGetUniformLocation(program, c"white".as_ptr()),
            })
        }
    }
}

impl Blitter {
    pub fn new(display: *mut wpe::WPEDisplay, width: usize, height: usize) -> Result<Blitter, String> {
        let stride = width * 4;
        let staging = alloc_cached(stride * height)?;
        unsafe {
            let mut error = std::ptr::null_mut();
            let dpy = wpe::wpe_display_get_egl_display(display, &mut error);
            if dpy.is_null() {
                return Err(format!("EGL display: {}", glib::translate::from_glib_full::<_, glib::Error>(error)));
            }
            eglBindAPI(EGL_OPENGL_ES_API);
            let attribs = [EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_NONE];
            let mut config = std::ptr::null_mut();
            let mut n = 0;
            if eglChooseConfig(dpy, attribs.as_ptr(), &mut config, 1, &mut n) == 0 || n == 0 {
                return Err(format!("eglChooseConfig: {:#x}", eglGetError()));
            }
            let ctx = eglCreateContext(dpy, config, std::ptr::null_mut(), [EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE].as_ptr());
            if ctx.is_null() {
                return Err(format!("eglCreateContext: {:#x}", eglGetError()));
            }
            if eglMakeCurrent(dpy, std::ptr::null_mut(), std::ptr::null_mut(), ctx) == 0 {
                let surface = eglCreatePbufferSurface(dpy, config, [EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE].as_ptr());
                if eglMakeCurrent(dpy, surface, surface, ctx) == 0 {
                    return Err(format!("eglMakeCurrent: {:#x}", eglGetError()));
                }
            }
            let create_image: CreateImage = proc(c"eglCreateImageKHR")?;
            let renderbuffer_target: ImageTarget = proc(c"glEGLImageTargetRenderbufferStorageOES")?;
            let texture_target: ImageTarget = proc(c"glEGLImageTargetTexture2DOES")?;

            let attrs = [
                EGL_WIDTH,
                width as i32,
                EGL_HEIGHT,
                height as i32,
                EGL_LINUX_DRM_FOURCC_EXT,
                DRM_FORMAT_ARGB8888,
                EGL_DMA_BUF_PLANE0_FD_EXT,
                staging.as_raw_fd(),
                EGL_DMA_BUF_PLANE0_OFFSET_EXT,
                0,
                EGL_DMA_BUF_PLANE0_PITCH_EXT,
                stride as i32,
                EGL_NONE,
            ];
            let image = create_image(dpy, std::ptr::null_mut(), EGL_LINUX_DMA_BUF_EXT, std::ptr::null_mut(), attrs.as_ptr());
            if image.is_null() {
                return Err(format!("EGLImage of the staging buffer: {:#x}", eglGetError()));
            }
            let (mut renderbuffer, mut draw_fbo) = (0, 0);
            glGenRenderbuffers(1, &mut renderbuffer);
            glBindRenderbuffer(GL_RENDERBUFFER, renderbuffer);
            renderbuffer_target(GL_RENDERBUFFER, image);
            glGenFramebuffers(1, &mut draw_fbo);
            glBindFramebuffer(GL_FRAMEBUFFER, draw_fbo);
            glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, renderbuffer);
            let status = glCheckFramebufferStatus(GL_FRAMEBUFFER);
            if status != GL_FRAMEBUFFER_COMPLETE {
                return Err(format!("staging framebuffer incomplete: {status:#x}"));
            }
            let map = libc::mmap(std::ptr::null_mut(), stride * height, libc::PROT_READ, libc::MAP_SHARED, staging.as_raw_fd(), 0);
            if map == libc::MAP_FAILED {
                return Err(format!("mmap staging: {}", std::io::Error::last_os_error()));
            }
            let mut read_fbo = 0;
            glGenFramebuffers(1, &mut read_fbo);
            let convert = Convert::new().map_err(|e| eprintln!("oriel-shell: no HDR conversion: {e}")).ok();
            eprintln!("oriel-shell: GPU staging in a cached dma-buf");
            Ok(Blitter {
                read_fbo,
                draw_fbo,
                texture_target,
                staging,
                map: map.cast(),
                stride,
                width: width as i32,
                height: height as i32,
                convert,
                transfer: Transfer::Sdr,
            })
        }
    }

    /// False if HDR would go out unconverted.
    pub fn set_transfer(&mut self, transfer: Transfer) -> bool {
        self.transfer = transfer;
        transfer == Transfer::Sdr || self.convert.is_some()
    }

    /// The returned pixels stay valid until [`Blitter::end_read`].
    pub fn blit(&self, buffer: *mut wpe::WPEBuffer, rects: &[Rect]) -> Result<*const u8, String> {
        unsafe {
            let mut error = std::ptr::null_mut();
            let image = wpe::wpe_buffer_import_to_egl_image(buffer, &mut error);
            if image.is_null() {
                return Err(format!("import_to_egl_image: {}", glib::translate::from_glib_full::<_, glib::Error>(error)));
            }
            // Textures live on the buffer, not keyed by EGLImage: a new buffer
            // can get a freed one's handle, and would then show the old page.
            delete_retired();
            let object = buffer.cast::<gobject_ffi::GObject>();
            let mut texture = gobject_ffi::g_object_get_data(object, TEXTURE.as_ptr()) as usize as c_uint;
            if texture == 0 {
                glGenTextures(1, &mut texture);
                glBindTexture(GL_TEXTURE_2D, texture);
                (self.texture_target)(GL_TEXTURE_2D, image);
                // The default filter needs mipmaps.
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST as i32);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST as i32);
                gobject_ffi::g_object_set_data_full(object, TEXTURE.as_ptr(), texture as usize as *mut c_void, Some(retire));
                println!("oriel-shell: new buffer, texture {texture}, EGLImage {image:p}");
            }
            glBindFramebuffer(GL_DRAW_FRAMEBUFFER, self.draw_fbo);
            match (self.transfer, &self.convert) {
                (Transfer::Sdr, _) | (_, None) => {
                    glBindFramebuffer(GL_READ_FRAMEBUFFER, self.read_fbo);
                    glFramebufferTexture2D(GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, texture, 0);
                    for r in rects {
                        let (x, y, w, h) = (r.x as i32, r.y as i32, r.w as i32, r.h as i32);
                        glBlitFramebuffer(x, y, x + w, y + h, x, y, x + w, y + h, GL_COLOR_BUFFER_BIT, GL_NEAREST);
                    }
                }
                (transfer, Some(convert)) => {
                    glUseProgram(convert.program);
                    let (hlg, white) = match transfer {
                        Transfer::Pq { white } => (0, white),
                        _ => (1, 0.0),
                    };
                    glUniform1i(convert.hlg, hlg);
                    glUniform1f(convert.white, white);
                    glBindVertexArray(convert.vao);
                    glActiveTexture(GL_TEXTURE0);
                    glBindTexture(GL_TEXTURE_2D, texture);
                    glViewport(0, 0, self.width, self.height);
                    glEnable(GL_SCISSOR_TEST);
                    for r in rects {
                        glScissor(r.x as i32, r.y as i32, r.w as i32, r.h as i32);
                        glDrawArrays(GL_TRIANGLES, 0, 3);
                    }
                    glDisable(GL_SCISSOR_TEST);
                }
            }
            glFinish();
            if let e @ 1.. = glGetError() {
                return Err(format!("GL error {e:#x}"));
            }
        }
        self.sync(DMA_BUF_SYNC_READ);
        Ok(self.map)
    }

    pub fn end_read(&self) {
        self.sync(DMA_BUF_SYNC_READ | DMA_BUF_SYNC_END);
    }

    fn sync(&self, flags: u64) {
        unsafe { libc::ioctl(self.staging.as_raw_fd(), DMA_BUF_IOCTL_SYNC, &flags) };
    }
}

pub const DMA_BUF_IOCTL_SYNC: libc::c_ulong = 0x4008_6200; // _IOW('b', 0, u64)
pub const DMA_BUF_SYNC_READ: u64 = 1;
pub const DMA_BUF_SYNC_END: u64 = 4;

const TEXTURE: &std::ffi::CStr = c"oriel-texture";

thread_local! {
    /// Deleted at the next blit, when the context is current.
    static RETIRED: RefCell<Vec<c_uint>> = const { RefCell::new(Vec::new()) };
}

unsafe extern "C" fn retire(texture: *mut c_void) {
    RETIRED.with_borrow_mut(|r| r.push(texture as usize as c_uint));
}

unsafe fn delete_retired() {
    RETIRED.with_borrow_mut(|r| {
        if !r.is_empty() {
            unsafe { glDeleteTextures(r.len() as i32, r.as_ptr()) };
            r.clear();
        }
    });
}
