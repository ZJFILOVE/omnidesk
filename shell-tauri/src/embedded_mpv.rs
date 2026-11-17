//! Embedded libmpv playback and software-rendered video frames.
//!
//! The render API writes RGB frames into a CPU buffer, which is JPEG-encoded and
//! emitted over `omnidesk:mpv-frame`. The React canvas decodes those frames,
//! giving us a portable embedded viewport on both X11 and Wayland without
//! depending on native window embedding (`--wid`).

use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_int, c_ulong, c_void};
use std::ptr;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use base64::Engine as _;
use jpeg_encoder::{ColorType, Encoder};
use libloading::Library;
use serde::Serialize;
use serde_json::json;

use crate::events::EventSink;
use crate::mpv_ipc::{
    build_loadfile_options, emit_state_snapshot, redact_media_url, validate_media_open_request,
    MediaOpenRequest, MpvPlaybackState, StateObserver,
};
use crate::protocol::RpcError;
use crate::redact::redact_text;

pub const MPV_FRAME_CHANNEL: &str = "omnidesk:mpv-frame";

/// Restore libmpv's numeric-locale requirement after GTK initializes the locale.
/// Call once on the application setup thread, before starting media workers;
/// changing the process locale in individual playback requests is unsafe.
pub fn initialize_numeric_locale() -> Result<(), RpcError> {
    // SAFETY: the static C string is valid and only LC_NUMERIC is changed at startup.
    let locale = unsafe { libc::setlocale(libc::LC_NUMERIC, c"C".as_ptr()) };
    if locale.is_null() {
        return Err(RpcError::internal_error("Unable to set LC_NUMERIC=C for libmpv"));
    }
    Ok(())
}

const MPV_FORMAT_STRING: c_int = 1;
const MPV_FORMAT_FLAG: c_int = 3;
const MPV_FORMAT_INT64: c_int = 4;
const MPV_FORMAT_DOUBLE: c_int = 5;

const MPV_RENDER_PARAM_API_TYPE: c_int = 1;
const MPV_RENDER_PARAM_SW_SIZE: c_int = 17;
const MPV_RENDER_PARAM_SW_FORMAT: c_int = 18;
const MPV_RENDER_PARAM_SW_STRIDE: c_int = 19;
const MPV_RENDER_PARAM_SW_POINTER: c_int = 20;
const MPV_RENDER_UPDATE_FRAME: u64 = 1;

const DEFAULT_FRAME_WIDTH: u32 = 960;
const DEFAULT_FRAME_HEIGHT: u32 = 540;
const DEFAULT_FRAME_FPS: u32 = 24;
const JPEG_QUALITY: u8 = 72;
const STATE_POLL_INTERVAL: Duration = Duration::from_millis(250);

