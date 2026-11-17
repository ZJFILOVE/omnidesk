//! Real mpv probe (`libmpv` + `mpv` executable/IPC) and `--input-ipc-server` playback controller.

use std::ffi::CStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::process::CommandExt;

use serde::Serialize;
use serde_json::{json, Value};

use crate::daemon::{find_executable_in_path, is_executable_file, terminate_child_process_group};
use crate::embedded_mpv::EmbeddedMpv;
use crate::events::EventSink;
use crate::mpv_ipc::{
    build_loadfile_options, emit_state_snapshot, redact_media_url, validate_media_open_request,
    MediaOpenRequest, MpvIpcClient, MpvPlaybackState, StateObserver,
};
use crate::paths::{ensure_private_dir, has_parent_or_nul, OmnideskPaths};
use crate::protocol::RpcError;
use crate::redact::redact_text;

const MPV_CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
const MPV_COMMAND_TIMEOUT: Duration = Duration::from_secs(3);
const MPV_SHUTDOWN_GRACE: Duration = Duration::from_millis(800);

#[cfg(omnidesk_libmpv_linked)]
mod libmpv_ffi {
    use std::os::raw::c_ulong;

    extern "C" {
        pub fn mpv_client_api_version() -> c_ulong;
    }

    pub fn linked_version() -> Option<(u32, u32)> {
        // SAFETY: `mpv_client_api_version` is a stateless getter returning `(major << 16) | minor`.
        let raw = unsafe { mpv_client_api_version() };
        let major = ((raw >> 16) & 0xffff) as u32;
        let minor = (raw & 0xffff) as u32;
        Some((major, minor))
    }
}

#[cfg(not(omnidesk_libmpv_linked))]
mod libmpv_ffi {
    /// Compile-time stub when `libmpv` development library is not feature-linked.
    pub fn linked_version() -> Option<(u32, u32)> {
        None
    }
}

#[derive(Debug, Clone)]
pub struct MpvProbeConfig {
    pub mpv_bin_override: Option<PathBuf>,
    pub path_env: Option<String>,
    pub allow_dlopen_libmpv: bool,
    pub extra_spawn_args: Vec<String>,
}

