//! Bounded NDJSON Unix domain socket client for OmniDesk control.sock.

use std::io::ErrorKind;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde_json::{json, Map, Value};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;

use crate::paths::{verify_peer_uid, OmnideskPaths};
use crate::protocol::{
    normalize_object_payload, parse_control_response, validate_capability_id, validate_op,
    validate_plugin_id, validate_source_id, validate_timeout_ms, validate_topic, RpcError,
    DEFAULT_TIMEOUT_MS, MAX_PAYLOAD_BYTES, MAX_TIMEOUT_MS,
};
use crate::redact::{redact_text, scrub_capability_response};

const RESERVED_RPC_KEYS: &[&str] = &["jsonrpc", "method", "path", "params"];

static REQUEST_SEQ: AtomicU64 = AtomicU64::new(1);

/// Generate a unique per-request identifier for single-request correlation.
pub fn next_request_id() -> String {
    let seq = REQUEST_SEQ.fetch_add(1, Ordering::Relaxed);
    format!("tauri-{}-{seq}", std::process::id())
}

/// Incremental NDJSON frame reader enforcing a hard byte limit and preserving carryover bytes.
pub struct NdjsonFrameReader {
    carry: Vec<u8>,
    max_bytes: usize,
}

impl NdjsonFrameReader {
    pub fn new(max_bytes: usize) -> Self {
        Self {
            carry: Vec::new(),
            max_bytes,
        }
    }

    /// Read one `\n`-delimited (or EOF-terminated) frame up to `max_bytes`.
    /// Returns `Ok(None)` cleanly at EOF with no buffered trailing bytes.
    pub async fn read_frame<R: AsyncRead + Unpin>(
        &mut self,
        reader: &mut R,
    ) -> Result<Option<Vec<u8>>, RpcError> {
        let limit_err = |max_bytes: usize| {
            RpcError::internal_error(format!("Control frame exceeded {max_bytes} byte limit"))
        };
        if let Some(nl_pos) = self.carry.iter().position(|&b| b == b'\n') {
            if nl_pos > self.max_bytes {
                return Err(limit_err(self.max_bytes));
            }
            let mut frame = self.carry[..nl_pos].to_vec();
            self.carry.drain(..=nl_pos);
            if frame.ends_with(b"\r") {
                frame.pop();
            }
            return Ok(Some(frame));
        }

        if self.carry.len() > self.max_bytes {
            return Err(limit_err(self.max_bytes));
        }

        let mut chunk = [0u8; 8192];
        loop {
            let n = reader.read(&mut chunk).await.map_err(map_stream_io_error)?;
            if n == 0 {
                if self.carry.is_empty() {
                    return Ok(None);
                }
                let mut frame = std::mem::take(&mut self.carry);
                if frame.ends_with(b"\r") {
                    frame.pop();
                }
                return Ok(Some(frame));
            }

            if let Some(rel_nl) = chunk[..n].iter().position(|&b| b == b'\n') {
                if self.carry.len() + rel_nl > self.max_bytes {
                    return Err(limit_err(self.max_bytes));
                }
                let mut frame = std::mem::take(&mut self.carry);
                frame.extend_from_slice(&chunk[..rel_nl]);
                self.carry.extend_from_slice(&chunk[rel_nl + 1..n]);
                if frame.ends_with(b"\r") {
                    frame.pop();
                }
                return Ok(Some(frame));
            }

            if self.carry.len() + n > self.max_bytes {
                return Err(limit_err(self.max_bytes));
            }
            self.carry.extend_from_slice(&chunk[..n]);
        }
    }
}

/// Async single-request RPC client for `$XDG_RUNTIME_DIR/omnidesk/control.sock`.
#[derive(Debug, Clone)]
pub struct ControlClient {
    paths: OmnideskPaths,
    caller: String,
    default_timeout: Duration,
    max_payload_bytes: usize,
}

impl ControlClient {
    pub fn new(paths: OmnideskPaths) -> Self {
        Self {
            paths,
            caller: "shell-tauri".to_string(),
            default_timeout: Duration::from_millis(DEFAULT_TIMEOUT_MS),
            max_payload_bytes: MAX_PAYLOAD_BYTES,
        }
    }

    pub fn with_limits(
        paths: OmnideskPaths,
        default_timeout: Duration,
        max_payload_bytes: usize,
    ) -> Self {
        Self {
            paths,
            caller: "shell-tauri".to_string(),
            default_timeout,
            max_payload_bytes,
        }
    }

    pub fn paths(&self) -> &OmnideskPaths {
        &self.paths
    }

    pub fn max_payload_bytes(&self) -> usize {
        self.max_payload_bytes
    }