#[repr(C)]
#[derive(Clone, Copy)]
struct MpvRenderParam {
    type_: c_int,
    data: *mut c_void,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct EmbeddedMpvProbe {
    pub available: bool,
    pub backend: &'static str,
    pub api_version: Option<String>,
    pub detail: String,
    pub unavailable_reason: Option<String>,
}

type MpvCreate = unsafe extern "C" fn() -> *mut c_void;
type MpvInitialize = unsafe extern "C" fn(*mut c_void) -> c_int;
type MpvTerminateDestroy = unsafe extern "C" fn(*mut c_void);
type MpvSetOptionString = unsafe extern "C" fn(*mut c_void, *const c_char, *const c_char) -> c_int;
type MpvSetProperty = unsafe extern "C" fn(*mut c_void, *const c_char, c_int, *mut c_void) -> c_int;
type MpvGetProperty = unsafe extern "C" fn(*mut c_void, *const c_char, c_int, *mut c_void) -> c_int;
type MpvCommand = unsafe extern "C" fn(*mut c_void, *const *const c_char) -> c_int;
type MpvFree = unsafe extern "C" fn(*mut c_void);
type MpvErrorString = unsafe extern "C" fn(c_int) -> *const c_char;
type MpvClientApiVersion = unsafe extern "C" fn() -> c_ulong;
type MpvRenderContextCreate =
    unsafe extern "C" fn(*mut *mut c_void, *mut c_void, *mut MpvRenderParam) -> c_int;
type MpvRenderContextSetUpdateCallback = unsafe extern "C" fn(
    *mut c_void,
    Option<unsafe extern "C" fn(*mut c_void)>,
    *mut c_void,
);
type MpvRenderContextUpdate = unsafe extern "C" fn(*mut c_void) -> u64;
type MpvRenderContextRender = unsafe extern "C" fn(*mut c_void, *mut MpvRenderParam) -> c_int;
type MpvRenderContextFree = unsafe extern "C" fn(*mut c_void);

struct LibMpv {
    _library: Library,
    create: MpvCreate,
    initialize: MpvInitialize,
    terminate_destroy: MpvTerminateDestroy,
    set_option_string: MpvSetOptionString,
    set_property: MpvSetProperty,
    get_property: MpvGetProperty,
    command: MpvCommand,
    free: MpvFree,
    error_string: MpvErrorString,
    client_api_version: MpvClientApiVersion,
    render_create: MpvRenderContextCreate,
    render_set_update_callback: MpvRenderContextSetUpdateCallback,
    render_update: MpvRenderContextUpdate,
    render_render: MpvRenderContextRender,
    render_free: MpvRenderContextFree,
}

impl LibMpv {
    fn load() -> Result<Self, String> {
        let mut last_error = None;
        let mut library = None;
        for name in ["libmpv.so.2", "libmpv.so"] {
            match unsafe { Library::new(name) } {
                Ok(lib) => {
                    library = Some(lib);
                    break;
                }
                Err(err) => last_error = Some(format!("{name}: {err}")),
            }
        }
        let library = library.ok_or_else(|| {
            format!(
                "libmpv shared library not found ({})",
                last_error.unwrap_or_else(|| "unknown loader error".to_string())
            )
        })?;

        macro_rules! symbol {
            ($name:literal, $ty:ty) => {{
                let symbol: libloading::Symbol<$ty> = unsafe {
                    library
                        .get($name)
                        .map_err(|err| format!("missing libmpv symbol {}: {err}", stringify!($name)))?
                };
                *symbol
            }};
        }

        Ok(Self {
            create: symbol!(b"mpv_create\0", MpvCreate),
            initialize: symbol!(b"mpv_initialize\0", MpvInitialize),
            terminate_destroy: symbol!(b"mpv_terminate_destroy\0", MpvTerminateDestroy),
            set_option_string: symbol!(b"mpv_set_option_string\0", MpvSetOptionString),
            set_property: symbol!(b"mpv_set_property\0", MpvSetProperty),
            get_property: symbol!(b"mpv_get_property\0", MpvGetProperty),
            command: symbol!(b"mpv_command\0", MpvCommand),
            free: symbol!(b"mpv_free\0", MpvFree),
            error_string: symbol!(b"mpv_error_string\0", MpvErrorString),
            client_api_version: symbol!(b"mpv_client_api_version\0", MpvClientApiVersion),
            render_create: symbol!(b"mpv_render_context_create\0", MpvRenderContextCreate),
            render_set_update_callback: symbol!(
                b"mpv_render_context_set_update_callback\0",
                MpvRenderContextSetUpdateCallback
            ),
            render_update: symbol!(b"mpv_render_context_update\0", MpvRenderContextUpdate),
            render_render: symbol!(b"mpv_render_context_render\0", MpvRenderContextRender),
            render_free: symbol!(b"mpv_render_context_free\0", MpvRenderContextFree),
            _library: library,
        })
    }

    fn error_message(&self, code: c_int) -> String {
        let ptr = unsafe { (self.error_string)(code) };
        if ptr.is_null() {
            return format!("libmpv error {code}");
        }
        unsafe { CStr::from_ptr(ptr) }
            .to_string_lossy()
            .trim()
            .to_string()
    }

    fn version(&self) -> String {
        let raw = unsafe { (self.client_api_version)() };
        format!("{}.{}", (raw >> 16) & 0xffff, raw & 0xffff)
    }
}

#[derive(Clone, Copy)]
struct MpvHandle(*mut c_void);

unsafe impl Send for MpvHandle {}
unsafe impl Sync for MpvHandle {}

impl MpvHandle {
    fn raw(self) -> *mut c_void {
        self.0
    }
}

struct RenderWake {
    dirty: Mutex<bool>,
    cv: Condvar,
}

struct RenderReady {
    result: Mutex<Option<Result<(), String>>>,
    cv: Condvar,
}

impl RenderReady {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            result: Mutex::new(None),
            cv: Condvar::new(),
        })
    }

    fn complete(&self, result: Result<(), String>) {
        if let Ok(mut slot) = self.result.lock() {
            *slot = Some(result);
        }
        self.cv.notify_all();
    }

    fn wait(&self, timeout: Duration) -> Result<(), String> {
        let guard = self
            .result
            .lock()
            .map_err(|_| "libmpv render readiness lock poisoned".to_string())?;
        let (guard, _) = self
            .cv
            .wait_timeout_while(guard, timeout, |slot| slot.is_none())
            .map_err(|_| "libmpv render readiness lock poisoned".to_string())?;
        match guard.as_ref() {
            Some(Ok(())) => Ok(()),
            Some(Err(err)) => Err(err.clone()),
            None => Err("libmpv render context did not become ready".to_string()),
        }
    }
}

impl RenderWake {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            dirty: Mutex::new(true),
            cv: Condvar::new(),
        })
    }

    fn notify(&self) {
        if let Ok(mut dirty) = self.dirty.lock() {
            *dirty = true;
        }
        self.cv.notify_one();
    }
}

unsafe extern "C" fn render_wakeup_callback(ctx: *mut c_void) {
    if ctx.is_null() {
        return;
    }
    let wake = unsafe { &*(ctx as *const RenderWake) };
    wake.notify();
}