impl Default for MpvProbeConfig {
    fn default() -> Self {
        Self {
            mpv_bin_override: None,
            path_env: std::env::var("PATH").ok(),
            allow_dlopen_libmpv: true,
            extra_spawn_args: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct MpvProbeStatus {
    pub available: bool,
    pub initialized: bool,
    pub backend: String,
    pub hwdec: String,
    pub hwdec_active: bool,
    pub hwdec_current: String,
    pub status: String,
    pub detail: String,
    pub executable_path: Option<String>,
    pub executable_version: Option<String>,
    pub ipc_supported: bool,
    pub libmpv_available: bool,
    pub libmpv_version: Option<String>,
    pub libmpv_linked: bool,
    pub unavailable_reason: Option<String>,
}

/// Probe `mpv` executable, `--input-ipc-server` capability, and `libmpv` availability.
pub fn probe() -> MpvProbeStatus {
    probe_with(&MpvProbeConfig::default(), None)
}

pub fn probe_with(cfg: &MpvProbeConfig, live_state: Option<&MpvPlaybackState>) -> MpvProbeStatus {
    let (libmpv_available, libmpv_version, libmpv_linked, libmpv_err) =
        probe_libmpv(cfg.allow_dlopen_libmpv);

    let exe_path = resolve_mpv_executable(cfg);
    let mut executable_version = None;
    let mut ipc_supported = false;
    let mut exe_issue: Option<String> = None;

    if let Some(ref bin) = exe_path {
        match probe_mpv_executable(bin) {
            Ok((ver, has_ipc)) => {
                executable_version = ver;
                ipc_supported = has_ipc;
                if !has_ipc {
                    exe_issue = Some(format!(
                        "mpv at {} does not advertise --input-ipc-server",
                        bin.display()
                    ));
                }
            }
            Err(reason) => {
                exe_issue = Some(reason);
            }
        }
    } else {
        exe_issue = Some("mpv executable not found in PATH".to_string());
    }

    let available = ipc_supported;
    let initialized = live_state.is_some_and(|s| s.initialized);
    let hwdec_active = live_state.is_some_and(|s| s.hwdec_active);
    let hwdec_current = live_state
        .map(|s| s.hwdec_current.clone())
        .unwrap_or_default();

    let unavailable_reason = if available {
        None
    } else {
        let base = exe_issue.unwrap_or_else(|| "mpv IPC backend unavailable".to_string());
        let suffix = match (&libmpv_version, &libmpv_err) {
            (Some(v), _) if libmpv_linked => {
                format!(" (libmpv {v} FFI linked, but IPC playback requires mpv binary)")
            }
            (Some(v), _) => {
                format!(" (libmpv {v} shared library detected, but IPC playback requires mpv binary)")
            }
            (None, Some(err)) => format!("; libmpv unavailable ({err})"),
            (None, None) => "; libmpv not loaded".to_string(),
        };
        Some(redact_text(&format!("{base}{suffix}")))
    };

    let backend = if available {
        "mpv-ipc".to_string()
    } else if libmpv_linked {
        "libmpv-stub".to_string()
    } else {
        "unavailable".to_string()
    };

    let hwdec = if available {
        "auto-safe".to_string()
    } else {
        "unavailable".to_string()
    };

    let status = if !available {
        "unavailable".to_string()
    } else if let Some(st) = live_state {
        if !st.last_error.is_empty() && st.stopped {
            "error".to_string()
        } else if st.buffering {
            "buffering".to_string()
        } else if st.playing && !st.paused && !st.stopped {
            "playing".to_string()
        } else if st.initialized && !st.stopped && st.paused {
            "paused".to_string()
        } else {
            "ready".to_string()
        }
    } else {
        "ready".to_string()
    };

    let detail = if let Some(ref reason) = unavailable_reason {
        format!("mpv unavailable: {reason}")
    } else {
        let ver_str = executable_version.as_deref().unwrap_or("unknown");
        let lib_note = match (&libmpv_version, libmpv_linked) {
            (Some(v), true) => format!("libmpv {v} linked"),
            (Some(v), false) => format!("libmpv {v} detected"),
            (None, _) => "libmpv absent".to_string(),
        };
        let state_note = if initialized {
            if hwdec_active {
                format!("active, hwdec={hwdec_current}")
            } else {
                "active, hwdec idle/sw".to_string()
            }
        } else {
            "idle (not yet started)".to_string()
        };
        format!("{ver_str} JSON IPC ready ({lib_note}; target hwdec=auto-safe; {state_note})")
    };

    MpvProbeStatus {
        available,
        initialized,
        backend,
        hwdec,
        hwdec_active,
        hwdec_current,
        status,
        detail,
        executable_path: exe_path.map(|p| p.display().to_string()),
        executable_version,
        ipc_supported,
        libmpv_available,
        libmpv_version,
        libmpv_linked,
        unavailable_reason,
    }
}

fn resolve_mpv_executable(cfg: &MpvProbeConfig) -> Option<PathBuf> {
    if let Some(ref over) = cfg.mpv_bin_override {
        return is_executable_file(over).then(|| over.clone());
    }
    find_executable_in_path("mpv", cfg.path_env.as_deref())
}

fn probe_mpv_executable(bin: &Path) -> Result<(Option<String>, bool), String> {
    let ver_out = Command::new(bin)
        .arg("--version")
        .stdin(Stdio::null())
        .output()
        .map_err(|err| format!("Failed executing {} --version: {err}", bin.display()))?;
    if !ver_out.status.success() {
        return Err(format!(
            "{} --version exited with {}",
            bin.display(),
            ver_out.status
        ));
    }
    let version = String::from_utf8_lossy(&ver_out.stdout)
        .lines()
        .next()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(ToString::to_string);

    let opts_out = Command::new(bin)
        .arg("--list-options")
        .stdin(Stdio::null())
        .output()
        .map_err(|err| format!("Failed executing {} --list-options: {err}", bin.display()))?;
    if !opts_out.status.success() {
        return Err(format!(
            "{} --list-options exited with {}",
            bin.display(),
            opts_out.status
        ));
    }
    let opts_text = String::from_utf8_lossy(&opts_out.stdout);
    let has_ipc = opts_text.contains("--input-ipc-server");
    Ok((version, has_ipc))
}

fn probe_libmpv(allow_dlopen: bool) -> (bool, Option<String>, bool, Option<String>) {
    if let Some((maj, min)) = libmpv_ffi::linked_version() {
        return (true, Some(format!("{maj}.{min}")), true, None);
    }
    if !allow_dlopen {
        return (
            false,
            None,
            false,
            Some("libmpv runtime probe disabled".to_string()),
        );
    }
    #[cfg(unix)]
    {
        for soname in [c"libmpv.so.2", c"libmpv.so"] {
            // SAFETY: `soname` is a static NUL-terminated string; `RTLD_LAZY | RTLD_LOCAL` avoids symbol pollution.
            let handle = unsafe { libc::dlopen(soname.as_ptr(), libc::RTLD_LAZY | libc::RTLD_LOCAL) };
            if !handle.is_null() {
                // SAFETY: `handle` is valid; looking up standard libmpv symbol `mpv_client_api_version`.
                let sym = unsafe { libc::dlsym(handle, c"mpv_client_api_version".as_ptr()) };
                let ver = if !sym.is_null() {
                    // SAFETY: `mpv_client_api_version` has signature `extern "C" fn() -> c_ulong`.
                    let func: unsafe extern "C" fn() -> std::os::raw::c_ulong =
                        unsafe { std::mem::transmute(sym) };
                    let raw = unsafe { func() };
                    Some(format!("{}.{}", (raw >> 16) & 0xffff, raw & 0xffff))
                } else {
                    None
                };
                // SAFETY: `handle` came from `dlopen`.
                unsafe { libc::dlclose(handle) };
                return (true, ver, false, None);
            }
        }
        // SAFETY: `dlerror` returns a thread-local C string describing the last `dlopen` error.
        let err_ptr = unsafe { libc::dlerror() };
        let err_str = if err_ptr.is_null() {
            "libmpv.so.2 not found".to_string()
        } else {
            unsafe { CStr::from_ptr(err_ptr) }
                .to_string_lossy()
                .into_owned()
        };
        (false, None, false, Some(err_str))
    }
    #[cfg(not(unix))]
    {
        (false, None, false, Some("libmpv probe only supported on Unix".to_string()))
    }
}

struct MpvChild {
    child: std::process::Child,
    pid: u32,
}

pub struct MpvPlayer {
    paths: OmnideskPaths,
    probe_cfg: MpvProbeConfig,
    state: Arc<RwLock<MpvPlaybackState>>,
    ipc: Mutex<Option<Arc<MpvIpcClient>>>,
    child: Mutex<Option<MpvChild>>,
    sink: Arc<RwLock<Option<Arc<dyn EventSink>>>>,
    observer: Arc<RwLock<Option<StateObserver>>>,
    embedded: Mutex<Option<Arc<EmbeddedMpv>>>,
    embedded_active: AtomicBool,
    op_mu: Mutex<()>,
    loadfile_uses_index: AtomicBool,
}

impl MpvPlayer {
    pub fn new(paths: OmnideskPaths) -> Arc<Self> {
        Self::with_config(paths, MpvProbeConfig::default())
    }

    pub fn with_config(paths: OmnideskPaths, probe_cfg: MpvProbeConfig) -> Arc<Self> {
        let probe_snap = probe_with(&probe_cfg, None);
        let state = MpvPlaybackState {
            available: probe_snap.available,
            ipc_socket_path: Some(paths.mpv_socket_path().display().to_string()),
            ..MpvPlaybackState::default()
        };
        Arc::new(Self {
            paths,
            probe_cfg,
            state: Arc::new(RwLock::new(state)),
            ipc: Mutex::new(None),
            child: Mutex::new(None),
            sink: Arc::new(RwLock::new(None)),
            observer: Arc::new(RwLock::new(None)),
            embedded: Mutex::new(None),
            embedded_active: AtomicBool::new(false),
            op_mu: Mutex::new(()),
            loadfile_uses_index: AtomicBool::new(true),
        })
    }

    pub fn set_event_sink(&self, sink: Arc<dyn EventSink>) {
        if let Ok(mut guard) = self.sink.write() {
            *guard = Some(Arc::clone(&sink));
        }
        if let Some(embedded) = self.embedded() {
            embedded.set_event_sink(sink);
        }
    }

    pub fn set_state_observer(&self, observer: StateObserver) {
        if let Ok(mut guard) = self.observer.write() {
            *guard = Some(Arc::clone(&observer));
        }
        if let Some(embedded) = self.embedded() {
            embedded.set_state_observer(observer);
        }
    }

    pub fn probe(&self) -> MpvProbeStatus {
        if let Some(embedded) = self.embedded() {
            let st = embedded.status();
            let status = if st.last_error.is_empty() {
                if st.playing && !st.paused && !st.stopped {
                    "playing"
                } else if st.initialized && !st.stopped && st.paused {
                    "paused"
                } else {
                    "ready"
                }
            } else {
                "error"
            };
            return MpvProbeStatus {
                available: true,
                initialized: st.initialized,
                backend: "libmpv-sw".to_string(),
                hwdec: "auto-copy-safe".to_string(),
                hwdec_active: st.hwdec_active,
                hwdec_current: st.hwdec_current,
                status: status.to_string(),
                detail: format!(
                    "libmpv {} software render -> canvas (embedded)",
                    embedded.api_version()
                ),
                executable_path: None,
                executable_version: None,
                ipc_supported: false,
                libmpv_available: true,
                libmpv_version: Some(embedded.api_version()),
                libmpv_linked: true,
                unavailable_reason: None,
            };
        }
        let snap = self.status();
        probe_with(&self.probe_cfg, Some(&snap))
    }

    pub fn status(&self) -> MpvPlaybackState {
        if let Some(embedded) = self.embedded() {
            return embedded.status();
        }
        self.reconcile_dead_process();
        self.state
            .read()
            .map(|guard| guard.clone())
            .unwrap_or_default()
    }

    /// Attach directly to an existing `--input-ipc-server` socket (used by real or mock IPC tests).
    pub fn attach_socket(&self, sock_path: &Path) -> Result<MpvPlaybackState, RpcError> {
        let _ = self.disable_embedded();
        let _op = self.op_mu.lock().unwrap_or_else(|e| e.into_inner());
        if has_parent_or_nul(sock_path) {
            return Err(RpcError::path_safety("Unsafe mpv IPC socket path"));
        }
        self.connect_ipc_locked(sock_path)?;
        Ok(self.status())
    }

    pub fn open(&self, req: MediaOpenRequest) -> Result<MpvPlaybackState, RpcError> {
        validate_media_open_request(&req)?;
        if let Some(embedded) = self.embedded() {
            return embedded.open(req);
        }
        let _op = self.op_mu.lock().unwrap_or_else(|e| e.into_inner());
        let client = self.ensure_connected_locked()?;

        // Reset previous track metrics BEFORE sending `loadfile` so fast IPC property events
        // arriving immediately after `loadfile` are never clobbered.
        let redacted_url = redact_media_url(&req.url);
        let redacted_cover = req.cover_url.as_deref().map(redact_media_url).unwrap_or_default();
        let start_pos = req.start_seconds.unwrap_or(0.0);
        let item_id = req.item_id.clone().unwrap_or_default();
        let title = req.title.clone().unwrap_or_default();
        let artist = req.artist.clone().unwrap_or_default();
        let album = req.album.clone().unwrap_or_default();
        {
            let mut guard = self.state.write().unwrap_or_else(|e| e.into_inner());
            guard.available = true;
            guard.initialized = true;
            guard.playing = false;
            guard.stopped = true;
            guard.buffering = false;
            guard.last_error.clear();
            guard.position_sec = start_pos;
            guard.duration_sec = 0.0;
            guard.video_width = 0;
            guard.video_height = 0;
            guard.fps = 0.0;
            guard.video_bitrate = 0.0;
            guard.video_codec.clear();
            guard.audio_codec.clear();
            guard.audio_samplerate = 0;
            guard.audio_channels = 0;
            guard.hwdec_current.clear();
            guard.hwdec_active = false;
            guard.current_url = redacted_url;
            guard.item_id = item_id;
            guard.title = title;
            guard.artist = artist;
            guard.album = album;
            guard.cover_url = redacted_cover;
        }

        let opts_str = build_loadfile_options(&req);
        let url_trimmed = req.url.trim().to_string();
        let cmd = if self.loadfile_uses_index.load(Ordering::Acquire) {
            json!(["loadfile", url_trimmed, "replace", 0, opts_str])
        } else {
            json!(["loadfile", url_trimmed, "replace", opts_str])
        };

        if let Err(err) = client.command(cmd, MPV_COMMAND_TIMEOUT) {
            self.record_error_and_emit(&err.message);
            return Err(err);
        }

        let desired_pause = req.paused.unwrap_or(false);
        if let Err(err) = client.command(json!(["set_property", "pause", desired_pause]), MPV_COMMAND_TIMEOUT) {
            self.record_error_and_emit(&err.message);
            return Err(err);
        }

        let snap = {
            let mut guard = self.state.write().unwrap_or_else(|e| e.into_inner());
            guard.available = true;
            guard.initialized = true;
            guard.stopped = false;
            guard.paused = desired_pause;
            guard.playing = !desired_pause;
            guard.clone()
        };
        emit_state_snapshot(&snap, &self.sink, &self.observer);
        Ok(snap)
    }

    pub fn pause(&self) -> Result<MpvPlaybackState, RpcError> {
        if let Some(embedded) = self.embedded() {
            return embedded.pause();
        }
        let client = self.active_client()?;
        client.command(json!(["set_property", "pause", true]), MPV_COMMAND_TIMEOUT)?;
        let snap = {
            let mut guard = self.state.write().unwrap_or_else(|e| e.into_inner());
            guard.paused = true;
            guard.playing = false;
            guard.clone()
        };
        emit_state_snapshot(&snap, &self.sink, &self.observer);
        Ok(snap)
    }

    pub fn resume(&self) -> Result<MpvPlaybackState, RpcError> {
        if let Some(embedded) = self.embedded() {
            return embedded.resume();
        }
        let client = self.active_client()?;
        client.command(json!(["set_property", "pause", false]), MPV_COMMAND_TIMEOUT)?;
        let snap = {
            let mut guard = self.state.write().unwrap_or_else(|e| e.into_inner());
            guard.paused = false;
            if !guard.stopped {
                guard.playing = true;
            }
            guard.clone()
        };
        emit_state_snapshot(&snap, &self.sink, &self.observer);
        Ok(snap)
    }

    pub fn toggle_pause(&self) -> Result<MpvPlaybackState, RpcError> {
        if let Some(embedded) = self.embedded() {
            return embedded.toggle_pause();
        }
        let client = self.active_client()?;
        client.command(json!(["cycle", "pause"]), MPV_COMMAND_TIMEOUT)?;
        let paused_val = client.command(json!(["get_property", "pause"]), MPV_COMMAND_TIMEOUT)?;
        let is_paused = paused_val.as_bool().unwrap_or(true);
        let snap = {
            let mut guard = self.state.write().unwrap_or_else(|e| e.into_inner());
            guard.paused = is_paused;
            if !guard.stopped {
                guard.playing = !is_paused;
            }
            guard.clone()
        };
        emit_state_snapshot(&snap, &self.sink, &self.observer);
        Ok(snap)
    }

    pub fn stop_playback(&self) -> Result<MpvPlaybackState, RpcError> {
        if let Some(embedded) = self.embedded() {
            return embedded.stop();
        }
        let client = self.active_client()?;
        client.command(json!(["stop"]), MPV_COMMAND_TIMEOUT)?;
        let snap = {
            let mut guard = self.state.write().unwrap_or_else(|e| e.into_inner());
            guard.playing = false;
            guard.paused = true;
            guard.stopped = true;
            guard.position_sec = 0.0;
            guard.clone()
        };
        emit_state_snapshot(&snap, &self.sink, &self.observer);
        Ok(snap)
    }

    pub fn seek(&self, seconds: f64, relative: bool) -> Result<MpvPlaybackState, RpcError> {
        if !seconds.is_finite() {
            return Err(RpcError::invalid_argument(
                "seek 'seconds' must be a finite number",
            ));
        }
        if let Some(embedded) = self.embedded() {
            return embedded.seek(seconds, relative);
        }
        let target = if relative { seconds } else { seconds.max(0.0) };
        let mode = if relative { "relative" } else { "absolute" };
        let client = self.active_client()?;
        client.command(json!(["seek", target, mode]), MPV_COMMAND_TIMEOUT)?;

        let queried_pos = client
            .command(json!(["get_property", "playback-time"]), MPV_COMMAND_TIMEOUT)
            .ok()
            .and_then(|v| v.as_f64())
            .filter(|v| v.is_finite() && *v >= 0.0);

        let snap = {
            let mut guard = self.state.write().unwrap_or_else(|e| e.into_inner());
            if let Some(pos) = queried_pos {
                guard.position_sec = pos;
            } else if !relative {
                guard.position_sec = target;
            }
            guard.clone()
        };
        emit_state_snapshot(&snap, &self.sink, &self.observer);
        Ok(snap)
    }

    pub fn set_volume(&self, volume: i64) -> Result<MpvPlaybackState, RpcError> {
        let clamped = volume.clamp(0, 100);
        if let Some(embedded) = self.embedded() {
            return embedded.set_volume(clamped);
        }
        let client = self.active_client()?;
        client.command(json!(["set_property", "volume", clamped]), MPV_COMMAND_TIMEOUT)?;
        let snap = {
            let mut guard = self.state.write().unwrap_or_else(|e| e.into_inner());
            guard.volume = clamped;
            guard.clone()
        };
        emit_state_snapshot(&snap, &self.sink, &self.observer);
        Ok(snap)
    }

    pub fn set_speed(&self, speed: f64) -> Result<MpvPlaybackState, RpcError> {
        if !speed.is_finite() || speed <= 0.0 {
            return Err(RpcError::invalid_argument(
                "speed must be a positive finite number",
            ));
        }
        if let Some(embedded) = self.embedded() {
            return embedded.set_speed(speed);
        }
        let clamped = speed.clamp(0.25, 4.0);
        let client = self.active_client()?;
        client.command(json!(["set_property", "speed", clamped]), MPV_COMMAND_TIMEOUT)?;
        let snap = {
            let mut guard = self.state.write().unwrap_or_else(|e| e.into_inner());
            guard.speed = clamped;
            guard.clone()
        };
        emit_state_snapshot(&snap, &self.sink, &self.observer);
        Ok(snap)
    }

    pub fn shutdown(&self) {
        let embedded = self
            .embedded
            .lock()
            .ok()
            .and_then(|mut guard| guard.take());
        self.embedded_active.store(false, Ordering::Release);
        if let Some(embedded) = embedded {
            embedded.shutdown();
        }
        self.shutdown_external();
    }

    fn shutdown_external(&self) {
        let _op = self.op_mu.lock().unwrap_or_else(|e| e.into_inner());
        let client_opt = self.ipc.lock().ok().and_then(|mut g| g.take());
        if let Some(client) = client_opt {
            let _ = client.command(json!(["quit"]), Duration::from_millis(250));
            client.close();
        }
        let child_opt = self.child.lock().ok().and_then(|mut g| g.take());
        if let Some(mut mc) = child_opt {
            terminate_child_process_group(&mut mc.child, mc.pid, MPV_SHUTDOWN_GRACE);
        }
        let sock = self.paths.mpv_socket_path();
        if let Ok(meta) = fs::symlink_metadata(&sock) {
            if !meta.file_type().is_symlink() {
                let _ = fs::remove_file(&sock);
            }
        }
        if let Ok(mut guard) = self.state.write() {
            guard.initialized = false;
            guard.playing = false;
            guard.stopped = true;
            guard.buffering = false;
            guard.hwdec_active = false;
            guard.hwdec_current.clear();
        }
    }

    fn active_client(&self) -> Result<Arc<MpvIpcClient>, RpcError> {
        self.reconcile_dead_process();
        let guard = self.ipc.lock().unwrap_or_else(|e| e.into_inner());
        match guard.as_ref() {
            Some(c) if !c.is_closed() => Ok(Arc::clone(c)),
            _ => Err(RpcError::service_offline(
                "mpv player is not initialized; call mpv_open first",
            )),
        }
    }

    fn embedded(&self) -> Option<Arc<EmbeddedMpv>> {
        if !self.embedded_active.load(Ordering::Acquire) {
            return None;
        }
        self.embedded
            .lock()
            .ok()
            .and_then(|guard| guard.as_ref().map(Arc::clone))
    }

    /// Switch from the external `mpv --input-ipc-server` process to an
    /// in-process libmpv renderer. All playback commands and MPRIS controls are
    /// routed to the embedded instance while it is active.
    pub fn enable_embedded(&self) -> Result<MpvPlaybackState, RpcError> {
        if let Some(embedded) = self.embedded() {
            return Ok(embedded.status());
        }
        let embedded = EmbeddedMpv::new()?;
        self.shutdown_external();
        let sink = self
            .sink
            .read()
            .ok()
            .and_then(|guard| guard.as_ref().map(Arc::clone));
        if let Some(sink) = sink {
            embedded.set_event_sink(sink);
        }
        let observer = self
            .observer
            .read()
            .ok()
            .and_then(|guard| guard.as_ref().map(Arc::clone));
        if let Some(observer) = observer {
            embedded.set_state_observer(observer);
        }
        let snapshot = embedded.status();
        if let Ok(mut guard) = self.embedded.lock() {
            *guard = Some(embedded);
        }
        self.embedded_active.store(true, Ordering::Release);
        Ok(snapshot)
    }

    pub fn disable_embedded(&self) -> Result<MpvPlaybackState, RpcError> {
        self.embedded_active.store(false, Ordering::Release);
        let embedded = self
            .embedded
            .lock()
            .ok()
            .and_then(|mut guard| guard.take());
        if let Some(embedded) = embedded {
            embedded.shutdown();
        }
        self.reconcile_dead_process();
        Ok(self
            .state
            .read()
            .map(|guard| guard.clone())
            .unwrap_or_default())
    }

    pub fn set_embedded_viewport(&self, width: u32, height: u32, fps: u32) -> Result<(), RpcError> {
        let embedded = self.embedded().ok_or_else(|| {
            RpcError::service_offline("embedded libmpv renderer is not enabled")
        })?;
        embedded.set_viewport(width, height, fps);
        Ok(())
    }

    pub fn set_embedded_frames_active(&self, active: bool) -> Result<(), RpcError> {
        let embedded = self.embedded().ok_or_else(|| {
            RpcError::service_offline("embedded libmpv renderer is not enabled")
        })?;
        embedded.set_frames_enabled(active);
        Ok(())
    }

    fn reconcile_dead_process(&self) {
        let ipc_closed = self
            .ipc
            .lock()
            .ok()
            .and_then(|g| g.as_ref().map(|c| c.is_closed()))
            .unwrap_or(true);
        let child_dead = self
            .child
            .lock()
            .ok()
            .and_then(|mut g| {
                g.as_mut()
                    .and_then(|mc| mc.child.try_wait().ok().flatten())
                    .map(|_| true)
            })
            .unwrap_or(false);

        if ipc_closed || child_dead {
            if child_dead {
                if let Ok(mut g) = self.child.lock() {
                    *g = None;
                }
            }
            if ipc_closed {
                if let Ok(mut g) = self.ipc.lock() {
                    *g = None;
                }
            }
            if let Ok(mut guard) = self.state.write() {
                if guard.initialized && (child_dead || ipc_closed) {
                    guard.initialized = false;
                    guard.playing = false;
                    guard.stopped = true;
                    guard.hwdec_active = false;
                    guard.hwdec_current.clear();
                }
            }
        }
    }

    fn ensure_connected_locked(&self) -> Result<Arc<MpvIpcClient>, RpcError> {
        self.reconcile_dead_process();
        if let Ok(guard) = self.ipc.lock() {
            if let Some(ref existing) = *guard {
                if !existing.is_closed() {
                    return Ok(Arc::clone(existing));
                }
            }
        }

        let probe_res = probe_with(&self.probe_cfg, None);
        if !probe_res.available {
            let reason = probe_res
                .unavailable_reason
                .unwrap_or_else(|| "mpv IPC backend unavailable".to_string());
            self.record_error_and_emit(&reason);
            return Err(RpcError::service_offline(reason));
        }

        let exe_path = probe_res
            .executable_path
            .ok_or_else(|| RpcError::service_offline("mpv executable path unavailable"))?;
        let sock_path = self.paths.mpv_socket_path();
        let sock_parent = sock_path
            .parent()
            .ok_or_else(|| RpcError::path_safety("mpv socket path has no parent directory"))?;
        ensure_private_dir(sock_parent, "mpv runtime directory")?;

        if let Ok(meta) = fs::symlink_metadata(&sock_path) {
            if meta.file_type().is_symlink() {
                return Err(RpcError::path_safety(format!(
                    "Refusing symlinked mpv socket: {}",
                    sock_path.display()
                )));
            }
            let _ = fs::remove_file(&sock_path);
        }

        let mut args = vec![
            "--no-config".to_string(),
            "--idle=yes".to_string(),
            "--keep-open=yes".to_string(),
            "--hwdec=auto-safe".to_string(),
            "--hr-seek=yes".to_string(),
            "--ytdl=no".to_string(),
            "--cache=yes".to_string(),
            "--demuxer-max-bytes=150MiB".to_string(),
            "--demuxer-max-back-bytes=64MiB".to_string(),
            "--audio-channels=stereo".to_string(),
            "--reset-on-next-file=speed,pause,http-header-fields,referrer,user-agent,stream-lavf-o,force-seekable".to_string(),
            "--terminal=no".to_string(),
            "--force-window=no".to_string(),
            format!("--input-ipc-server={}", sock_path.display()),
        ];
        args.extend(self.probe_cfg.extra_spawn_args.iter().cloned());

        let mut cmd = Command::new(&exe_path);
        cmd.args(&args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(unix)]
        {
            // SAFETY: `setpgid(0, 0)` runs in the child between fork and exec.
            unsafe {
                cmd.pre_exec(|| {
                    let _ = libc::setpgid(0, 0);
                    Ok(())
                });
            }
        }

        let child = cmd.spawn().map_err(|err| {
            RpcError::service_offline(redact_text(&format!("Failed spawning mpv process: {err}")))
        })?;
        let pid = child.id();
        let mut mc = MpvChild { child, pid };

        let deadline = Instant::now() + MPV_CONNECT_TIMEOUT;
        loop {
            match mc.child.try_wait() {
                Ok(Some(status)) => {
                    let msg = format!("mpv exited before opening IPC socket ({status})");
                    self.record_error_and_emit(&msg);
                    return Err(RpcError::service_offline(msg));
                }
                Ok(None) => {}
                Err(err) => {
                    terminate_child_process_group(&mut mc.child, mc.pid, MPV_SHUTDOWN_GRACE);
                    return Err(RpcError::service_offline(format!(
                        "Failed polling mpv process: {err}"
                    )));
                }
            }
            if sock_path.exists() {
                if let Ok(client) = self.connect_ipc_locked(&sock_path) {
                    if let Ok(mut g) = self.child.lock() {
                        *g = Some(mc);
                    }
                    return Ok(client);
                }
            }
            if Instant::now() >= deadline {
                terminate_child_process_group(&mut mc.child, mc.pid, MPV_SHUTDOWN_GRACE);
                let msg = format!(
                    "Timed out waiting for mpv IPC socket at {}",
                    sock_path.display()
                );
                self.record_error_and_emit(&msg);
                return Err(RpcError::timeout(msg));
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    }

    fn connect_ipc_locked(&self, sock_path: &Path) -> Result<Arc<MpvIpcClient>, RpcError> {
        let client = Arc::new(MpvIpcClient::connect(
            sock_path,
            Arc::clone(&self.state),
            Arc::clone(&self.sink),
            Arc::clone(&self.observer),
        )?);

        // Detect whether `loadfile` expects 4 arguments (`index` added in mpv 0.38 / API 2.3).
        let uses_index = match client.command(json!(["get_property", "command-list"]), MPV_COMMAND_TIMEOUT) {
            Ok(Value::Array(cmds)) => cmds
                .iter()
                .find(|c| c.get("name").and_then(Value::as_str) == Some("loadfile"))
                .and_then(|c| c.get("args"))
                .and_then(Value::as_array)
                .map(|args| args.iter().any(|a| a.get("name").and_then(Value::as_str) == Some("index")))
                .unwrap_or(true),
            _ => true,
        };
        self.loadfile_uses_index.store(uses_index, Ordering::Release);

        let cur_vol = self.state.read().map(|g| g.volume).unwrap_or(85);
        let _ = client.command(json!(["set_property", "volume", cur_vol]), MPV_COMMAND_TIMEOUT);

        if let Ok(mut guard) = self.state.write() {
            guard.available = true;
            guard.initialized = true;
            guard.ipc_socket_path = Some(sock_path.display().to_string());
            guard.last_error.clear();
        }
        if let Ok(mut ipc_guard) = self.ipc.lock() {
            *ipc_guard = Some(Arc::clone(&client));
        }
        Ok(client)
    }

    fn record_error_and_emit(&self, err_msg: &str) {
        let safe = redact_text(err_msg);
        let snap = {
            let mut guard = self.state.write().unwrap_or_else(|e| e.into_inner());
            guard.last_error = safe;
            guard.clone()
        };
        emit_state_snapshot(&snap, &self.sink, &self.observer);
    }
}

impl Drop for MpvPlayer {
    fn drop(&mut self) {
        self.shutdown();
    }
}
