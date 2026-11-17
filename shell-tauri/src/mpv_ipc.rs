//! mpv `--input-ipc-server` JSON IPC protocol, per-stream option isolation, and state reducer.

use std::collections::{BTreeMap, HashMap};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex, RwLock};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::events::EventSink;
use crate::paths::has_parent_or_nul;
use crate::protocol::RpcError;
use crate::redact::redact_text;

pub const MPV_STATE_CHANNEL: &str = "omnidesk:mpv-state";
const MAX_IPC_LINE_BYTES: usize = 2 * 1024 * 1024;
const DEFAULT_IPC_TIMEOUT: Duration = Duration::from_millis(3_000);
const POSITION_EMIT_THROTTLE: Duration = Duration::from_millis(250);

pub const OBSERVED_PROPERTIES: &[&str] = &[
    "playback-time",
    "duration",
    "pause",
    "playback-abort",
    "paused-for-cache",
    "speed",
    "volume",
    "video-params/w",
    "video-params/h",
    "hwdec-current",
    "estimated-vf-fps",
    "video-bitrate",
    "decoder-frame-drop-count",
    "frame-drop-count",
    "video-codec",
    "audio-codec",
    "audio-params/samplerate",
    "audio-params/channel-count",
    "demuxer-cache-duration",
    "eof-reached",
    "media-title",
    "idle-active",
];

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct MediaOpenRequest {
    pub url: String,
    #[serde(default, alias = "audio_url")]
    pub audio_url: Option<String>,
    #[serde(default, alias = "subtitle_urls")]
    pub subtitle_urls: Vec<String>,
    #[serde(default, alias = "http_headers")]
    pub http_headers: BTreeMap<String, String>,
    #[serde(default)]
    pub referrer: Option<String>,
    #[serde(default, alias = "user_agent")]
    pub user_agent: Option<String>,
    #[serde(default, alias = "start_seconds")]
    pub start_seconds: Option<f64>,
    #[serde(default, alias = "end_seconds")]
    pub end_seconds: Option<f64>,
    #[serde(default)]
    pub hwdec: Option<bool>,
    #[serde(default)]
    pub paused: Option<bool>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub artist: Option<String>,
    #[serde(default)]
    pub album: Option<String>,
    #[serde(default, alias = "cover_url")]
    pub cover_url: Option<String>,
    #[serde(default, alias = "item_id")]
    pub item_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct MpvPlaybackState {
    pub available: bool,
    pub initialized: bool,
    pub playing: bool,
    pub paused: bool,
    pub stopped: bool,
    pub buffering: bool,
    pub position_sec: f64,
    pub duration_sec: f64,
    pub speed: f64,
    pub volume: i64,
    pub video_width: i64,
    pub video_height: i64,
    pub fps: f64,
    pub video_bitrate: f64,
    pub decoder_drop_frames: i64,
    pub output_drop_frames: i64,
    pub video_codec: String,
    pub audio_codec: String,
    pub audio_samplerate: i64,
    pub audio_channels: i64,
    pub cache_duration_sec: f64,
    pub hwdec_current: String,
    pub hwdec_active: bool,
    pub last_error: String,
    pub current_url: String,
    pub item_id: String,
    pub title: String,
    pub artist: String,
    pub album: String,
    pub cover_url: String,
    pub ipc_socket_path: Option<String>,
}

impl Default for MpvPlaybackState {
    fn default() -> Self {
        Self {
            available: false,
            initialized: false,
            playing: false,
            paused: true,
            stopped: true,
            buffering: false,
            position_sec: 0.0,
            duration_sec: 0.0,
            speed: 1.0,
            volume: 85,
            video_width: 0,
            video_height: 0,
            fps: 0.0,
            video_bitrate: 0.0,
            decoder_drop_frames: 0,
            output_drop_frames: 0,
            video_codec: String::new(),
            audio_codec: String::new(),
            audio_samplerate: 0,
            audio_channels: 0,
            cache_duration_sec: 0.0,
            hwdec_current: String::new(),
            hwdec_active: false,
            last_error: String::new(),
            current_url: String::new(),
            item_id: String::new(),
            title: String::new(),
            artist: String::new(),
            album: String::new(),
            cover_url: String::new(),
            ipc_socket_path: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct MpvIpcReply {
    pub request_id: u64,
    pub error: String,
    pub data: Value,
}

#[derive(Debug, Clone, PartialEq)]
pub enum MpvIpcMessage {
    Reply(MpvIpcReply),
    PropertyChange {
        name: String,
        data: Value,
    },
    EndFile {
        reason: String,
        file_error: Option<String>,
    },
    PlaybackRestart,
    StartFile,
    Idle,
    OtherEvent(String),
}

pub fn validate_media_open_request(req: &MediaOpenRequest) -> Result<(), RpcError> {
    validate_media_target("url", &req.url)?;
    if let Some(ref audio) = req.audio_url {
        if !audio.trim().is_empty() {
            validate_media_target("audio_url", audio)?;
        }
    }
    for sub in &req.subtitle_urls {
        if !sub.trim().is_empty() {
            validate_media_target("subtitle_urls", sub)?;
        }
    }
    for field in [
        req.referrer.as_deref(),
        req.user_agent.as_deref(),
        req.title.as_deref(),
        req.artist.as_deref(),
        req.album.as_deref(),
        req.cover_url.as_deref(),
        req.item_id.as_deref(),
    ]
    .into_iter()
    .flatten()
    {
        if field.contains('\0') || field.contains('\r') || field.contains('\n') {
            return Err(RpcError::invalid_argument(
                "Media metadata/headers must not contain NUL or newline characters",
            ));
        }
    }
    for (k, v) in &req.http_headers {
        if k.trim().is_empty()
            || k.contains('\0')
            || k.contains('\r')
            || k.contains('\n')
            || v.contains('\0')
            || v.contains('\r')
            || v.contains('\n')
        {
            return Err(RpcError::invalid_argument(
                "http_headers keys/values must be non-empty and free of NUL/newlines",
            ));
        }
    }
    if let Some(start) = req.start_seconds {
        if !start.is_finite() || start < 0.0 {
            return Err(RpcError::invalid_argument(
                "start_seconds must be a non-negative finite number",
            ));
        }
    }
    if let Some(end) = req.end_seconds {
        if !end.is_finite() || end < 0.0 {
            return Err(RpcError::invalid_argument(
                "end_seconds must be a non-negative finite number",
            ));
        }
    }
    Ok(())
}

fn validate_media_target(field: &str, raw: &str) -> Result<(), RpcError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(RpcError::invalid_argument(format!(
            "'{field}' must not be empty"
        )));
    }
    if trimmed.contains('\0') || trimmed.contains('\r') || trimmed.contains('\n') {
        return Err(RpcError::invalid_argument(format!(
            "'{field}' must not contain NUL or newline characters"
        )));
    }
    let lower = trimmed.to_ascii_lowercase();
    if lower.starts_with("javascript:")
        || lower.starts_with("vbscript:")
        || lower.starts_with("data:")
    {
        return Err(RpcError::invalid_argument(format!(
            "Disallowed URI scheme in '{field}'"
        )));
    }
    if let Some(local) = lower.strip_prefix("file://") {
        if has_parent_or_nul(Path::new(local)) {
            return Err(RpcError::path_safety(format!(
                "Refusing path traversal in '{field}'"
            )));
        }
        return Ok(());
    }
    if !trimmed.contains("://") && has_parent_or_nul(Path::new(trimmed)) {
        return Err(RpcError::path_safety(format!(
            "Refusing path traversal in '{field}'"
        )));
    }
    Ok(())
}

fn strip_newlines(s: &str) -> String {
    s.chars().filter(|c| *c != '\r' && *c != '\n' && *c != '\0').collect()
}

fn escape_mpv_quoted(raw: &str) -> String {
    let clean = strip_newlines(raw);
    let mut out = String::with_capacity(clean.len() + 4);
    for c in clean.chars() {
        if c == '\\' || c == '"' {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

fn escape_mpv_list_item(raw: &str) -> String {
    let clean = strip_newlines(raw);
    let mut out = String::with_capacity(clean.len() + 4);
    for c in clean.chars() {
        if c == '\\' || c == '"' || c == ',' {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

fn is_loopback_url(u: &str) -> bool {
    const PREFIXES: &[&str] = &[
        "https://127.0.0.1:",
        "http://127.0.0.1:",
        "https://localhost:",
        "http://localhost:",
        "https://[::1]:",
        "http://[::1]:",
    ];
    PREFIXES.iter().any(|p| u.starts_with(p))
}

/// Build per-file isolated mpv `loadfile` options matching v1 `MPVCore::buildLoadfileOptions`.
pub fn build_loadfile_options(opts: &MediaOpenRequest) -> String {
    let mut referrer = opts.referrer.clone().unwrap_or_default();
    let mut user_agent = opts.user_agent.clone().unwrap_or_default();
    let mut filtered_headers: BTreeMap<String, String> = BTreeMap::new();

    for (k, v) in &opts.http_headers {
        let trimmed_k = k.trim();
        if trimmed_k.is_empty() {
            continue;
        }
        if trimmed_k.eq_ignore_ascii_case("referer") || trimmed_k.eq_ignore_ascii_case("referrer") {
            if referrer.is_empty() {
                referrer = v.clone();
            }
        } else if trimmed_k.eq_ignore_ascii_case("user-agent") {
            if user_agent.is_empty() {
                user_agent = v.clone();
            }
        } else {
            filtered_headers.insert(strip_newlines(trimmed_k), strip_newlines(v));
        }
    }

    let mut parts = Vec::new();
    parts.push(format!("referrer=\"{}\"", escape_mpv_quoted(&referrer)));
    if user_agent.is_empty() {
        parts.push("user-agent=\"OmniDesk/2.0\"".to_string());
    } else {
        parts.push(format!("user-agent=\"{}\"", escape_mpv_quoted(&user_agent)));
    }

    if filtered_headers.is_empty() {
        parts.push("http-header-fields=\"\"".to_string());
    } else {
        let joined = filtered_headers
            .iter()
            .map(|(hk, hv)| escape_mpv_list_item(&format!("{hk}: {hv}")))
            .collect::<Vec<_>>()
            .join(",");
        parts.push(format!(
            "http-header-fields=\"{}\"",
            escape_mpv_quoted(&joined)
        ));
    }

    let audio_url = opts.audio_url.as_deref().unwrap_or("");
    let loopback = is_loopback_url(&opts.url) || is_loopback_url(audio_url);
    if loopback {
        parts.push("network-timeout=30".to_string());
        parts.push("tls-verify=no".to_string());
        parts.push("http-proxy=\"\"".to_string());
        if opts.url.contains("/api/v1/playback/") || audio_url.contains("/api/v1/playback/") {
            parts.push("stream-lavf-o=\"seekable=0\"".to_string());
            parts.push("force-seekable=yes".to_string());
        }
    } else {
        parts.push("network-timeout=10".to_string());
    }

    let hwdec = opts.hwdec.unwrap_or(true);
    parts.push(if hwdec {
        "hwdec=\"auto-safe\"".to_string()
    } else {
        "hwdec=\"no\"".to_string()
    });
    if !hwdec && audio_url.is_empty() {
        parts.push("vid=no".to_string());
        parts.push("audio-display=no".to_string());
    }

    if let Some(start) = opts.start_seconds.filter(|s| s.is_finite() && *s > 0.0) {
        parts.push(format!("start={start}"));
    }
    if let Some(end) = opts.end_seconds.filter(|e| e.is_finite() && *e > 0.0) {
        parts.push(format!("end={end}"));
    }
    if !audio_url.is_empty() {
        parts.push(format!("audio-file=\"{}\"", escape_mpv_quoted(audio_url)));
    }
    let clean_subs: Vec<String> = opts
        .subtitle_urls
        .iter()
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(escape_mpv_list_item)
        .collect();
    if !clean_subs.is_empty() {
        parts.push(format!(
            "sub-files=\"{}\"",
            escape_mpv_quoted(&clean_subs.join(","))
        ));
    }

    parts.join(",")
}

/// Scrub sensitive query parameters, userinfo, and token strings from media URLs before logs/events.
pub fn redact_media_url(url: &str) -> String {
    let redacted = redact_text(url);
    if let Some(scheme) = redacted.find("://") {
        if let Some(rel_q) = redacted[scheme..].find('?') {
            let q_idx = scheme + rel_q;
            return format!("{}?<redacted>", &redacted[..q_idx]);
        }
    }
    redacted
}

pub fn encode_ipc_command(command: Value, request_id: u64) -> Result<Vec<u8>, RpcError> {
    let mut payload = serde_json::to_vec(&json!({
        "command": command,
        "request_id": request_id,
    }))
    .map_err(|err| RpcError::invalid_argument(format!("Failed serializing mpv IPC command: {err}")))?;
    payload.push(b'\n');
    Ok(payload)
}

pub fn parse_ipc_message(line: &str) -> Result<MpvIpcMessage, RpcError> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return Err(RpcError::internal_error("Empty mpv IPC frame"));
    }
    let val: Value = serde_json::from_str(trimmed)
        .map_err(|err| RpcError::internal_error(format!("Malformed mpv IPC JSON: {err}")))?;
    let obj = val
        .as_object()
        .ok_or_else(|| RpcError::internal_error("mpv IPC message must be a JSON object"))?;

    if let Some(req_id) = obj.get("request_id").and_then(Value::as_u64) {
        let error = obj
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string();
        let data = obj.get("data").cloned().unwrap_or(Value::Null);
        return Ok(MpvIpcMessage::Reply(MpvIpcReply {
            request_id: req_id,
            error,
            data,
        }));
    }

    if let Some(ev) = obj.get("event").and_then(Value::as_str) {
        return Ok(match ev {
            "property-change" => MpvIpcMessage::PropertyChange {
                name: obj
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                data: obj.get("data").cloned().unwrap_or(Value::Null),
            },
            "end-file" => MpvIpcMessage::EndFile {
                reason: obj
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown")
                    .to_string(),
                file_error: obj
                    .get("file_error")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .map(redact_text),
            },
            "playback-restart" => MpvIpcMessage::PlaybackRestart,
            "start-file" => MpvIpcMessage::StartFile,
            "idle" => MpvIpcMessage::Idle,
            other => MpvIpcMessage::OtherEvent(other.to_string()),
        });
    }

    Err(RpcError::internal_error(
        "mpv IPC JSON object missing 'request_id' or 'event'",
    ))
}

/// Apply an incoming mpv IPC message onto `MpvPlaybackState`. Returns `(changed, is_time_only)`.
pub fn apply_ipc_message(state: &mut MpvPlaybackState, msg: &MpvIpcMessage) -> (bool, bool) {
    match msg {
        MpvIpcMessage::Reply(_) | MpvIpcMessage::OtherEvent(_) => (false, false),
        MpvIpcMessage::StartFile => {
            state.stopped = false;
            state.last_error.clear();
            (true, false)
        }
        MpvIpcMessage::PlaybackRestart => {
            state.last_error.clear();
            state.stopped = false;
            state.playing = !state.paused;
            (true, false)
        }
        MpvIpcMessage::Idle => {
            let changed = state.playing || !state.stopped;
            state.playing = false;
            state.paused = true;
            state.stopped = true;
            state.buffering = false;
            (changed, false)
        }
        MpvIpcMessage::EndFile { reason, file_error } => {
            state.playing = false;
            state.stopped = true;
            state.buffering = false;
            if reason == "error" {
                state.last_error = file_error
                    .clone()
                    .unwrap_or_else(|| "Playback failed (mpv end-file error)".to_string());
            }
            (true, false)
        }
        MpvIpcMessage::PropertyChange { name, data } => match name.as_str() {
            "playback-time" => {
                if let Some(t) = data.as_f64().filter(|v| v.is_finite() && *v >= 0.0) {
                    let diff = (state.position_sec - t).abs();
                    state.position_sec = t;
                    (diff >= 0.01, true)
                } else {
                    (false, true)
                }
            }
            "duration" => {
                if let Some(d) = data.as_f64().filter(|v| v.is_finite() && *v >= 0.0) {
                    state.duration_sec = d;
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "pause" => {
                if let Some(p) = data.as_bool() {
                    state.paused = p;
                    if !state.stopped {
                        state.playing = !p;
                    }
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "playback-abort" | "idle-active" => {
                if data.as_bool() == Some(true) {
                    let changed = state.playing || !state.stopped;
                    state.playing = false;
                    state.stopped = true;
                    (changed, false)
                } else {
                    (false, false)
                }
            }
            "paused-for-cache" => {
                if let Some(b) = data.as_bool() {
                    state.buffering = b;
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "speed" => {
                if let Some(s) = data.as_f64().filter(|v| v.is_finite() && *v > 0.0) {
                    state.speed = s;
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "volume" => {
                if let Some(v) = data.as_f64().filter(|v| v.is_finite()) {
                    state.volume = v.round().clamp(0.0, 100.0) as i64;
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "video-params/w" => {
                if let Some(w) = data.as_i64() {
                    state.video_width = w.max(0);
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "video-params/h" => {
                if let Some(h) = data.as_i64() {
                    state.video_height = h.max(0);
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "hwdec-current" => {
                let val = data.as_str().unwrap_or("").trim();
                if val.is_empty() || val.eq_ignore_ascii_case("no") {
                    state.hwdec_current.clear();
                    state.hwdec_active = false;
                } else {
                    state.hwdec_current = val.to_string();
                    state.hwdec_active = true;
                }
                (true, false)
            }
            "estimated-vf-fps" => {
                if let Some(fps) = data.as_f64().filter(|v| v.is_finite() && *v >= 0.0) {
                    state.fps = fps;
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "video-bitrate" => {
                if let Some(br) = data.as_f64().filter(|v| v.is_finite() && *v >= 0.0) {
                    state.video_bitrate = br;
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "decoder-frame-drop-count" => {
                if let Some(c) = data.as_i64() {
                    state.decoder_drop_frames = c.max(0);
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "frame-drop-count" => {
                if let Some(c) = data.as_i64() {
                    state.output_drop_frames = c.max(0);
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "video-codec" => {
                state.video_codec = data.as_str().unwrap_or("").to_string();
                (true, false)
            }
            "audio-codec" => {
                state.audio_codec = data.as_str().unwrap_or("").to_string();
                (true, false)
            }
            "audio-params/samplerate" => {
                if let Some(sr) = data.as_i64() {
                    state.audio_samplerate = sr.max(0);
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "audio-params/channel-count" => {
                if let Some(ch) = data.as_i64() {
                    state.audio_channels = ch.max(0);
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "demuxer-cache-duration" => {
                if let Some(cd) = data.as_f64().filter(|v| v.is_finite() && *v >= 0.0) {
                    state.cache_duration_sec = cd;
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "eof-reached" => {
                if data.as_bool() == Some(true) {
                    state.playing = false;
                    (true, false)
                } else {
                    (false, false)
                }
            }
            "media-title" => {
                if let Some(t) = data.as_str().filter(|s| !s.trim().is_empty()) {
                    if state.title.is_empty() {
                        state.title = redact_text(t);
                        return (true, false);
                    }
                }
                (false, false)
            }
            _ => (false, false),
        },
    }
}

pub type StateObserver = Arc<dyn Fn(&MpvPlaybackState) + Send + Sync>;

pub struct MpvIpcClient {
    writer: Mutex<UnixStream>,
    next_request_id: AtomicU64,
    pending: Arc<Mutex<HashMap<u64, mpsc::SyncSender<Result<MpvIpcReply, RpcError>>>>>,
    closed: Arc<AtomicBool>,
    reader_handle: Mutex<Option<JoinHandle<()>>>,
}

impl MpvIpcClient {
    pub fn connect(
        sock_path: &Path,
        state: Arc<RwLock<MpvPlaybackState>>,
        sink: Arc<RwLock<Option<Arc<dyn EventSink>>>>,
        observer: Arc<RwLock<Option<StateObserver>>>,
    ) -> Result<Self, RpcError> {
        let stream = UnixStream::connect(sock_path).map_err(|err| {
            RpcError::service_offline(format!(
                "Failed connecting to mpv IPC socket {}: {err}",
                sock_path.display()
            ))
        })?;
        let read_stream = stream.try_clone().map_err(|err| {
            RpcError::internal_error(format!("Failed cloning mpv IPC stream: {err}"))
        })?;

        let pending: Arc<Mutex<HashMap<u64, mpsc::SyncSender<Result<MpvIpcReply, RpcError>>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let closed = Arc::new(AtomicBool::new(false));

        let pending_bg = Arc::clone(&pending);
        let closed_bg = Arc::clone(&closed);
        let state_bg = Arc::clone(&state);
        let sink_bg = Arc::clone(&sink);
        let observer_bg = Arc::clone(&observer);

        let handle = std::thread::Builder::new()
            .name("omnidesk-mpv-ipc".to_string())
            .spawn(move || {
                run_ipc_reader(
                    read_stream,
                    pending_bg,
                    closed_bg,
                    state_bg,
                    sink_bg,
                    observer_bg,
                );
            })
            .map_err(|err| {
                RpcError::internal_error(format!("Failed spawning mpv IPC reader thread: {err}"))
            })?;

        let client = Self {
            writer: Mutex::new(stream),
            next_request_id: AtomicU64::new(1),
            pending,
            closed,
            reader_handle: Mutex::new(Some(handle)),
        };

        for (idx, prop) in OBSERVED_PROPERTIES.iter().enumerate() {
            let obs_id = (idx + 1) as u64;
            client.command(json!(["observe_property", obs_id, prop]), DEFAULT_IPC_TIMEOUT)?;
        }

        Ok(client)
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    pub fn command(&self, cmd: Value, timeout: Duration) -> Result<Value, RpcError> {
        if self.is_closed() {
            return Err(RpcError::service_offline("mpv IPC connection is closed"));
        }
        let req_id = self.next_request_id.fetch_add(1, Ordering::Relaxed);
        let payload = encode_ipc_command(cmd, req_id)?;
        let (tx, rx) = mpsc::sync_channel(1);
        {
            let mut guard = self.pending.lock().unwrap_or_else(|e| e.into_inner());
            guard.insert(req_id, tx);
        }

        let write_res = {
            let mut writer = self.writer.lock().unwrap_or_else(|e| e.into_inner());
            writer.write_all(&payload).and_then(|_| writer.flush())
        };
        if let Err(err) = write_res {
            if let Ok(mut guard) = self.pending.lock() {
                guard.remove(&req_id);
            }
            return Err(RpcError::service_offline(redact_text(&format!(
                "Failed writing mpv IPC command: {err}"
            ))));
        }

        match rx.recv_timeout(timeout) {
            Ok(Ok(reply)) => {
                if reply.error == "success" {
                    Ok(reply.data)
                } else {
                    Err(RpcError::internal_error(redact_text(&format!(
                        "mpv rejected command: {}",
                        reply.error
                    ))))
                }
            }
            Ok(Err(err)) => Err(err),
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if let Ok(mut guard) = self.pending.lock() {
                    guard.remove(&req_id);
                }
                Err(RpcError::timeout(format!(
                    "Timed out waiting for mpv IPC reply ({} ms)",
                    timeout.as_millis()
                )))
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(RpcError::service_offline(
                "mpv IPC reader disconnected while awaiting command reply",
            )),
        }
    }

    pub fn close(&self) {
        self.closed.store(true, Ordering::Release);
        if let Ok(writer) = self.writer.lock() {
            let _ = writer.shutdown(std::net::Shutdown::Both);
        }
        if let Ok(mut handle_guard) = self.reader_handle.lock() {
            if let Some(handle) = handle_guard.take() {
                let _ = handle.join();
            }
        }
    }
}

impl Drop for MpvIpcClient {
    fn drop(&mut self) {
        self.close();
    }
}

fn run_ipc_reader(
    stream: UnixStream,
    pending: Arc<Mutex<HashMap<u64, mpsc::SyncSender<Result<MpvIpcReply, RpcError>>>>>,
    closed: Arc<AtomicBool>,
    state: Arc<RwLock<MpvPlaybackState>>,
    sink: Arc<RwLock<Option<Arc<dyn EventSink>>>>,
    observer: Arc<RwLock<Option<StateObserver>>>,
) {
    let mut reader = BufReader::new(stream);
    let mut buf = Vec::with_capacity(512);
    let mut last_time_emit = Instant::now() - POSITION_EMIT_THROTTLE;

    loop {
        buf.clear();
        let read_res = read_bounded_line(&mut reader, &mut buf, MAX_IPC_LINE_BYTES);
        match read_res {
            Ok(0) => break,
            Ok(_) => {
                let line = String::from_utf8_lossy(&buf);
                let Ok(msg) = parse_ipc_message(&line) else {
                    continue;
                };
                match msg {
                    MpvIpcMessage::Reply(reply) => {
                        let tx_opt = pending
                            .lock()
                            .ok()
                            .and_then(|mut map| map.remove(&reply.request_id));
                        if let Some(tx) = tx_opt {
                            let _ = tx.send(Ok(reply));
                        }
                    }
                    event_msg => {
                        let snap_opt = {
                            if let Ok(mut guard) = state.write() {
                                let (changed, time_only) = apply_ipc_message(&mut guard, &event_msg);
                                if changed
                                    && (!time_only
                                        || last_time_emit.elapsed() >= POSITION_EMIT_THROTTLE)
                                {
                                    last_time_emit = Instant::now();
                                    Some(guard.clone())
                                } else {
                                    None
                                }
                            } else {
                                None
                            }
                        };
                        if let Some(snap) = snap_opt {
                            emit_state_snapshot(&snap, &sink, &observer);
                        }
                    }
                }
            }
            Err(_) => break,
        }
    }

    let was_closed = closed.swap(true, Ordering::AcqRel);
    if let Ok(mut guard) = pending.lock() {
        for (_, tx) in guard.drain() {
            let _ = tx.send(Err(RpcError::service_offline(
                "mpv IPC socket closed before reply",
            )));
        }
    }

    let snap_opt = if let Ok(mut guard) = state.write() {
        let changed = guard.initialized || guard.playing || !guard.stopped;
        guard.initialized = false;
        guard.playing = false;
        guard.stopped = true;
        guard.buffering = false;
        guard.hwdec_active = false;
        guard.hwdec_current.clear();
        if !was_closed && guard.last_error.is_empty() && changed {
            guard.last_error = "mpv IPC connection closed".to_string();
        }
        if changed {
            Some(guard.clone())
        } else {
            None
        }
    } else {
        None
    };

    if let Some(snap) = snap_opt {
        emit_state_snapshot(&snap, &sink, &observer);
    }
}

pub(crate) fn emit_state_snapshot(
    snap: &MpvPlaybackState,
    sink: &RwLock<Option<Arc<dyn EventSink>>>,
    observer: &RwLock<Option<StateObserver>>,
) {
    if let Ok(guard) = sink.read() {
        if let Some(ref event_sink) = *guard {
            if let Ok(val) = serde_json::to_value(snap) {
                event_sink.emit_event(MPV_STATE_CHANNEL, val);
            }
        }
    }
    if let Ok(guard) = observer.read() {
        if let Some(ref obs) = *guard {
            obs(snap);
        }
    }
}

fn read_bounded_line(
    reader: &mut BufReader<UnixStream>,
    out: &mut Vec<u8>,
    max_bytes: usize,
) -> std::io::Result<usize> {
    let mut total = 0usize;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return Ok(total);
        }
        if let Some(pos) = available.iter().position(|&b| b == b'\n') {
            if total + pos > max_bytes {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "mpv IPC frame exceeded limit",
                ));
            }
            out.extend_from_slice(&available[..pos]);
            reader.consume(pos + 1);
            return Ok(total + pos + 1);
        }
        let len = available.len();
        if total + len > max_bytes {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "mpv IPC frame exceeded limit",
            ));
        }
        out.extend_from_slice(available);
        total += len;
        reader.consume(len);
    }
}