pub struct EmbeddedMpv {
    lib: Arc<LibMpv>,
    handle: MpvHandle,
    state: Arc<RwLock<MpvPlaybackState>>,
    sink: Arc<RwLock<Option<Arc<dyn EventSink>>>>,
    observer: Arc<RwLock<Option<StateObserver>>>,
    wake: Arc<RenderWake>,
    stop: Arc<AtomicBool>,
    viewport_width: Arc<AtomicU32>,
    viewport_height: Arc<AtomicU32>,
    target_fps: Arc<AtomicU32>,
    frame_seq: Arc<AtomicU64>,
    frames_enabled: Arc<AtomicBool>,
    render_thread: Mutex<Option<JoinHandle<()>>>,
    poll_thread: Mutex<Option<JoinHandle<()>>>,
    shutdown_once: AtomicBool,
}

impl EmbeddedMpv {
    pub fn probe() -> EmbeddedMpvProbe {
        match LibMpv::load() {
            Ok(lib) => EmbeddedMpvProbe {
                available: true,
                backend: "libmpv-sw",
                api_version: Some(lib.version()),
                detail: format!(
                    "libmpv {} software-render API ready (embedded canvas)",
                    lib.version()
                ),
                unavailable_reason: None,
            },
            Err(err) => EmbeddedMpvProbe {
                available: false,
                backend: "libmpv-sw",
                api_version: None,
                detail: "libmpv render API unavailable".to_string(),
                unavailable_reason: Some(redact_text(&err)),
            },
        }
    }

    pub fn new() -> Result<Arc<Self>, RpcError> {
        let lib = Arc::new(LibMpv::load().map_err(|err| {
            RpcError::new("mpv_unavailable", format!("libmpv unavailable: {err}"))
        })?);
        let raw_handle = unsafe { (lib.create)() };
        if raw_handle.is_null() {
            return Err(RpcError::internal_error("libmpv mpv_create returned NULL"));
        }
        let handle = MpvHandle(raw_handle);

        let options = [
            ("config", "no"),
            ("terminal", "no"),
            ("idle", "yes"),
            ("keep-open", "yes"),
            ("vo", "libmpv"),
            ("hwdec", "auto-copy-safe"),
            ("audio-display", "no"),
            ("osc", "no"),
            ("osd-level", "0"),
            ("input-default-bindings", "no"),
            ("input-vo-keyboard", "no"),
            ("sw-fast", "yes"),
            ("vd-lavc-dr", "no"),
            ("audio-client-name", "OmniDesk"),
        ];
        for (name, value) in options {
            let (Ok(c_name), Ok(c_value)) = (CString::new(name), CString::new(value)) else {
                continue;
            };
            let rc = unsafe { (lib.set_option_string)(handle.0, c_name.as_ptr(), c_value.as_ptr()) };
            if rc < 0 && name == "vo" {
                let message = lib.error_message(rc);
                unsafe { (lib.terminate_destroy)(handle.0) };
                return Err(RpcError::new(
                    "mpv_unavailable",
                    format!("libmpv rejected vo=libmpv: {message}"),
                ));
            }
        }
        if std::env::var("OMNIDESK_MPV_NULL_AUDIO").ok().as_deref() == Some("1") {
            if let (Ok(c_name), Ok(c_value)) = (CString::new("ao"), CString::new("null")) {
                unsafe { (lib.set_option_string)(handle.0, c_name.as_ptr(), c_value.as_ptr()) };
            }
        }

        let rc = unsafe { (lib.initialize)(handle.0) };
        if rc < 0 {
            let message = lib.error_message(rc);
            unsafe { (lib.terminate_destroy)(handle.0) };
            return Err(RpcError::new(
                "mpv_unavailable",
                format!("libmpv initialize failed: {message}"),
            ));
        }

        let state = Arc::new(RwLock::new(MpvPlaybackState {
            available: true,
            initialized: true,
            paused: true,
            stopped: true,
            volume: 85,
            speed: 1.0,
            ..MpvPlaybackState::default()
        }));
        let sink = Arc::new(RwLock::new(None));
        let observer = Arc::new(RwLock::new(None));
        let wake = RenderWake::new();
        let stop = Arc::new(AtomicBool::new(false));
        let viewport_width = Arc::new(AtomicU32::new(DEFAULT_FRAME_WIDTH));
        let viewport_height = Arc::new(AtomicU32::new(DEFAULT_FRAME_HEIGHT));
        let target_fps = Arc::new(AtomicU32::new(DEFAULT_FRAME_FPS));
        let frame_seq = Arc::new(AtomicU64::new(0));
        let frames_enabled = Arc::new(AtomicBool::new(true));

        let this = Arc::new(Self {
            lib,
            handle,
            state,
            sink,
            observer,
            wake,
            stop,
            viewport_width,
            viewport_height,
            target_fps,
            frame_seq,
            frames_enabled,
            render_thread: Mutex::new(None),
            poll_thread: Mutex::new(None),
            shutdown_once: AtomicBool::new(false),
        });

        let render_ready = RenderReady::new();
        let render_thread = Self::spawn_render_thread(&this, Arc::clone(&render_ready))?;
        if let Ok(mut slot) = this.render_thread.lock() {
            *slot = Some(render_thread);
        }
        if let Err(err) = render_ready.wait(Duration::from_secs(3)) {
            this.shutdown();
            return Err(RpcError::new(
                "mpv_unavailable",
                format!("libmpv embedded renderer failed to start: {err}"),
            ));
        }
        let poll_thread = Self::spawn_poll_thread(Arc::clone(&this))?;
        if let Ok(mut slot) = this.poll_thread.lock() {
            *slot = Some(poll_thread);
        }
        Ok(this)
    }