    /// Encode and send a single NDJSON request over `control.sock`, returning normalized JSON envelope.
    pub async fn request(
        &self,
        op: &str,
        mut body: Map<String, Value>,
        timeout_ms: Option<u64>,
    ) -> Result<Value, RpcError> {
        let valid_op = validate_op(op)?;
        let timeout = match timeout_ms {
            Some(ms) => validate_timeout_ms(Some(ms))?,
            None => self.default_timeout,
        };
        let req_id = next_request_id();
        for key in RESERVED_RPC_KEYS {
            body.remove(*key);
        }
        body.insert("id".to_string(), Value::String(req_id.clone()));
        body.insert("op".to_string(), Value::String(valid_op.to_string()));
        body.insert("caller".to_string(), Value::String(self.caller.clone()));

        let mut encoded = serde_json::to_vec(&Value::Object(body))
            .map_err(|exc| RpcError::invalid_argument(format!("Failed to serialize request: {exc}")))?;
        if encoded.len() + 1 > self.max_payload_bytes {
            return Err(RpcError::invalid_argument(format!(
                "Request exceeds {} byte payload limit",
                self.max_payload_bytes
            )));
        }
        encoded.push(b'\n');

        let sock_path = self.paths.resolve_active_socket()?;
        let max_bytes = self.max_payload_bytes;

        let io_future = async move {
            let mut stream = UnixStream::connect(&sock_path)
                .await
                .map_err(|err| map_connect_io_error(&sock_path, err))?;
            verify_peer_uid(&stream, &sock_path)?;
            stream.write_all(&encoded).await.map_err(map_stream_io_error)?;
            stream.flush().await.map_err(map_stream_io_error)?;

            let mut reader = NdjsonFrameReader::new(max_bytes);
            let frame = reader
                .read_frame(&mut stream)
                .await?
                .ok_or_else(|| RpcError::internal_error("Empty response from control.sock"))?;
            parse_control_response(&frame, &req_id)
        };

        tokio::time::timeout(timeout, io_future)
            .await
            .map_err(|_| {
                RpcError::timeout(format!(
                    "Timed out waiting for control.sock ({} ms)",
                    timeout.as_millis()
                ))
            })?
    }

    pub async fn ping(&self, timeout_ms: Option<u64>) -> Result<Value, RpcError> {
        self.request("ping", Map::new(), timeout_ms).await
    }

    pub async fn list_plugins_live(&self, timeout_ms: Option<u64>) -> Result<Value, RpcError> {
        self.request("plugin.list", Map::new(), timeout_ms).await
    }

    pub async fn call_capability(
        &self,
        capability: &str,
        payload: Option<Value>,
        timeout_ms: Option<u64>,
    ) -> Value {
        let cap_id = match validate_capability_id(capability) {
            Ok(id) => id,
            Err(err) => return err.to_envelope(),
        };
        let payload_map = match normalize_object_payload(payload) {
            Ok(map) => map,
            Err(err) => return err.to_envelope(),
        };
        if let Err(err) = check_capability_call_policy(cap_id, &payload_map) {
            return err.to_envelope();
        }
        let timeout_dur = match timeout_ms {
            Some(ms) => match validate_timeout_ms(Some(ms)) {
                Ok(d) => d,
                Err(err) => return err.to_envelope(),
            },
            None => self.default_timeout,
        };

        let mut body = Map::new();
        body.insert("capability".to_string(), Value::String(cap_id.to_string()));
        body.insert("payload".to_string(), Value::Object(payload_map));
        body.insert("timeout".to_string(), json!(timeout_dur.as_secs_f64()));

        match self.request("capability.call", body, timeout_ms).await {
            Ok(val) => scrub_capability_response(cap_id, val),
            Err(err) => err.to_envelope(),
        }
    }

    pub async fn emit_event(&self, topic: &str, payload: Option<Value>, timeout_ms: Option<u64>) -> Value {
        let valid_topic = match validate_topic(topic) {
            Ok(t) => t,
            Err(err) => return err.to_envelope(),
        };
        let payload_map = match normalize_object_payload(payload) {
            Ok(map) => map,
            Err(err) => return err.to_envelope(),
        };
        let mut body = Map::new();
        body.insert("topic".to_string(), Value::String(valid_topic.to_string()));
        body.insert("payload".to_string(), Value::Object(payload_map));
        match self.request("bus.publish", body, timeout_ms).await {
            Ok(val) => val,
            Err(err) => err.to_envelope(),
        }
    }

    pub async fn call_raw(&self, op: &str, params: Option<Value>, timeout_ms: Option<u64>) -> Value {
        let valid_op = match validate_op(op) {
            Ok(v) => v,
            Err(err) => return err.to_envelope(),
        };
        let mut body = match normalize_object_payload(params) {
            Ok(map) => map,
            Err(err) => return err.to_envelope(),
        };
        let cap_opt = match validate_raw_op_params(valid_op, &mut body) {
            Ok(cap) => cap,
            Err(err) => return err.to_envelope(),
        };
        match self.request(valid_op, body, timeout_ms).await {
            Ok(val) => match cap_opt.as_deref() {
                Some(cap) => scrub_capability_response(cap, val),
                None => val,
            },
            Err(err) => err.to_envelope(),
        }
    }
}

