//! Protocol constants, parameter validation, and structured RPC envelopes.

use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::redact::{redact_json_value, redact_text};

/// Maximum control.sock NDJSON frame size (8 MiB, matching Core DEFAULT_MAX_PAYLOAD_BYTES).
pub const MAX_PAYLOAD_BYTES: usize = 8 * 1024 * 1024;
/// Default RPC request timeout in milliseconds.
pub const DEFAULT_TIMEOUT_MS: u64 = 15_000;
/// Minimum allowed per-request timeout in milliseconds.
pub const MIN_TIMEOUT_MS: u64 = 100;
/// Maximum allowed per-request timeout in milliseconds (1 hour, matching SpeechAsrRequest).
pub const MAX_TIMEOUT_MS: u64 = 3_600_000;

/// Allowlisted single-request control.sock operations callable via `call`.
pub const ALLOWED_CONTROL_OPS: &[&str] = &[
    "ping",
    "health",
    "core.ping",
    "capability.list",
    "capability.call",
    "bus.publish",
    "bus.emit",
    "plugin.list",
    "plugin.load",
    "plugin.unload",
    "plugin.reload",
    "plugin.verify",
    "plugin.health",
];

/// Structured IPC/control error mapped into `{ ok: false, error_code, error }`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RpcError {
    pub code: String,
    pub message: String,
    pub provider_plugin_id: String,
    pub data: Option<Value>,
}

impl RpcError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: redact_text(&message.into()),
            provider_plugin_id: String::new(),
            data: None,
        }
    }

    pub fn service_offline(message: impl Into<String>) -> Self {
        Self::new("service_offline", message)
    }

    pub fn timeout(message: impl Into<String>) -> Self {
        Self::new("timeout", message)
    }

    pub fn invalid_argument(message: impl Into<String>) -> Self {
        Self::new("invalid_argument", message)
    }

    pub fn path_safety(message: impl Into<String>) -> Self {
        Self::new("path_safety", message)
    }

    pub fn permission_denied(message: impl Into<String>) -> Self {
        Self::new("permission_denied", message)
    }

    pub fn internal_error(message: impl Into<String>) -> Self {
        Self::new("internal_error", message)
    }

    /// Serialize into a stable `{ ok: false, error_code, error, ... }` JSON envelope.
    pub fn to_envelope(&self) -> Value {
        let mut data_map = match &self.data {
            Some(Value::Object(m)) => match redact_json_value(&Value::Object(m.clone())) {
                Value::Object(redacted) => redacted,
                _ => Map::new(),
            },
            _ => Map::new(),
        };
        data_map.insert("error_code".to_string(), Value::String(self.code.clone()));

        json!({
            "ok": false,
            "error_code": self.code,
            "error": self.message,
            "provider_plugin_id": self.provider_plugin_id,
            "data": Value::Object(data_map),
        })
    }
}

/// Build a stable success envelope matching CapabilityResult.
pub fn ok_envelope(data: Value, provider_plugin_id: &str) -> Value {
    json!({
        "ok": true,
        "provider_plugin_id": provider_plugin_id,
        "data": data,
        "error": "",
    })
}

/// Validate a versioned capability identifier (`<ns>.<name>.v<n>`).
pub fn validate_capability_id(raw: &str) -> Result<&str, RpcError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed != raw || trimmed.len() > 128 {
        return Err(RpcError::invalid_argument(
            "Capability ID must be a non-empty trimmed identifier (<= 128 chars)",
        ));
    }
    let parts: Vec<&str> = trimmed.split('.').collect();
    if parts.len() < 3 {
        return Err(RpcError::invalid_argument(format!(
            "Invalid capability ID '{trimmed}': expected '<ns>.<name>.v<n>'"
        )));
    }
    let (version, base_parts) = parts.split_last().unwrap();
    if !version.starts_with('v') || version.len() < 2 || !version[1..].bytes().all(|b| b.is_ascii_digit()) {
        return Err(RpcError::invalid_argument(format!(
            "Invalid capability ID '{trimmed}': expected version suffix '.v<n>'"
        )));
    }
    for part in base_parts {
        if part.is_empty() || !part.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_') {
            return Err(RpcError::invalid_argument(format!(
                "Invalid capability ID '{trimmed}': segments must match [a-z0-9_]+"
            )));
        }
    }
    Ok(trimmed)
}