    fn spawn_render_thread(
        this: &Arc<Self>,
        ready: Arc<RenderReady>,
    ) -> Result<JoinHandle<()>, RpcError> {
        let owner = Arc::clone(this);
        let lib = Arc::clone(&this.lib);
        let handle = this.handle;
        let wake = Arc::clone(&this.wake);
        let stop = Arc::clone(&this.stop);
        let width = Arc::clone(&this.viewport_width);
        let height = Arc::clone(&this.viewport_height);
        let fps = Arc::clone(&this.target_fps);
        let sink = Arc::clone(&this.sink);
        let seq = Arc::clone(&this.frame_seq);
        let frames_enabled = Arc::clone(&this.frames_enabled);
        thread::Builder::new()
            .name("omnidesk-libmpv-render".to_string())
            .spawn(move || {
                let raw_handle = handle.raw();
                let mut ctx: *mut c_void = ptr::null_mut();
                let api_type = b"sw\0";
                let mut params = [
                    MpvRenderParam {
                        type_: MPV_RENDER_PARAM_API_TYPE,
                        data: api_type.as_ptr() as *mut c_void,
                    },
                    MpvRenderParam {
                        type_: 0,
                        data: ptr::null_mut(),
                    },
                ];
                let rc = unsafe {
                    (lib.render_create)(&mut ctx, raw_handle, params.as_mut_ptr())
                };
                if rc < 0 || ctx.is_null() {
                    let message =
                        format!("libmpv render context creation failed: {}", lib.error_message(rc));
                    Self::write_render_error(
                        &owner,
                        message.clone(),
                    );
                    ready.complete(Err(message));
                    return;
                }

                let wake_ptr = Arc::as_ptr(&wake) as *mut c_void;
                unsafe {
                    (lib.render_set_update_callback)(
                        ctx,
                        Some(render_wakeup_callback),
                        wake_ptr,
                    )
                };
                wake.notify();
                ready.complete(Ok(()));

                let mut buffer: Vec<u32> = Vec::new();
                let mut last_frame = Instant::now()
                    .checked_sub(Duration::from_secs(1))
                    .unwrap_or_else(Instant::now);

                loop {
                    if stop.load(Ordering::Acquire) {
                        break;
                    }
                    if let Ok(mut dirty) = wake.dirty.lock() {
                        while !*dirty && !stop.load(Ordering::Acquire) {
                            let (guard, _) = wake
                                .cv
                                .wait_timeout(dirty, Duration::from_millis(100))
                                .unwrap_or_else(|err| err.into_inner());
                            dirty = guard;
                        }
                        *dirty = false;
                    }
                    if stop.load(Ordering::Acquire) {
                        break;
                    }
                    if !frames_enabled.load(Ordering::Acquire) {
                        thread::sleep(Duration::from_millis(100));
                        continue;
                    }

                    let flags = unsafe { (lib.render_update)(ctx) };
                    if flags & MPV_RENDER_UPDATE_FRAME == 0 {
                        continue;
                    }

                    let frame_width = width.load(Ordering::Relaxed).max(1);
                    let frame_height = height.load(Ordering::Relaxed).max(1);
                    let target_fps = fps.load(Ordering::Relaxed).clamp(1, 60);
                    let frame_interval = Duration::from_millis(1000 / target_fps as u64);
                    let buffer_pixels = frame_width as usize * frame_height as usize;
                    if buffer.len() != buffer_pixels {
                        buffer.resize(buffer_pixels, 0);
                    }

                    let mut size = [frame_width as c_int, frame_height as c_int];
                    let format = b"rgb0\0";
                    let mut stride = frame_width as usize * 4;
                    let data = buffer.as_mut_ptr() as *mut c_void;
                    let mut render_params = [
                        MpvRenderParam {
                            type_: MPV_RENDER_PARAM_SW_SIZE,
                            data: size.as_mut_ptr() as *mut c_void,
                        },
                        MpvRenderParam {
                            type_: MPV_RENDER_PARAM_SW_FORMAT,
                            data: format.as_ptr() as *mut c_void,
                        },
                        MpvRenderParam {
                            type_: MPV_RENDER_PARAM_SW_STRIDE,
                            data: &mut stride as *mut usize as *mut c_void,
                        },
                        MpvRenderParam {
                            type_: MPV_RENDER_PARAM_SW_POINTER,
                            data,
                        },
                        MpvRenderParam {
                            type_: 0,
                            data: ptr::null_mut(),
                        },
                    ];
                    let render_rc =
                        unsafe { (lib.render_render)(ctx, render_params.as_mut_ptr()) };
                    if render_rc < 0 {
                        Self::write_render_error(
                            &owner,
                            format!("libmpv frame render failed: {}", lib.error_message(render_rc)),
                        );
                        thread::sleep(Duration::from_millis(100));
                        continue;
                    }

                    if last_frame.elapsed() >= frame_interval {
                        let frame_bytes = rgb0_to_rgb(&buffer, frame_width, frame_height, stride);
                        let mut jpeg = Vec::with_capacity(frame_bytes.len() / 8);
                        let encoder = Encoder::new(&mut jpeg, JPEG_QUALITY);
                        if encoder
                            .encode(
                                &frame_bytes,
                                frame_width as u16,
                                frame_height as u16,
                                ColorType::Rgb,
                            )
                            .is_ok()
                        {
                            if let Ok(sink_guard) = sink.read() {
                                if let Some(event_sink) = sink_guard.as_ref() {
                                    let sequence = seq.fetch_add(1, Ordering::Relaxed) + 1;
                                    event_sink.emit_event(
                                        MPV_FRAME_CHANNEL,
                                        json!({
                                            "seq": sequence,
                                            "width": frame_width,
                                            "height": frame_height,
                                            "mime": "image/jpeg",
                                            "data": base64::engine::general_purpose::STANDARD.encode(&jpeg),
                                        }),
                                    );
                                    last_frame = Instant::now();
                                }
                            }
                        }
                    }

                    let elapsed = last_frame.elapsed();
                    if elapsed < frame_interval {
                        thread::sleep(frame_interval - elapsed);
                    }
                }

                unsafe { (lib.render_free)(ctx) };
            })
            .map_err(|err| RpcError::internal_error(format!("spawn libmpv render thread failed: {err}")))
    }

