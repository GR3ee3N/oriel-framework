//! Shows one page full screen with WPE WebKit, over Kodi.
//!
//!     oriel-shell --fb /dev/fbN --control FIFO [--feature Name=0|1]... <uri>
//!     oriel-shell --list-features
//!
//! The page's background is transparent, so Kodi shows wherever it draws
//! nothing.
//!
//! A crashed web process reloads the page; the third crash within a minute
//! exits with status 3.

mod control;
mod fb;
mod gl;

use glib::prelude::*;
use glib::translate::{FromGlibPtrFull, ToGlibPtr, from_glib};
use std::ptr;
use wpe_sys as wpe;

fn main() {
    let mut fb_path: Option<String> = None;
    let mut control_path: Option<String> = None;
    let mut uri = "about:blank".to_owned();
    let mut features: Vec<(String, bool)> = Vec::new();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--fb" => fb_path = Some(args.next().expect("--fb /dev/fbN")),
            "--control" => control_path = Some(args.next().expect("--control FIFO")),
            "--list-features" => {
                for (name, default) in all_features().iter().map(|(n, d, _)| (n, d)) {
                    println!("{name}={}", u8::from(*default));
                }
                return;
            }
            "--feature" => {
                let spec = args.next().expect("--feature Name=0|1");
                let (name, value) = spec.split_once('=').expect("--feature Name=0|1");
                features.push((name.to_owned(), value == "1"));
            }
            _ => uri = arg,
        }
    }

    let (Some(fb_path), Some(control_path)) = (fb_path, control_path) else {
        eprintln!("oriel-shell: --fb and --control are required");
        std::process::exit(2);
    };
    let (width, height) = fb::open(&fb_path).unwrap_or_else(|e| {
        eprintln!("oriel-shell: cannot open {fb_path}: {e}");
        std::process::exit(1);
    });

    let display = unsafe {
        let display = wpe::wpe_display_headless_new();
        let mut error = ptr::null_mut();
        if wpe::wpe_display_connect(display, &mut error) == 0 {
            let error = glib::Error::from_glib_full(error);
            eprintln!("oriel-shell: cannot open the display: {error}");
            std::process::exit(1);
        }
        glib::Object::from_glib_full(display.cast::<glib::gobject_ffi::GObject>())
    };

    let view_type: glib::Type = unsafe { from_glib(wpe::webkit_web_view_get_type()) };
    let view = glib::Object::with_mut_values(view_type, &mut [("display", display.to_value())]);
    let web_view: *mut wpe::WebKitWebView = view.as_ptr().cast();

    unsafe {
        let settings = wpe::webkit_web_view_get_settings(web_view);
        wpe::webkit_settings_set_enable_write_console_messages_to_stdout(settings, 1);
        wpe::webkit_settings_set_enable_developer_extras(settings, 1);
        let all = all_features();
        for (name, enabled) in &features {
            match all.iter().find(|(n, _, _)| n == name) {
                Some((_, _, feature)) => {
                    wpe::webkit_settings_set_feature_enabled(settings, *feature, i32::from(*enabled));
                    eprintln!("oriel-shell: feature {name}={}", u8::from(*enabled));
                }
                None => eprintln!("oriel-shell: unknown feature {name}"),
            }
        }
        wpe::webkit_web_view_set_background_color(web_view, &wpe::WebKitColor::default());

        let wpe_view = wpe::webkit_web_view_get_wpe_view(web_view);
        let toplevel = wpe::wpe_view_get_toplevel(wpe_view);
        wpe::wpe_toplevel_resize(toplevel, width, height);
        wpe::wpe_toplevel_fullscreen(toplevel);
        wpe::wpe_view_map(wpe_view);
        wpe::wpe_view_focus_in(wpe_view);
        fb::install(wpe_view);
        control::install(wpe_view, &control_path).unwrap_or_else(|e| {
            eprintln!("oriel-shell: control {control_path}: {e}");
            std::process::exit(1);
        });

        wpe::webkit_web_view_load_uri(web_view, uri.to_glib_none().0);
    }

    let deaths = std::cell::RefCell::new(Vec::<std::time::Instant>::new());
    view.connect_local("web-process-terminated", false, move |args| {
        let reason = args.get(1).and_then(|v| v.transform::<i32>().ok()).and_then(|v| v.get::<i32>().ok());
        let now = std::time::Instant::now();
        let mut deaths = deaths.borrow_mut();
        deaths.retain(|t| now.duration_since(*t).as_secs() < 60);
        deaths.push(now);
        eprintln!("oriel-shell: web process terminated (reason {reason:?}), {} in the last minute", deaths.len());
        if deaths.len() >= 3 {
            std::process::exit(3);
        }
        unsafe { wpe::webkit_web_view_reload(web_view) };
        None
    });

    glib::MainLoop::new(None, false).run();
    drop(view);
}

fn all_features() -> Vec<(String, bool, *mut wpe::WebKitFeature)> {
    unsafe {
        let list = wpe::webkit_settings_get_all_features();
        (0..wpe::webkit_feature_list_get_length(list))
            .map(|i| {
                let feature = wpe::webkit_feature_list_get(list, i);
                let name = std::ffi::CStr::from_ptr(wpe::webkit_feature_get_identifier(feature));
                (name.to_string_lossy().into_owned(), wpe::webkit_feature_get_default_value(feature) != 0, feature)
            })
            .collect()
    }
}
