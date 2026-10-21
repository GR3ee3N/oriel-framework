//! Hand-written bindings for what the shell uses of WPE WebKit and WPEPlatform.

#![allow(non_camel_case_types)]

use glib_sys::{GError, GType, gboolean};
use gobject_sys::GObjectClass;
use std::ffi::{c_char, c_int, c_uint};

macro_rules! opaque {
    ($($name:ident),* $(,)?) => {
        $(#[repr(C)] pub struct $name { _private: [u8; 0] })*
    };
}

opaque!(
    WebKitWebView,
    WebKitSettings,
    WebKitFeature,
    WebKitFeatureList,
    WPEDisplay,
    WPEView,
    WPEToplevel,
    WPEBuffer,
    WPEKeymap,
    WPEEvent
);

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct WPERectangle {
    pub x: c_int,
    pub y: c_int,
    pub width: c_int,
    pub height: c_int,
}

pub type RenderBufferFn =
    unsafe extern "C" fn(*mut WPEView, *mut WPEBuffer, *const WPERectangle, c_uint, *mut *mut GError) -> gboolean;

/// Only up to the vfunc we replace.
#[repr(C)]
pub struct WPEViewClass {
    pub parent_class: GObjectClass,
    pub buffers_changed: Option<unsafe extern "C" fn(*mut WPEView, *mut *mut WPEBuffer, c_uint)>,
    pub render_buffer: Option<RenderBufferFn>,
}

pub const WPE_EVENT_KEYBOARD_KEY_DOWN: c_uint = 7;
pub const WPE_EVENT_KEYBOARD_KEY_UP: c_uint = 8;
pub const WPE_INPUT_SOURCE_KEYBOARD: c_uint = 2;

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct WebKitColor {
    pub red: f64,
    pub green: f64,
    pub blue: f64,
    pub alpha: f64,
}

unsafe extern "C" {
    pub fn webkit_web_view_get_type() -> GType;
    pub fn webkit_web_view_get_settings(web_view: *mut WebKitWebView) -> *mut WebKitSettings;
    pub fn webkit_web_view_get_wpe_view(web_view: *mut WebKitWebView) -> *mut WPEView;
    pub fn webkit_web_view_load_uri(web_view: *mut WebKitWebView, uri: *const c_char);
    pub fn webkit_web_view_reload(web_view: *mut WebKitWebView);
    pub fn webkit_web_view_set_background_color(web_view: *mut WebKitWebView, color: *const WebKitColor);

    pub fn webkit_settings_set_enable_developer_extras(settings: *mut WebKitSettings, enabled: gboolean);
    pub fn webkit_settings_set_enable_write_console_messages_to_stdout(
        settings: *mut WebKitSettings,
        enabled: gboolean,
    );

    pub fn webkit_settings_get_all_features() -> *mut WebKitFeatureList;
    pub fn webkit_settings_set_feature_enabled(
        settings: *mut WebKitSettings,
        feature: *mut WebKitFeature,
        enabled: gboolean,
    );
    pub fn webkit_feature_list_get_length(list: *mut WebKitFeatureList) -> usize;
    pub fn webkit_feature_list_get(list: *mut WebKitFeatureList, index: usize) -> *mut WebKitFeature;
    pub fn webkit_feature_get_identifier(feature: *mut WebKitFeature) -> *const c_char;
    pub fn webkit_feature_get_default_value(feature: *mut WebKitFeature) -> gboolean;

    pub fn wpe_display_connect(display: *mut WPEDisplay, error: *mut *mut GError) -> gboolean;

    pub fn wpe_view_get_toplevel(view: *mut WPEView) -> *mut WPEToplevel;
    pub fn wpe_view_map(view: *mut WPEView);
    pub fn wpe_view_focus_in(view: *mut WPEView);
    pub fn wpe_view_set_visible(view: *mut WPEView, visible: gboolean);
    pub fn wpe_toplevel_fullscreen(toplevel: *mut WPEToplevel) -> gboolean;
    pub fn wpe_toplevel_resize(toplevel: *mut WPEToplevel, width: c_int, height: c_int) -> gboolean;

    pub fn wpe_display_headless_new() -> *mut WPEDisplay;
    pub fn wpe_view_headless_get_type() -> GType;
    pub fn wpe_view_get_display(view: *mut WPEView) -> *mut WPEDisplay;
    pub fn wpe_view_event(view: *mut WPEView, event: *mut WPEEvent);
    pub fn wpe_view_buffer_rendered(view: *mut WPEView, buffer: *mut WPEBuffer);
    pub fn wpe_view_buffer_released(view: *mut WPEView, buffer: *mut WPEBuffer);

    pub fn wpe_buffer_get_width(buffer: *mut WPEBuffer) -> c_int;
    pub fn wpe_buffer_get_height(buffer: *mut WPEBuffer) -> c_int;
    pub fn wpe_buffer_dma_buf_get_type() -> GType;
    pub fn wpe_buffer_dma_buf_get_fd(buffer: *mut WPEBuffer, plane: c_uint) -> c_int;
    pub fn wpe_buffer_dma_buf_get_offset(buffer: *mut WPEBuffer, plane: c_uint) -> u32;
    pub fn wpe_buffer_dma_buf_get_stride(buffer: *mut WPEBuffer, plane: c_uint) -> u32;
    pub fn wpe_buffer_dma_buf_get_modifier(buffer: *mut WPEBuffer) -> u64;
    pub fn wpe_buffer_dma_buf_get_format(buffer: *mut WPEBuffer) -> u32;
    pub fn wpe_buffer_dma_buf_get_n_planes(buffer: *mut WPEBuffer) -> u32;

    pub fn wpe_display_get_keymap(display: *mut WPEDisplay) -> *mut WPEKeymap;
    pub fn wpe_keymap_translate_keyboard_state(
        keymap: *mut WPEKeymap,
        keycode: c_uint,
        modifiers: c_uint,
        group: c_int,
        keyval: *mut c_uint,
        effective_group: *mut c_int,
        level: *mut c_int,
        consumed_modifiers: *mut c_uint,
    ) -> gboolean;
    pub fn wpe_event_keyboard_new(
        kind: c_uint,
        view: *mut WPEView,
        source: c_uint,
        time: u32,
        modifiers: c_uint,
        keycode: c_uint,
        keyval: c_uint,
    ) -> *mut WPEEvent;
    pub fn wpe_event_unref(event: *mut WPEEvent);
}