    fn spawn_poll_thread(this: Arc<Self>) -> Result<JoinHandle<()>, RpcError> {
        thread::Builder::new()
            .name("omnidesk-libmpv-state".to_string())
            .spawn(move || {
                while !this.stop.load(Ordering::Acquire) {
                    this.refresh_state();
                    thread::sleep(STATE_POLL_INTERVAL);
                }
            })
            .map_err(|err| RpcError::internal_error(format!("spawn libmpv state thread failed: {err}")))
    }

    fn write_render_error(this: &Arc<Self>, message: String) {
        if let Ok(mut guard) = this.state.write() {
            guard.last_error = redact_text(&message);
        }
        this.emit_state();
    }

    fn emit_state(&self) {
        let snapshot = self
            .state
            .read()
            .map(|guard| guard.clone())
            .unwrap_or_default();
        emit_state_snapshot(&snapshot, &self.sink, &self.observer);
    }

    fn set_state(&self, next: MpvPlaybackState) {
        let changed = {
            let Ok(mut guard) = self.state.write() else {
                return;
            };
            if *guard == next {
                false
            } else {
                *guard = next;
                true
            }
        };
        if changed {
            self.emit_state();
        }
    }

    fn read_flag(&self, name: &str) -> Option<bool> {
        let Ok(c_name) = CString::new(name) else {
            return None;
        };
        let mut value: c_int = 0;
        let rc = unsafe {
            (self.lib.get_property)(
                self.handle.0,
                c_name.as_ptr(),
                MPV_FORMAT_FLAG,
                &mut value as *mut c_int as *mut c_void,
            )
        };
        (rc >= 0).then_some(value != 0)
    }

    fn read_i64(&self, name: &str) -> Option<i64> {
        let Ok(c_name) = CString::new(name) else {
            return None;
        };
        let mut value: i64 = 0;
        let rc = unsafe {
            (self.lib.get_property)(
                self.handle.0,
                c_name.as_ptr(),
                MPV_FORMAT_INT64,
                &mut value as *mut i64 as *mut c_void,
            )
        };
        (rc >= 0).then_some(value)
    }

    fn read_f64(&self, name: &str) -> Option<f64> {
        let Ok(c_name) = CString::new(name) else {
            return None;
        };
        let mut value: f64 = 0.0;
        let rc = unsafe {
            (self.lib.get_property)(
                self.handle.0,
                c_name.as_ptr(),
                MPV_FORMAT_DOUBLE,
                &mut value as *mut f64 as *mut c_void,
            )
        };
        (rc >= 0 && value.is_finite()).then_some(value)
    }

    fn read_string(&self, name: &str) -> Option<String> {
        let Ok(c_name) = CString::new(name) else {
            return None;
        };
        let mut value: *mut c_char = ptr::null_mut();
        let rc = unsafe {
            (self.lib.get_property)(
                self.handle.0,
                c_name.as_ptr(),
                MPV_FORMAT_STRING,
                &mut value as *mut *mut c_char as *mut c_void,
            )
        };
        if rc < 0 || value.is_null() {
            return None;
        }
        let text = unsafe { CStr::from_ptr(value) }
            .to_string_lossy()
            .trim()
            .to_string();
        unsafe { (self.lib.free)(value as *mut c_void) };
        (!text.is_empty()).then_some(text)
    }