/// Validate a plugin ID (`^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`, no traversal).
pub fn validate_plugin_id(raw: &str) -> Result<&str, RpcError> {
    if raw.is_empty() || raw.len() > 96 {
        return Err(RpcError::invalid_argument(
            "plugin_id must be 1..=96 characters",
        ));
    }
    if raw.contains("..") || raw.contains('/') || raw.contains('\\') || raw.contains('\0') {
        return Err(RpcError::invalid_argument(
            "plugin_id contains forbidden path traversal characters",
        ));
    }
    let bytes = raw.as_bytes();
    if !bytes[0].is_ascii_alphanumeric() {
        return Err(RpcError::invalid_argument(
            "plugin_id must start with an ASCII alphanumeric character",
        ));
    }
    if !bytes
        .iter()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
    {
        return Err(RpcError::invalid_argument(
            "plugin_id contains invalid characters",
        ));
    }
    Ok(raw)
}

/// Validate a domain source ID (`^[A-Za-z0-9][A-Za-z0-9._:-]{0,126}[A-Za-z0-9]?$`, no traversal).
pub fn validate_source_id(raw: &str) -> Result<&str, RpcError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed != raw || trimmed.len() > 128 {
        return Err(RpcError::invalid_argument(
            "source_id must be 1..=128 trimmed characters",
        ));
    }
    if trimmed.contains("..") || trimmed.contains('/') || trimmed.contains('\\') || trimmed.contains('\0') {
        return Err(RpcError::invalid_argument(
            "source_id contains forbidden path traversal characters",
        ));
    }
    let bytes = trimmed.as_bytes();
    if !bytes[0].is_ascii_alphanumeric() || !bytes[bytes.len() - 1].is_ascii_alphanumeric() {
        return Err(RpcError::invalid_argument(
            "source_id must start and end with an ASCII alphanumeric character",
        ));
    }
    if !bytes
        .iter()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-' | b':'))
    {
        return Err(RpcError::invalid_argument(
            "source_id may contain only [A-Za-z0-9._:-]",
        ));
    }
    Ok(trimmed)
}

/// Validate an EntityBus topic name (`^[A-Za-z0-9._-]+$`, no `..`).
pub fn validate_topic(raw: &str) -> Result<&str, RpcError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed != raw || trimmed.len() > 128 || trimmed.contains("..") {
        return Err(RpcError::invalid_argument(
            "Event topic must be 1..=128 chars without whitespace or '..'",
        ));
    }
    let bytes = trimmed.as_bytes();
    if !bytes[0].is_ascii_alphanumeric() || !bytes[bytes.len() - 1].is_ascii_alphanumeric() {
        return Err(RpcError::invalid_argument(
            "Event topic must start and end with an alphanumeric character",
        ));
    }
    if !bytes
        .iter()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
    {
        return Err(RpcError::invalid_argument(
            "Event topic may contain only [A-Za-z0-9._-]",
        ));
    }
    Ok(trimmed)
}

/// Validate a control.sock RPC operation name against the safe `ALLOWED_CONTROL_OPS` whitelist.
pub fn validate_op(raw: &str) -> Result<&str, RpcError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed != raw || trimmed.len() > 64 {
        return Err(RpcError::invalid_argument(
            "RPC op must be a non-empty identifier (<= 64 chars)",
        ));
    }
    for seg in trimmed.split('.') {
        if seg.is_empty() || !seg.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_') {
            return Err(RpcError::invalid_argument(format!(
                "Invalid RPC operation '{trimmed}'"
            )));
        }
    }
    if !ALLOWED_CONTROL_OPS.contains(&trimmed) {
        return Err(RpcError::invalid_argument(format!(
            "Unsupported or disallowed RPC operation '{trimmed}'"
        )));
    }
    Ok(trimmed)
}

/// Map a domain name for `read_source` onto its standard capability ID.
pub fn resolve_domain_source_capability(domain: &str) -> Result<(&'static str, &'static str), RpcError> {
    match domain.trim().to_ascii_lowercase().as_str() {
        "media" => Ok(("media", "media.source.v1")),
        "text" => Ok(("text", "text.source.v1")),
        "book" => Ok(("book", "book.source.v1")),
        "recommend" | "core" => Ok(("recommend", "recommend.feed.v1")),
        _ => Err(RpcError::invalid_argument(format!(
            "Unsupported domain '{domain}'; expected 'media', 'text', 'book', or 'recommend'"
        ))),
    }
}

