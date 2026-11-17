//! Tauri v2 command handlers bridging React UI onto OmniDesk Core control.sock.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde::Serialize;
use serde_json::{json, Map, Value};
use tauri::ipc::{CommandArg, CommandItem, InvokeBody, InvokeError};
use tauri::{Runtime, State};

use crate::control_client::ControlClient;
use crate::daemon::{DaemonManager, DaemonSnapshot};
use crate::embedded_mpv::EmbeddedMpv;
use crate::events::EventBridge;
use crate::manifest;
use crate::mpris::{self, MprisService};
use crate::mpv::{self, MpvPlayer};
use crate::mpv_ipc::MediaOpenRequest;
use crate::protocol::{
    normalize_object_payload, ok_envelope, resolve_domain_source_capability, validate_source_id,
    RpcError,
};

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeStatus {
    pub shell: &'static str,
    pub app_id: &'static str,
    pub version: &'static str,
    pub platform: &'static str,
    pub status: &'static str,
    pub mpv: mpv::MpvProbeStatus,
    pub mpris: mpris::MprisStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub daemon: Option<DaemonSnapshot>,
}

/// Shared Tauri managed state for Core RPC, EntityBus, Daemon, mpv, and MPRIS2.
pub struct ShellState {
    pub client: ControlClient,
    pub bridge: Arc<EventBridge>,
    pub daemon: Arc<DaemonManager>,
    pub mpv: Arc<MpvPlayer>,
    pub mpris: Arc<MprisService>,
    shutdown_once: AtomicBool,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HostPathsSnapshot {
    pub home_dir: String,
    pub config_dir: String,
    pub data_dir: String,
    pub state_dir: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub runtime_dir: Option<String>,
    pub plugin_dirs: Vec<String>,
    pub control_socket: String,
    pub mpv_socket: String,
}

impl ShellState {
    pub fn new(
        client: ControlClient,
        bridge: Arc<EventBridge>,
        daemon: Arc<DaemonManager>,
        mpv: Arc<MpvPlayer>,
        mpris: Arc<MprisService>,
    ) -> Self {
        Self {
            client,
            bridge,
            daemon,
            mpv,
            mpris,
            shutdown_once: AtomicBool::new(false),
        }
    }