    fn refresh_state(&self) {
        let mut next = self
            .state
            .read()
            .map(|guard| guard.clone())
            .unwrap_or_default();
        let paused = self.read_flag("pause").unwrap_or(next.paused);
        let idle = self.read_flag("idle-active").unwrap_or(false);
        let eof = self.read_flag("eof-reached").unwrap_or(false);
        next.available = true;
        next.initialized = true;
        next.paused = paused;
        next.stopped = idle || eof;
        next.playing = !paused && !next.stopped;
        next.position_sec = self
            .read_f64("playback-time")
            .unwrap_or(next.position_sec)
            .max(0.0);
        next.duration_sec = self
            .read_f64("duration")
            .unwrap_or(next.duration_sec)
            .max(0.0);
        next.buffering = self.read_flag("paused-for-cache").unwrap_or(false);
        next.speed = self
            .read_f64("speed")
            .unwrap_or(next.speed)
            .clamp(0.25, 4.0);
        next.volume = self
            .read_f64("volume")
            .unwrap_or(next.volume as f64)
            .round()
            .clamp(0.0, 100.0) as i64;
        next.video_width = self
            .read_i64("video-params/w")
            .unwrap_or(next.video_width as i64)
            .max(0);
        next.video_height = self
            .read_i64("video-params/h")
            .unwrap_or(next.video_height as i64)
            .max(0);
        next.fps = self.read_f64("estimated-vf-fps").unwrap_or(next.fps);
        next.video_bitrate = self
            .read_f64("video-bitrate")
            .unwrap_or(next.video_bitrate);
        next.decoder_drop_frames = self
            .read_i64("decoder-frame-drop-count")
            .unwrap_or(next.decoder_drop_frames);
        next.output_drop_frames = self
            .read_i64("frame-drop-count")
            .unwrap_or(next.output_drop_frames);
        next.audio_samplerate = self
            .read_i64("audio-params/samplerate")
            .unwrap_or(next.audio_samplerate);
        next.audio_channels = self
            .read_i64("audio-params/channel-count")
            .unwrap_or(next.audio_channels);
        next.cache_duration_sec = self
            .read_f64("demuxer-cache-duration")
            .unwrap_or(next.cache_duration_sec);
        next.video_codec = self.read_string("video-codec").unwrap_or(next.video_codec);
        next.audio_codec = self.read_string("audio-codec").unwrap_or(next.audio_codec);
        next.hwdec_current = self
            .read_string("hwdec-current")
            .unwrap_or(next.hwdec_current);
        next.hwdec_active =
            !next.hwdec_current.is_empty() && !next.hwdec_current.eq_ignore_ascii_case("no");
        if let Some(title) = self.read_string("media-title") {
            next.title = title;
        }
        self.set_state(next);
    }

    fn command(&self, args: &[&str]) -> Result<(), RpcError> {
        let cstrings = args
            .iter()
            .map(|arg| {
                CString::new(*arg)
                    .map_err(|_| RpcError::invalid_argument("mpv command argument contains NUL"))
            })
            .collect::<Result<Vec<_>, _>>()?;
        let mut pointers = cstrings
            .iter()
            .map(|arg| arg.as_ptr())
            .collect::<Vec<*const c_char>>();
        pointers.push(ptr::null());
        let rc = unsafe { (self.lib.command)(self.handle.0, pointers.as_ptr()) };
        if rc < 0 {
            let message = redact_text(&format!(
                "mpv command {} failed: {}",
                args.first().copied().unwrap_or("unknown"),
                self.lib.error_message(rc)
            ));
            if let Ok(mut guard) = self.state.write() {
                guard.last_error = message.clone();
            }
            self.emit_state();
            return Err(RpcError::new("mpv_command_failed", message));
        }
        Ok(())
    }

    fn set_flag(&self, name: &str, value: bool) -> Result<(), RpcError> {
        let c_name = CString::new(name)
            .map_err(|_| RpcError::invalid_argument("mpv property name contains NUL"))?;
        let mut raw: c_int = if value { 1 } else { 0 };
        let rc = unsafe {
            (self.lib.set_property)(
                self.handle.0,
                c_name.as_ptr(),
                MPV_FORMAT_FLAG,
                &mut raw as *mut c_int as *mut c_void,
            )
        };
        if rc < 0 {
            return Err(RpcError::new(
                "mpv_command_failed",
                format!("set {name} failed: {}", self.lib.error_message(rc)),
            ));
        }
        Ok(())
    }

    fn set_i64(&self, name: &str, value: i64) -> Result<(), RpcError> {
        let c_name = CString::new(name)
            .map_err(|_| RpcError::invalid_argument("mpv property name contains NUL"))?;
        let mut raw = value;
        let rc = unsafe {
            (self.lib.set_property)(
                self.handle.0,
                c_name.as_ptr(),
                MPV_FORMAT_INT64,
                &mut raw as *mut i64 as *mut c_void,
            )
        };
        if rc < 0 {
            return Err(RpcError::new(
                "mpv_command_failed",
                format!("set {name} failed: {}", self.lib.error_message(rc)),
            ));
        }
        Ok(())
    }