fn check_capability_call_policy(cap_id: &str, payload: &Map<String, Value>) -> Result<(), RpcError> {
    if cap_id == "web.cookies.v1" {
        let action = payload.get("action").and_then(Value::as_str).map(|s| s.trim().to_ascii_lowercase());
        if !matches!(action.as_deref(), Some("list") | Some("import")) {
            return Err(RpcError::permission_denied("WebView is forbidden from reading plaintext cookies via web.cookies.v1"));
        }
    }
    if matches!(cap_id, "media.source.v1" | "text.source.v1" | "book.source.v1") {
        if let Some(sid_val) = payload.get("source_id").filter(|v| !v.is_null()) {
            let sid = sid_val.as_str().ok_or_else(|| RpcError::invalid_argument("payload.source_id must be a string"))?;
            validate_source_id(sid.trim())?;
        }
    }
    Ok(())
}

fn validate_timeout_seconds(op: &str, val: Option<&Value>) -> Result<(), RpcError> {
    let Some(t_val) = val else { return Ok(()); };
    let max_secs = (MAX_TIMEOUT_MS as f64) / 1000.0;
    if !t_val.as_f64().is_some_and(|v| v.is_finite() && v > 0.0 && v <= max_secs) {
        return Err(RpcError::invalid_argument(format!("{op} 'timeout' must be a finite number in (0, {max_secs}] seconds")));
    }
    Ok(())
}

fn validate_raw_op_params(op: &str, body: &mut Map<String, Value>) -> Result<Option<String>, RpcError> {
    for reserved in RESERVED_RPC_KEYS {
        if body.contains_key(*reserved) {
            return Err(RpcError::invalid_argument(format!("Reserved RPC field '{reserved}' is not permitted in params")));
        }
    }
    if op == "capability.call" {
        let cap = match (body.get("capability"), body.get("capability_id")) {
            (Some(a), Some(b)) if a != b => return Err(RpcError::invalid_argument("Conflicting 'capability' and 'capability_id'")),
            (Some(v), _) | (None, Some(v)) => v.as_str().ok_or_else(|| RpcError::invalid_argument("capability.call requires string 'capability'"))?.to_string(),
            (None, None) => return Err(RpcError::invalid_argument("capability.call requires string 'capability'")),
        };
        validate_capability_id(&cap)?;
        body.remove("capability_id");
        body.insert("capability".to_string(), Value::String(cap.clone()));
        let payload_val = match (body.get("payload"), body.get("request")) {
            (Some(a), Some(b)) if !a.is_null() && !b.is_null() && a != b => return Err(RpcError::invalid_argument("Conflicting 'payload' and 'request'")),
            (Some(a), _) if !a.is_null() => Some(a.clone()),
            (_, Some(b)) if !b.is_null() => Some(b.clone()),
            _ => None,
        };
        let norm = normalize_object_payload(payload_val)?;
        check_capability_call_policy(&cap, &norm)?;
        body.remove("request");
        body.insert("payload".to_string(), Value::Object(norm));
        validate_timeout_seconds(op, body.get("timeout"))?;
        return Ok(Some(cap));
    } else if op == "bus.publish" || op == "bus.emit" {
        let topic = body.get("topic").and_then(Value::as_str).ok_or_else(|| RpcError::invalid_argument(format!("{op} requires string 'topic'")))?;
        validate_topic(topic)?;
        let norm = normalize_object_payload(body.get("payload").cloned())?;
        body.insert("payload".to_string(), Value::Object(norm));
        validate_timeout_seconds(op, body.get("timeout"))?;
    } else if op.starts_with("plugin.") && op != "plugin.list" {
        let pid = body.get("plugin_id").and_then(Value::as_str).ok_or_else(|| RpcError::invalid_argument(format!("{op} requires string 'plugin_id'")))?;
        validate_plugin_id(pid)?;
    }
    Ok(None)
}

pub(crate) fn map_connect_io_error(sock_path: &std::path::Path, err: std::io::Error) -> RpcError {
    match err.kind() {
        ErrorKind::PermissionDenied => RpcError::permission_denied(format!("Permission denied connecting to {}: {err}", sock_path.display())),
        ErrorKind::TimedOut => RpcError::timeout(format!("Timed out connecting to {}", sock_path.display())),
        ErrorKind::NotFound | ErrorKind::ConnectionRefused => RpcError::service_offline(format!("Cannot connect to control.sock at {}: {err}", sock_path.display())),
        _ => RpcError::service_offline(redact_text(&format!("Cannot connect to control.sock at {}: {err}", sock_path.display()))),
    }
}

pub(crate) fn map_stream_io_error(err: std::io::Error) -> RpcError {
    match err.kind() {
        ErrorKind::TimedOut | ErrorKind::WouldBlock => RpcError::timeout(format!("control.sock I/O timed out: {err}")),
        ErrorKind::PermissionDenied => RpcError::permission_denied(format!("control.sock permission denied: {err}")),
        ErrorKind::ConnectionReset | ErrorKind::BrokenPipe | ErrorKind::NotConnected => RpcError::service_offline(format!("control.sock disconnected: {err}")),
        _ => RpcError::internal_error(redact_text(&format!("control.sock I/O error: {err}"))),
    }
}