    pub fn shutdown(&self) {
        if self.shutdown_once.swap(true, Ordering::AcqRel) {
            return;
        }
        self.bridge.stop();
        self.mpris.stop();
        self.mpv.shutdown();
        self.daemon.stop();
    }
}

pub fn build_runtime_status(
    mpv_status: mpv::MpvProbeStatus,
    mpris_status: mpris::MprisStatus,
    daemon_snap: Option<DaemonSnapshot>,
) -> RuntimeStatus {
    let overall = if mpv_status.available {
        "ready"
    } else {
        "degraded"
    };
    RuntimeStatus {
        shell: "tauri-v2",
        app_id: "omnidesk",
        version: env!("CARGO_PKG_VERSION"),
        platform: std::env::consts::OS,
        status: overall,
        mpv: mpv_status,
        mpris: mpris_status,
        daemon: daemon_snap,
    }
}

pub fn standalone_runtime_status() -> RuntimeStatus {
    build_runtime_status(
        mpv::probe(),
        mpris::MprisStatus::disabled("MPRIS2 service not started in standalone probe"),
        None,
    )
}

/// Pure core_status implementation usable both from Tauri commands and headless tests.
pub async fn exec_core_status(client: &ControlClient, bridge: &EventBridge) -> Value {
    exec_core_status_with_daemon(client, bridge, None).await
}

pub async fn exec_core_status_with_daemon(
    client: &ControlClient,
    bridge: &EventBridge,
    daemon: Option<&DaemonManager>,
) -> Value {
    if let Some(d) = daemon {
        d.wait_if_starting(Duration::from_secs(7)).await;
    }

    let bridge_snap = bridge.snapshot();
    let daemon_snap = daemon.map(DaemonManager::snapshot);
    let sock_display = client
        .paths()
        .resolve_active_socket()
        .unwrap_or_else(|_| client.paths().primary_socket_path())
        .display()
        .to_string();

    match client.ping(None).await {
        Ok(mut val) => {
            if val.get("ok").and_then(Value::as_bool) == Some(true) {
                if bridge_snap.status == "offline" {
                    bridge.notify_wakeup();
                }
                let mut data = val.get("data").cloned().unwrap_or_else(|| json!({}));
                if let Some(obj) = data.as_object_mut() {
                    obj.insert("online".to_string(), Value::Bool(true));
                    obj.insert("degraded".to_string(), Value::Bool(!bridge_snap.supported));
                    obj.insert("socket_path".to_string(), Value::String(sock_display));
                    obj.insert(
                        "event_bridge".to_string(),
                        serde_json::to_value(&bridge_snap).unwrap_or(Value::Null),
                    );
                    if let Some(ref d_snap) = daemon_snap {
                        obj.insert(
                            "daemon".to_string(),
                            serde_json::to_value(d_snap).unwrap_or(Value::Null),
                        );
                    }
                }
                ok_envelope(data, "core")
            } else {
                if !val.get("data").is_some_and(Value::is_object) {
                    val["data"] = json!({});
                }
                if let Some(obj) = val.get_mut("data").and_then(Value::as_object_mut) {
                    obj.insert("ok".to_string(), Value::Bool(false));
                    obj.insert("online".to_string(), Value::Bool(false));
                    obj.insert("degraded".to_string(), Value::Bool(true));
                    obj.insert("socket_path".to_string(), Value::String(sock_display));
                    obj.insert(
                        "event_bridge".to_string(),
                        serde_json::to_value(&bridge_snap).unwrap_or(Value::Null),
                    );
                    if let Some(ref d_snap) = daemon_snap {
                        obj.insert(
                            "daemon".to_string(),
                            serde_json::to_value(d_snap).unwrap_or(Value::Null),
                        );
                    }
                }
                val
            }
        }
        Err(mut err) => {
            if let Some(ref d_snap) = daemon_snap {
                if let Some(ref d_err) = d_snap.last_error {
                    if !d_err.is_empty() && !err.message.contains(d_err) {
                        err.message = format!("{}; {d_err}", err.message);
                    }
                }
            }
            let mut data_map = Map::new();
            data_map.insert("ok".to_string(), Value::Bool(false));
            data_map.insert("online".to_string(), Value::Bool(false));
            data_map.insert("degraded".to_string(), Value::Bool(true));
            data_map.insert("socket_path".to_string(), Value::String(sock_display));
            data_map.insert(
                "event_bridge".to_string(),
                serde_json::to_value(&bridge_snap).unwrap_or(Value::Null),
            );
            if let Some(ref d_snap) = daemon_snap {
                data_map.insert(
                    "daemon".to_string(),
                    serde_json::to_value(d_snap).unwrap_or(Value::Null),
                );
            }
            err.data = Some(Value::Object(data_map));
            err.to_envelope()
        }
    }
}

/// Pure list_plugins implementation usable both from Tauri commands and headless tests.
pub async fn exec_list_plugins(client: &ControlClient, live_only: Option<bool>) -> Value {
    match client.list_plugins_live(None).await {
        Ok(val) => {
            if val.get("ok").and_then(Value::as_bool) == Some(true) {
                let plugins = val
                    .get("data")
                    .and_then(|d| d.get("plugins"))
                    .cloned()
                    .unwrap_or_else(|| json!([]));
                ok_envelope(
                    json!({
                        "plugins": plugins,
                        "source": "control_socket",
                        "core_online": true,
                        "degraded": false,
                    }),
                    "",
                )
            } else {
                val
            }
        }
        Err(err) => {
            if err.code == "service_offline" && !live_only.unwrap_or(false) {
                let plugins = manifest::scan_plugin_manifests(client.paths());
                ok_envelope(
                    json!({
                        "plugins": plugins,
                        "source": "manifest_scan",
                        "core_online": false,
                        "degraded": true,
                        "notice": err.message,
                    }),
                    "",
                )
            } else {
                err.to_envelope()
            }
        }
    }
}

/// Pure read_source implementation usable both from Tauri commands and headless tests.
pub async fn exec_read_source(
    client: &ControlClient,
    domain: &str,
    source_id: Option<&str>,
    payload: Option<Value>,
    timeout_ms: Option<u64>,
) -> Value {
    let (_, cap_id) = match resolve_domain_source_capability(domain) {
        Ok(pair) => pair,
        Err(err) => return err.to_envelope(),
    };
    let mut map = match normalize_object_payload(payload) {
        Ok(m) => m,
        Err(err) => return err.to_envelope(),
    };
    let payload_sid = match map.get("source_id") {
        None | Some(Value::Null) => {
            map.remove("source_id");
            None
        }
        Some(Value::String(s)) => {
            let trimmed = s.trim();
            if trimmed.is_empty() {
                return RpcError::invalid_argument("payload.source_id must not be empty").to_envelope();
            }
            if let Err(err) = validate_source_id(trimmed) {
                return err.to_envelope();
            }
            Some(trimmed.to_string())
        }
        Some(_) => return RpcError::invalid_argument("payload.source_id must be a string").to_envelope(),
    };

    if let Some(sid) = source_id {
        let trimmed = sid.trim();
        if trimmed.is_empty() {
            return RpcError::invalid_argument("source_id must not be empty").to_envelope();
        }
        if let Err(err) = validate_source_id(trimmed) {
            return err.to_envelope();
        }
        if let Some(ref p_sid) = payload_sid {
            if p_sid != trimmed {
                return RpcError::invalid_argument(
                    "Conflicting 'source_id' parameter and payload.source_id",
                )
                .to_envelope();
            }
        }
        map.insert("source_id".to_string(), Value::String(trimmed.to_string()));
    } else if let Some(p_sid) = payload_sid {
        map.insert("source_id".to_string(), Value::String(p_sid));
    }

    client
        .call_capability(cap_id, Some(Value::Object(map)), timeout_ms)
        .await
}

/// Owned raw IPC JSON payload supporting flat and `{ "args": ... }` forms plus `snake_case`/`camelCase`.
#[derive(Debug, Clone)]
pub enum RawJsonArgs {
    Value(Value),
    Invalid(String),
}

impl RawJsonArgs {
    /// Decode a Tauri [`InvokeBody`] (`Json` or `Raw`) into raw JSON command arguments.
    pub fn from_invoke_body(body: &InvokeBody) -> Self {
        match body {
            InvokeBody::Json(v) => Self::Value(v.clone()),
            InvokeBody::Raw(bytes) if bytes.is_empty() => Self::Value(json!({})),
            InvokeBody::Raw(bytes) => match serde_json::from_slice::<Value>(bytes) {
                Ok(v) => Self::Value(v),
                Err(err) => Self::Invalid(format!("Invalid JSON command arguments: {err}")),
            },
        }
    }
}

impl<'de, R: Runtime> CommandArg<'de, R> for RawJsonArgs {
    fn from_command(command: CommandItem<'de, R>) -> Result<Self, InvokeError> {
        Ok(Self::from_invoke_body(command.message.payload()))
    }
}

fn parse_args_map(raw: &Value) -> Result<Map<String, Value>, RpcError> {
    match raw {
        Value::Null => Ok(Map::new()),
        Value::Object(map) => {
            let Some(args_val) = map.get("args") else {
                return Ok(map.clone());
            };
            let inner = args_val
                .as_object()
                .ok_or_else(|| RpcError::invalid_argument("Command 'args' must be a JSON object"))?;
            if inner.contains_key("args") {
                return Err(RpcError::invalid_argument(
                    "Nested 'args' wrapper is not supported",
                ));
            }
            let mut merged = inner.clone();
            for (key, top_val) in map {
                if key == "args" {
                    continue;
                }
                if let Some(inner_val) = merged.get(key) {
                    if inner_val != top_val {
                        return Err(RpcError::invalid_argument(format!(
                            "Conflicting top-level '{key}' and 'args.{key}'"
                        )));
                    }
                } else {
                    merged.insert(key.clone(), top_val.clone());
                }
            }
            Ok(merged)
        }
        _ => Err(RpcError::invalid_argument("Command arguments must be a JSON object")),
    }
}

fn read_dual<T>(
    map: &Map<String, Value>,
    snake: &str,
    camel: &str,
    parse: fn(&str, &Value) -> Result<Option<T>, RpcError>,
    eq: fn(&T, &T) -> bool,
) -> Result<Option<T>, RpcError> {
    let s_val = map.get(snake).map(|v| parse(snake, v)).transpose()?.flatten();
    let c_val = map.get(camel).map(|v| parse(camel, v)).transpose()?.flatten();
    match (s_val, c_val) {
        (Some(a), Some(b)) if !eq(&a, &b) => {
            Err(RpcError::invalid_argument(format!("Conflicting '{snake}' and '{camel}'")))
        }
        (a, b) => Ok(a.or(b)),
    }
}

fn parse_opt_bool(key: &str, val: &Value) -> Result<Option<bool>, RpcError> {
    match val {
        Value::Null => Ok(None),
        Value::Bool(b) => Ok(Some(*b)),
        _ => Err(RpcError::invalid_argument(format!("'{key}' must be a boolean"))),
    }
}

fn parse_opt_u64(key: &str, val: &Value) -> Result<Option<u64>, RpcError> {
    match val {
        Value::Null => Ok(None),
        Value::Number(n) => n
            .as_u64()
            .map(Some)
            .ok_or_else(|| RpcError::invalid_argument(format!("'{key}' must be a non-negative integer"))),
        _ => Err(RpcError::invalid_argument(format!("'{key}' must be a non-negative integer"))),
    }
}

fn parse_opt_str(key: &str, val: &Value) -> Result<Option<String>, RpcError> {
    match val {
        Value::Null => Ok(None),
        Value::String(s) => Ok(Some(s.clone())),
        _ => Err(RpcError::invalid_argument(format!("'{key}' must be a string"))),
    }
}

fn opt_bool_dual(map: &Map<String, Value>, snake: &str, camel: &str) -> Result<Option<bool>, RpcError> {
    read_dual(map, snake, camel, parse_opt_bool, |a, b| a == b)
}

fn opt_u64_dual(map: &Map<String, Value>, snake: &str, camel: &str) -> Result<Option<u64>, RpcError> {
    read_dual(map, snake, camel, parse_opt_u64, |a, b| a == b)
}

fn opt_str_dual(map: &Map<String, Value>, snake: &str, camel: &str) -> Result<Option<String>, RpcError> {
    read_dual(map, snake, camel, parse_opt_str, |a, b| a.trim() == b.trim())
}

fn req_str(map: &Map<String, Value>, key: &str) -> Result<String, RpcError> {
    match map.get(key) {
        Some(Value::String(s)) => Ok(s.clone()),
        None | Some(Value::Null) => Err(RpcError::invalid_argument(format!("Missing required argument '{key}'"))),
        Some(_) => Err(RpcError::invalid_argument(format!("'{key}' must be a string"))),
    }
}

/// Dispatch decoded [`RawJsonArgs`] to one of the parameterized Tauri commands.
pub async fn exec_command_raw(
    client: &ControlClient,
    command: &str,
    args: RawJsonArgs,
) -> Result<Value, ()> {
    let raw = match args {
        RawJsonArgs::Value(v) => v,
        RawJsonArgs::Invalid(msg) => return Ok(RpcError::invalid_argument(msg).to_envelope()),
    };
    Ok(match command {
        "list_plugins" => exec_list_plugins_args(client, &raw).await,
        "call_capability" => exec_call_capability_args(client, &raw).await,
        "emit_event" => exec_emit_event_args(client, &raw).await,
        "read_source" => exec_read_source_args(client, &raw).await,
        "call" => exec_call_args(client, &raw).await,
        other => RpcError::invalid_argument(format!("Unknown command '{other}'")).to_envelope(),
    })
}

/// Decode a Tauri [`InvokeBody`] (`Json` or `Raw`) and dispatch it to a parameterized command.
pub async fn exec_command_invoke_body(
    client: &ControlClient,
    command: &str,
    body: &InvokeBody,
) -> Result<Value, ()> {
    exec_command_raw(client, command, RawJsonArgs::from_invoke_body(body)).await
}

pub async fn exec_list_plugins_args(client: &ControlClient, raw: &Value) -> Value {
    match parse_args_map(raw).and_then(|m| opt_bool_dual(&m, "live_only", "liveOnly")) {
        Ok(live_only) => exec_list_plugins(client, live_only).await,
        Err(err) => err.to_envelope(),
    }
}

pub async fn exec_call_capability_args(client: &ControlClient, raw: &Value) -> Value {
    match parse_args_map(raw).and_then(|m| Ok((req_str(&m, "capability")?, m.get("payload").cloned(), opt_u64_dual(&m, "timeout_ms", "timeoutMs")?))) {
        Ok((cap, payload, timeout_ms)) => client.call_capability(&cap, payload, timeout_ms).await,
        Err(err) => err.to_envelope(),
    }
}

pub async fn exec_emit_event_args(client: &ControlClient, raw: &Value) -> Value {
    match parse_args_map(raw).and_then(|m| Ok((req_str(&m, "topic")?, m.get("payload").cloned(), opt_u64_dual(&m, "timeout_ms", "timeoutMs")?))) {
        Ok((topic, payload, timeout_ms)) => client.emit_event(&topic, payload, timeout_ms).await,
        Err(err) => err.to_envelope(),
    }
}

pub async fn exec_read_source_args(client: &ControlClient, raw: &Value) -> Value {
    match parse_args_map(raw).and_then(|m| Ok((req_str(&m, "domain")?, opt_str_dual(&m, "source_id", "sourceId")?, m.get("payload").cloned(), opt_u64_dual(&m, "timeout_ms", "timeoutMs")?))) {
        Ok((domain, source_id, payload, timeout_ms)) => exec_read_source(client, &domain, source_id.as_deref(), payload, timeout_ms).await,
        Err(err) => err.to_envelope(),
    }
}

pub async fn exec_call_args(client: &ControlClient, raw: &Value) -> Value {
    match parse_args_map(raw).and_then(|m| Ok((req_str(&m, "op")?, m.get("params").cloned(), opt_u64_dual(&m, "timeout_ms", "timeoutMs")?))) {
        Ok((op, params, timeout_ms)) => client.call_raw(&op, params, timeout_ms).await,
        Err(err) => err.to_envelope(),
    }
}

fn parse_req_f64(map: &Map<String, Value>, key: &str) -> Result<f64, RpcError> {
    match map.get(key) {
        Some(Value::Number(n)) => n
            .as_f64()
            .filter(|v| v.is_finite())
            .ok_or_else(|| RpcError::invalid_argument(format!("'{key}' must be a finite number"))),
        None | Some(Value::Null) => Err(RpcError::invalid_argument(format!(
            "Missing required argument '{key}'"
        ))),
        Some(_) => Err(RpcError::invalid_argument(format!(
            "'{key}' must be a number"
        ))),
    }
}

fn parse_req_volume(map: &Map<String, Value>) -> Result<i64, RpcError> {
    match map.get("volume") {
        Some(Value::Number(n)) => n
            .as_f64()
            .filter(|v| v.is_finite())
            .map(|v| v.round().clamp(0.0, 100.0) as i64)
            .ok_or_else(|| RpcError::invalid_argument("'volume' must be a finite number")),
        None | Some(Value::Null) => Err(RpcError::invalid_argument(
            "Missing required argument 'volume'",
        )),
        Some(_) => Err(RpcError::invalid_argument("'volume' must be a number")),
    }
}

fn parse_req_u32(map: &Map<String, Value>, key: &str) -> Result<u32, RpcError> {
    match map.get(key) {
        Some(Value::Number(n)) => n
            .as_u64()
            .and_then(|v| u32::try_from(v).ok())
            .ok_or_else(|| RpcError::invalid_argument(format!("'{key}' must be a valid u32"))),
        None | Some(Value::Null) => Err(RpcError::invalid_argument(format!(
            "Missing required argument '{key}'"
        ))),
        Some(_) => Err(RpcError::invalid_argument(format!(
            "'{key}' must be a number"
        ))),
    }
}

fn mpv_result_envelope(res: Result<crate::mpv_ipc::MpvPlaybackState, RpcError>) -> Value {
    match res {
        Ok(st) => ok_envelope(serde_json::to_value(st).unwrap_or(Value::Null), "mpv"),
        Err(err) => err.to_envelope(),
    }
}

pub fn exec_mpv_open_args(player: &MpvPlayer, raw: &Value) -> Value {
    let map = match parse_args_map(raw) {
        Ok(m) => m,
        Err(err) => return err.to_envelope(),
    };
    let req_val = if let Some(inner) = map.get("options").or_else(|| map.get("request")) {
        if !inner.is_object() {
            return RpcError::invalid_argument("'options' must be a JSON object").to_envelope();
        }
        inner.clone()
    } else {
        Value::Object(map)
    };
    let req: MediaOpenRequest = match serde_json::from_value(req_val) {
        Ok(r) => r,
        Err(err) => {
            return RpcError::invalid_argument(format!("Invalid mpv_open arguments: {err}"))
                .to_envelope()
        }
    };
    mpv_result_envelope(player.open(req))
}

pub fn exec_mpv_seek_args(player: &MpvPlayer, raw: &Value) -> Value {
    let map = match parse_args_map(raw) {
        Ok(m) => m,
        Err(err) => return err.to_envelope(),
    };
    let seconds = match parse_req_f64(&map, "seconds") {
        Ok(v) => v,
        Err(err) => return err.to_envelope(),
    };
    let rel_opt = match parse_opt_bool_field(&map, "relative") {
        Ok(v) => v,
        Err(err) => return err.to_envelope(),
    };
    let mode_opt = match parse_opt_str_field(&map, "mode") {
        Ok(v) => v,
        Err(err) => return err.to_envelope(),
    };
    let relative = match (rel_opt, mode_opt.as_deref()) {
        (Some(r), Some(mode)) => {
            let mode_rel = match mode.trim().to_ascii_lowercase().as_str() {
                "relative" => true,
                "absolute" => false,
                _ => {
                    return RpcError::invalid_argument(
                        "seek 'mode' must be 'absolute' or 'relative'",
                    )
                    .to_envelope()
                }
            };
            if r != mode_rel {
                return RpcError::invalid_argument("Conflicting 'relative' and 'mode' arguments")
                    .to_envelope();
            }
            r
        }
        (Some(r), None) => r,
        (None, Some(mode)) => match mode.trim().to_ascii_lowercase().as_str() {
            "relative" => true,
            "absolute" => false,
            _ => {
                return RpcError::invalid_argument(
                    "seek 'mode' must be 'absolute' or 'relative'",
                )
                .to_envelope()
            }
        },
        (None, None) => false,
    };
    mpv_result_envelope(player.seek(seconds, relative))
}

pub fn exec_mpv_volume_args(player: &MpvPlayer, raw: &Value) -> Value {
    let map = match parse_args_map(raw) {
        Ok(m) => m,
        Err(err) => return err.to_envelope(),
    };
    match parse_req_volume(&map) {
        Ok(vol) => mpv_result_envelope(player.set_volume(vol)),
        Err(err) => err.to_envelope(),
    }
}

pub fn exec_mpv_speed_args(player: &MpvPlayer, raw: &Value) -> Value {
    let map = match parse_args_map(raw) {
        Ok(m) => m,
        Err(err) => return err.to_envelope(),
    };
    match parse_req_f64(&map, "speed") {
        Ok(speed) => mpv_result_envelope(player.set_speed(speed)),
        Err(err) => err.to_envelope(),
    }
}

fn parse_opt_bool_field(map: &Map<String, Value>, key: &str) -> Result<Option<bool>, RpcError> {
    match map.get(key) {
        None => Ok(None),
        Some(v) => parse_opt_bool(key, v),
    }
}

fn parse_opt_str_field(map: &Map<String, Value>, key: &str) -> Result<Option<String>, RpcError> {
    match map.get(key) {
        None => Ok(None),
        Some(v) => parse_opt_str(key, v),
    }
}

fn decode_raw_json(args: RawJsonArgs) -> Result<Value, Value> {
    match args {
        RawJsonArgs::Value(v) => Ok(v),
        RawJsonArgs::Invalid(msg) => Err(RpcError::invalid_argument(msg).to_envelope()),
    }
}

#[tauri::command]
pub fn runtime_status(state: State<'_, ShellState>) -> RuntimeStatus {
    build_runtime_status(
        state.mpv.probe(),
        state.mpris.status(),
        Some(state.daemon.snapshot()),
    )
}

#[tauri::command]
pub fn host_paths(state: State<'_, ShellState>) -> HostPathsSnapshot {
    let paths = state.client.paths();
    HostPathsSnapshot {
        home_dir: paths.home_dir.display().to_string(),
        config_dir: paths.config_dir.display().to_string(),
        data_dir: paths.data_dir.display().to_string(),
        state_dir: paths.state_dir.display().to_string(),
        runtime_dir: paths.runtime_dir.as_ref().map(|path| path.display().to_string()),
        plugin_dirs: paths
            .plugin_search_dirs()
            .into_iter()
            .map(|path| path.display().to_string())
            .collect(),
        control_socket: paths
            .resolve_active_socket()
            .unwrap_or_else(|_| paths.primary_socket_path())
            .display()
            .to_string(),
        mpv_socket: paths.mpv_socket_path().display().to_string(),
    }
}

#[tauri::command]
pub fn mpv_probe(state: State<'_, ShellState>) -> mpv::MpvProbeStatus {
    state.mpv.probe()
}

#[tauri::command]
pub fn mpv_embed_probe() -> Value {
    ok_envelope(
        serde_json::to_value(EmbeddedMpv::probe()).unwrap_or(Value::Null),
        "libmpv",
    )
}

#[tauri::command]
pub async fn mpv_embed_enable(state: State<'_, ShellState>) -> Result<Value, ()> {
    let player = Arc::clone(&state.mpv);
    let mpris = Arc::clone(&state.mpris);
    let result = tokio::task::spawn_blocking(move || player.enable_embedded())
        .await
        .unwrap_or_else(|err| {
            Err(RpcError::internal_error(format!(
                "mpv_embed_enable task failed: {err}"
            )))
        });
    mpris.notify_state_changed();
    Ok(match result {
        Ok(st) => ok_envelope(serde_json::to_value(st).unwrap_or(Value::Null), "libmpv"),
        Err(err) => err.to_envelope(),
    })
}

#[tauri::command]
pub async fn mpv_embed_disable(state: State<'_, ShellState>) -> Result<Value, ()> {
    let player = Arc::clone(&state.mpv);
    let mpris = Arc::clone(&state.mpris);
    let result = tokio::task::spawn_blocking(move || player.disable_embedded())
        .await
        .unwrap_or_else(|err| {
            Err(RpcError::internal_error(format!(
                "mpv_embed_disable task failed: {err}"
            )))
        });
    mpris.notify_state_changed();
    Ok(match result {
        Ok(st) => ok_envelope(serde_json::to_value(st).unwrap_or(Value::Null), "libmpv"),
        Err(err) => err.to_envelope(),
    })
}

#[tauri::command]
pub fn mpv_embed_set_viewport(state: State<'_, ShellState>, args: RawJsonArgs) -> Value {
    let raw = match decode_raw_json(args) {
        Ok(v) => v,
        Err(env) => return env,
    };
    let map = match parse_args_map(&raw) {
        Ok(map) => map,
        Err(err) => return err.to_envelope(),
    };
    let width = match parse_req_u32(&map, "width") {
        Ok(v) => v,
        Err(err) => return err.to_envelope(),
    };
    let height = match parse_req_u32(&map, "height") {
        Ok(v) => v,
        Err(err) => return err.to_envelope(),
    };
    let fps = map
        .get("fps")
        .and_then(Value::as_u64)
        .unwrap_or(24)
        .clamp(1, 60) as u32;
    match state.mpv.set_embedded_viewport(width, height, fps) {
        Ok(()) => ok_envelope(
            json!({ "width": width.clamp(320, 1280), "height": height.clamp(180, 720), "fps": fps }),
            "libmpv",
        ),
        Err(err) => err.to_envelope(),
    }
}

#[tauri::command]
pub fn mpv_embed_set_active(state: State<'_, ShellState>, args: RawJsonArgs) -> Value {
    let raw = match decode_raw_json(args) {
        Ok(v) => v,
        Err(env) => return env,
    };
    let map = match parse_args_map(&raw) {
        Ok(map) => map,
        Err(err) => return err.to_envelope(),
    };
    let active = match map.get("active").and_then(Value::as_bool) {
        Some(value) => value,
        None => {
            return RpcError::invalid_argument("Missing required boolean argument 'active'")
                .to_envelope()
        }
    };
    match state.mpv.set_embedded_frames_active(active) {
        Ok(()) => ok_envelope(json!({ "active": active }), "libmpv"),
        Err(err) => err.to_envelope(),
    }
}

#[tauri::command]
pub async fn mpv_open(state: State<'_, ShellState>, args: RawJsonArgs) -> Result<Value, ()> {
    let raw = match decode_raw_json(args) {
        Ok(v) => v,
        Err(env) => return Ok(env),
    };
    let player = Arc::clone(&state.mpv);
    let mpris = Arc::clone(&state.mpris);
    let res = tokio::task::spawn_blocking(move || exec_mpv_open_args(&player, &raw))
        .await
        .unwrap_or_else(|err| RpcError::internal_error(format!("mpv_open task failed: {err}")).to_envelope());
    mpris.notify_state_changed();
    Ok(res)
}

#[tauri::command]
pub async fn mpv_pause(state: State<'_, ShellState>) -> Result<Value, ()> {
    let player = Arc::clone(&state.mpv);
    let mpris = Arc::clone(&state.mpris);
    let res = tokio::task::spawn_blocking(move || mpv_result_envelope(player.pause()))
        .await
        .unwrap_or_else(|err| RpcError::internal_error(format!("mpv_pause task failed: {err}")).to_envelope());
    mpris.notify_state_changed();
    Ok(res)
}

#[tauri::command]
pub async fn mpv_resume(state: State<'_, ShellState>) -> Result<Value, ()> {
    let player = Arc::clone(&state.mpv);
    let mpris = Arc::clone(&state.mpris);
    let res = tokio::task::spawn_blocking(move || mpv_result_envelope(player.resume()))
        .await
        .unwrap_or_else(|err| RpcError::internal_error(format!("mpv_resume task failed: {err}")).to_envelope());
    mpris.notify_state_changed();
    Ok(res)
}

#[tauri::command]
pub async fn mpv_toggle_pause(state: State<'_, ShellState>) -> Result<Value, ()> {
    let player = Arc::clone(&state.mpv);
    let mpris = Arc::clone(&state.mpris);
    let res = tokio::task::spawn_blocking(move || mpv_result_envelope(player.toggle_pause()))
        .await
        .unwrap_or_else(|err| RpcError::internal_error(format!("mpv_toggle_pause task failed: {err}")).to_envelope());
    mpris.notify_state_changed();
    Ok(res)
}

#[tauri::command]
pub async fn mpv_stop(state: State<'_, ShellState>) -> Result<Value, ()> {
    let player = Arc::clone(&state.mpv);
    let mpris = Arc::clone(&state.mpris);
    let res = tokio::task::spawn_blocking(move || mpv_result_envelope(player.stop_playback()))
        .await
        .unwrap_or_else(|err| RpcError::internal_error(format!("mpv_stop task failed: {err}")).to_envelope());
    mpris.notify_state_changed();
    Ok(res)
}

#[tauri::command]
pub async fn mpv_seek(state: State<'_, ShellState>, args: RawJsonArgs) -> Result<Value, ()> {
    let raw = match decode_raw_json(args) {
        Ok(v) => v,
        Err(env) => return Ok(env),
    };
    let player = Arc::clone(&state.mpv);
    let mpris = Arc::clone(&state.mpris);
    let res = tokio::task::spawn_blocking(move || exec_mpv_seek_args(&player, &raw))
        .await
        .unwrap_or_else(|err| RpcError::internal_error(format!("mpv_seek task failed: {err}")).to_envelope());
    mpris.notify_state_changed();
    Ok(res)
}

#[tauri::command]
pub async fn mpv_volume(state: State<'_, ShellState>, args: RawJsonArgs) -> Result<Value, ()> {
    let raw = match decode_raw_json(args) {
        Ok(v) => v,
        Err(env) => return Ok(env),
    };
    let player = Arc::clone(&state.mpv);
    let mpris = Arc::clone(&state.mpris);
    let res = tokio::task::spawn_blocking(move || exec_mpv_volume_args(&player, &raw))
        .await
        .unwrap_or_else(|err| RpcError::internal_error(format!("mpv_volume task failed: {err}")).to_envelope());
    mpris.notify_state_changed();
    Ok(res)
}

#[tauri::command]
pub async fn mpv_set_speed(state: State<'_, ShellState>, args: RawJsonArgs) -> Result<Value, ()> {
    let raw = match decode_raw_json(args) {
        Ok(v) => v,
        Err(env) => return Ok(env),
    };
    let player = Arc::clone(&state.mpv);
    let mpris = Arc::clone(&state.mpris);
    let res = tokio::task::spawn_blocking(move || exec_mpv_speed_args(&player, &raw))
        .await
        .unwrap_or_else(|err| RpcError::internal_error(format!("mpv_set_speed task failed: {err}")).to_envelope());
    mpris.notify_state_changed();
    Ok(res)
}

#[tauri::command]
pub fn mpv_status(state: State<'_, ShellState>) -> Value {
    mpv_result_envelope(Ok(state.mpv.status()))
}

#[tauri::command]
pub fn mpris_status(state: State<'_, ShellState>) -> Value {
    ok_envelope(serde_json::to_value(state.mpris.status()).unwrap_or(Value::Null), "mpris")
}

#[tauri::command]
pub fn daemon_status(state: State<'_, ShellState>) -> Value {
    ok_envelope(serde_json::to_value(state.daemon.snapshot()).unwrap_or(Value::Null), "daemon")
}

#[tauri::command]
pub async fn core_status(state: State<'_, ShellState>) -> Result<Value, ()> {
    Ok(exec_core_status_with_daemon(&state.client, &state.bridge, Some(&state.daemon)).await)
}

#[tauri::command]
pub async fn list_plugins(state: State<'_, ShellState>, args: RawJsonArgs) -> Result<Value, ()> {
    exec_command_raw(&state.client, "list_plugins", args).await
}

#[tauri::command]
pub async fn call_capability(state: State<'_, ShellState>, args: RawJsonArgs) -> Result<Value, ()> {
    exec_command_raw(&state.client, "call_capability", args).await
}

#[tauri::command]
pub async fn emit_event(state: State<'_, ShellState>, args: RawJsonArgs) -> Result<Value, ()> {
    exec_command_raw(&state.client, "emit_event", args).await
}

#[tauri::command]
pub async fn read_source(state: State<'_, ShellState>, args: RawJsonArgs) -> Result<Value, ()> {
    exec_command_raw(&state.client, "read_source", args).await
}

#[tauri::command]
pub async fn call(state: State<'_, ShellState>, args: RawJsonArgs) -> Result<Value, ()> {
    exec_command_raw(&state.client, "call", args).await
}

#[tauri::command]
pub fn event_bridge_status(state: State<'_, ShellState>) -> Value {
    let snap = state.bridge.snapshot();
    ok_envelope(serde_json::to_value(snap).unwrap_or(Value::Null), "")
}