    fn set_f64(&self, name: &str, value: f64) -> Result<(), RpcError> {
        let c_name = CString::new(name)
            .map_err(|_| RpcError::invalid_argument("mpv property name contains NUL"))?;
        let mut raw = value;
        let rc = unsafe {
            (self.lib.set_property)(
                self.handle.0,
                c_name.as_ptr(),
                MPV_FORMAT_DOUBLE,
                &mut raw as *mut f64 as *mut c_void,
            )
        };
        if rc < 0 {
            return Err(RpcError::new(
                "mpv_command_failed",
                format!("set {name} failed: {}", self.lib.error_message(rc)),
            ));
        }
        Ok(())
    }

    pub fn status(&self) -> MpvPlaybackState {
        self.state
            .read()
            .map(|guard| guard.clone())
            .unwrap_or_default()
    }

    pub fn api_version(&self) -> String {
        self.lib.version()
    }

    pub fn open(&self, req: MediaOpenRequest) -> Result<MpvPlaybackState, RpcError> {
        validate_media_open_request(&req)?;
        let options = build_loadfile_options(&req)
            .replace("hwdec=\"auto-safe\"", "hwdec=\"auto-copy-safe\"");
        self.command(&["loadfile", req.url.trim(), "replace", "0", &options])?;
        self.set_flag("pause", req.paused.unwrap_or(false))?;

        let start = req.start_seconds.unwrap_or(0.0).max(0.0);
        let item_id = req.item_id.clone().unwrap_or_default();
        let title = req.title.clone().unwrap_or_default();
        let artist = req.artist.clone().unwrap_or_default();
        let album = req.album.clone().unwrap_or_default();
        let current_url = redact_media_url(&req.url);
        let cover_url = req.cover_url.as_deref().map(redact_media_url).unwrap_or_default();
        {
            let Ok(mut guard) = self.state.write() else {
                return Err(RpcError::internal_error("mpv state lock poisoned"));
            };
            guard.available = true;
            guard.initialized = true;
            guard.playing = !req.paused.unwrap_or(false);
            guard.paused = req.paused.unwrap_or(false);
            guard.stopped = false;
            guard.buffering = false;
            guard.position_sec = start;
            guard.duration_sec = 0.0;
            guard.current_url = current_url;
            guard.item_id = item_id;
            guard.title = title;
            guard.artist = artist;
            guard.album = album;
            guard.cover_url = cover_url;
            guard.last_error.clear();
            guard.ipc_socket_path = None;
        }
        self.emit_state();
        Ok(self.status())
    }

    pub fn pause(&self) -> Result<MpvPlaybackState, RpcError> {
        self.set_flag("pause", true)?;
        self.update_pause_state(true);
        Ok(self.status())
    }

    pub fn resume(&self) -> Result<MpvPlaybackState, RpcError> {
        self.set_flag("pause", false)?;
        self.update_pause_state(false);
        Ok(self.status())
    }

    pub fn toggle_pause(&self) -> Result<MpvPlaybackState, RpcError> {
        let paused = self.read_flag("pause").unwrap_or(self.status().paused);
        if paused {
            self.resume()
        } else {
            self.pause()
        }
    }

    pub fn stop(&self) -> Result<MpvPlaybackState, RpcError> {
        self.command(&["stop"])?;
        if let Ok(mut guard) = self.state.write() {
            guard.playing = false;
            guard.paused = true;
            guard.stopped = true;
        }
        self.emit_state();
        Ok(self.status())
    }

    pub fn seek(&self, seconds: f64, relative: bool) -> Result<MpvPlaybackState, RpcError> {
        if !seconds.is_finite() {
            return Err(RpcError::invalid_argument("seek seconds must be finite"));
        }
        let mode = if relative { "relative" } else { "absolute" };
        self.command(&["seek", &format!("{seconds:.3}"), mode])?;
        if let Ok(mut guard) = self.state.write() {
            if relative {
                guard.position_sec = (guard.position_sec + seconds).max(0.0);
            } else {
                guard.position_sec = seconds.max(0.0);
            }
        }
        self.emit_state();
        Ok(self.status())
    }

    pub fn set_volume(&self, volume: i64) -> Result<MpvPlaybackState, RpcError> {
        let volume = volume.clamp(0, 100);
        self.set_i64("volume", volume)?;
        if let Ok(mut guard) = self.state.write() {
            guard.volume = volume;
        }
        self.emit_state();
        Ok(self.status())
    }

    pub fn set_speed(&self, speed: f64) -> Result<MpvPlaybackState, RpcError> {
        if !speed.is_finite() {
            return Err(RpcError::invalid_argument("speed must be finite"));
        }
        let speed = speed.clamp(0.25, 4.0);
        self.set_f64("speed", speed)?;
        if let Ok(mut guard) = self.state.write() {
            guard.speed = speed;
        }
        self.emit_state();
        Ok(self.status())
    }

    fn update_pause_state(&self, paused: bool) {
        if let Ok(mut guard) = self.state.write() {
            guard.paused = paused;
            guard.playing = !paused && !guard.stopped;
        }
        self.emit_state();
    }