/// Normalize an optional payload into a JSON object map (rejecting non-object values).
pub fn normalize_object_payload(payload: Option<Value>) -> Result<Map<String, Value>, RpcError> {
    match payload {
        None | Some(Value::Null) => Ok(Map::new()),
        Some(Value::Object(map)) => Ok(map),
        Some(_) => Err(RpcError::invalid_argument(
            "Payload must be a JSON object or null",
        )),
    }
}

/// Validate optional timeout in milliseconds against safe bounds.
pub fn validate_timeout_ms(timeout_ms: Option<u64>) -> Result<Duration, RpcError> {
    let ms = timeout_ms.unwrap_or(DEFAULT_TIMEOUT_MS);
    if !(MIN_TIMEOUT_MS..=MAX_TIMEOUT_MS).contains(&ms) {
        return Err(RpcError::invalid_argument(format!(
            "timeout_ms must be between {MIN_TIMEOUT_MS} and {MAX_TIMEOUT_MS}"
        )));
    }
    Ok(Duration::from_millis(ms))
}

/// Parse and normalize a single control.sock NDJSON response frame.
pub fn parse_control_response(raw: &[u8], expected_req_id: &str) -> Result<Value, RpcError> {
    let trimmed = std::str::from_utf8(raw)
        .map_err(|_| RpcError::internal_error("Control response was not valid UTF-8"))?
        .trim();
    if trimmed.is_empty() {
        return Err(RpcError::internal_error("Empty response from control.sock"));
    }
    let parsed: Value = serde_json::from_str(trimmed)
        .map_err(|exc| RpcError::internal_error(format!("Malformed JSON response from control.sock: {exc}")))?;
    let obj = parsed
        .as_object()
        .ok_or_else(|| RpcError::internal_error("Control response must be a JSON object"))?;

    let ok = obj
        .get("ok")
        .and_then(Value::as_bool)
        .ok_or_else(|| RpcError::internal_error("Control response missing boolean 'ok' field"))?;

    match obj.get("id") {
        Some(Value::String(id_str)) if id_str == expected_req_id => {}
        None | Some(Value::Null) if !ok => {}
        Some(Value::Null) | None => {
            return Err(RpcError::internal_error(
                "Missing response id from control.sock",
            ));
        }
        Some(_) => {
            return Err(RpcError::internal_error(
                "Mismatched response id from control.sock",
            ));
        }
    }

    let provider_plugin_id = obj
        .get("provider_plugin_id")
        .and_then(Value::as_str)
        .unwrap_or("");

    if ok {
        let data = match obj.get("data") {
            None | Some(Value::Null) => json!({}),
            Some(Value::Object(_)) => obj.get("data").cloned().unwrap(),
            Some(_) => {
                return Err(RpcError::internal_error(
                    "Control response 'data' must be a JSON object",
                ));
            }
        };
        return Ok(ok_envelope(data, provider_plugin_id));
    }

    let error_code = obj
        .get("error_code")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .or_else(|| {
            obj.get("data")
                .and_then(|d| d.get("error_code"))
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
        })
        .or_else(|| {
            obj.get("error_detail")
                .and_then(|d| d.get("code"))
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
        })
        .unwrap_or("plugin_fault");

    let error_msg = obj
        .get("error")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("Control request failed");

    let err = RpcError {
        code: error_code.to_string(),
        message: redact_text(error_msg),
        provider_plugin_id: provider_plugin_id.to_string(),
        data: obj.get("data").cloned(),
    };
    Ok(err.to_envelope())
}

/// Check Tauri v2 event name validity (`[A-Za-z0-9-_/:]`, dots are rejected by Tauri).
pub fn is_valid_tauri_event_name(event: &str) -> bool {
    !event.is_empty()
        && event
            .chars()
            .all(|c| c.is_alphanumeric() || matches!(c, '-' | '/' | ':' | '_'))
}

/// Convert an EntityBus dot-separated topic (e.g. `entity.media.open`) into a valid Tauri event name.
pub fn tauri_event_name_for_topic(topic: &str) -> Option<String> {
    if validate_topic(topic).is_err() {
        return None;
    }
    let mapped = format!("omnidesk:topic:{}", topic.replace('.', ":"));
    if is_valid_tauri_event_name(&mapped) {
        Some(mapped)
    } else {
        None
    }
}