    pub fn set_viewport(&self, width: u32, height: u32, fps: u32) {
        self.viewport_width
            .store(width.clamp(320, 1280), Ordering::Relaxed);
        self.viewport_height
            .store(height.clamp(180, 720), Ordering::Relaxed);
        self.target_fps.store(fps.clamp(1, 60), Ordering::Relaxed);
        self.wake.notify();
    }

    pub fn set_frames_enabled(&self, enabled: bool) {
        self.frames_enabled.store(enabled, Ordering::Release);
        if enabled {
            self.wake.notify();
        }
    }

    pub fn set_event_sink(&self, sink: Arc<dyn EventSink>) {
        if let Ok(mut guard) = self.sink.write() {
            *guard = Some(sink);
        }
        self.wake.notify();
    }

    pub fn set_state_observer(&self, observer: StateObserver) {
        if let Ok(mut guard) = self.observer.write() {
            *guard = Some(observer);
        }
    }

    pub fn shutdown(&self) {
        if self.shutdown_once.swap(true, Ordering::AcqRel) {
            return;
        }
        self.stop.store(true, Ordering::Release);
        self.wake.notify();
        if let Ok(mut slot) = self.poll_thread.lock() {
            if let Some(handle) = slot.take() {
                let _ = handle.join();
            }
        }
        if let Ok(mut slot) = self.render_thread.lock() {
            if let Some(handle) = slot.take() {
                let _ = handle.join();
            }
        }
        unsafe { (self.lib.terminate_destroy)(self.handle.0) };
    }
}

impl Drop for EmbeddedMpv {
    fn drop(&mut self) {
        self.shutdown();
    }
}

fn rgb0_to_rgb(buffer: &[u32], width: u32, height: u32, stride: usize) -> Vec<u8> {
    let width = width as usize;
    let height = height as usize;
    let bytes = unsafe {
        std::slice::from_raw_parts(buffer.as_ptr() as *const u8, buffer.len() * 4)
    };
    let mut out = Vec::with_capacity(width * height * 3);
    for y in 0..height {
        let row = &bytes[y * stride..];
        for x in 0..width {
            let offset = x * 4;
            out.extend_from_slice(&row[offset..offset + 3]);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    struct CaptureSink {
        frames: Arc<Mutex<Vec<serde_json::Value>>>,
    }

    impl EventSink for CaptureSink {
        fn emit_event(&self, event: &str, payload: serde_json::Value) {
            if event == MPV_FRAME_CHANNEL {
                if let Ok(mut frames) = self.frames.lock() {
                    frames.push(payload);
                }
            }
        }
    }

    #[test]
    fn embedded_mpv_renders_real_frames() {
        if std::env::var("OMNIDESK_MPV_E2E").ok().as_deref() != Some("1") {
            eprintln!(
                "skipping embedded libmpv frame test: set OMNIDESK_MPV_E2E=1 (needs libmpv + ffmpeg)"
            );
            return;
        }
        let probe = EmbeddedMpv::probe();
        assert!(probe.available, "{probe:?}");

        let media = std::env::temp_dir().join(format!(
            "omnidesk-embedded-test-{}.mp4",
            std::process::id()
        ));
        let status = Command::new("ffmpeg")
            .args([
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x180:rate=12",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-t",
                "2",
                "-pix_fmt",
                "yuv420p",
                "-shortest",
            ])
            .arg(&media)
            .status()
            .expect("ffmpeg should be available");
        assert!(status.success(), "ffmpeg failed to create test media");

        // GTK adopts the user's locale before Tauri setup. Reproduce that here;
        // otherwise a Rust test starts in C and misses mpv_create returning NULL.
        let locale_name = std::env::var("OMNIDESK_MPV_TEST_LOCALE").unwrap_or_else(|_| "C".into());
        let locale_name = CString::new(locale_name).unwrap();
        assert!(!unsafe { libc::setlocale(libc::LC_NUMERIC, locale_name.as_ptr()) }.is_null());
        initialize_numeric_locale().expect("numeric locale should be restored after GUI startup");
        let mpv = EmbeddedMpv::new().expect("embedded libmpv should initialize");
        let frames = Arc::new(Mutex::new(Vec::new()));
        mpv.set_event_sink(Arc::new(CaptureSink {
            frames: Arc::clone(&frames),
        }));
        mpv.set_viewport(320, 180, 12);
        mpv.open(MediaOpenRequest {
            url: format!("file://{}", media.display()),
            paused: Some(false),
            ..MediaOpenRequest::default()
        })
        .expect("embedded open should succeed");

        for _ in 0..60 {
            if frames.lock().map(|guard| !guard.is_empty()).unwrap_or(false) {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
        let captured = frames.lock().map(|guard| guard.clone()).unwrap_or_default();
        mpv.shutdown();
        let _ = std::fs::remove_file(&media);
        assert!(
            !captured.is_empty(),
            "expected at least one JPEG frame on {MPV_FRAME_CHANNEL}"
        );
        let first = &captured[0];
        assert_eq!(first["mime"], "image/jpeg");
        assert!(first["data"].as_str().is_some_and(|data| data.len() > 100));
    }
}
