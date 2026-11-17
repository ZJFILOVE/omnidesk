//! Background EntityBus / control.sock event forwarding with honest degraded state reporting.

use std::future::Future;
use std::pin::pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, RwLock};
use std::task::Poll;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};
use tokio::io::AsyncWriteExt;
use tokio::net::UnixStream;
use tokio::sync::Notify;

use crate::control_client::{map_connect_io_error, next_request_id, NdjsonFrameReader};
use crate::paths::{verify_peer_uid, OmnideskPaths};
use crate::protocol::{
    parse_control_response, tauri_event_name_for_topic, validate_topic, MAX_PAYLOAD_BYTES,
};
use crate::redact::{redact_json_value, redact_text};

pub const BUS_EVENT_CHANNEL: &str = "omnidesk:bus";
pub const BRIDGE_STATUS_CHANNEL: &str = "omnidesk:bridge-status";

/// Destination abstraction allowing unit tests to capture emitted Tauri events without a GUI.
pub trait EventSink: Send + Sync + 'static {
    fn emit_event(&self, event: &str, payload: Value);
}

impl EventSink for AppHandle {
    fn emit_event(&self, event: &str, payload: Value) {
        let _ = self.emit(event, payload);
    }
}

/// Snapshot of the EntityBus background forwarding state.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct EventBridgeSnapshot {
    pub status: String,
    pub supported: bool,
    pub reason: String,
    pub socket_path: Option<String>,
    pub forwarded_count: u64,
    pub last_event_topic: Option<String>,
    pub last_checked_epoch_ms: u64,
    pub bus_event_channel: &'static str,
    pub status_event_channel: &'static str,
}

impl Default for EventBridgeSnapshot {
    fn default() -> Self {
        Self {
            status: "offline".to_string(),
            supported: false,
            reason: "Event bridge has not probed control.sock yet".to_string(),
            socket_path: None,
            forwarded_count: 0,
            last_event_topic: None,
            last_checked_epoch_ms: 0,
            bus_event_channel: BUS_EVENT_CHANNEL,
            status_event_channel: BRIDGE_STATUS_CHANNEL,
        }
    }
}

fn now_epoch_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

async fn await_or_stop<F: Future>(stop_flag: &AtomicBool, stop_notify: &Notify, fut: F) -> Option<F::Output> {
    let mut stopped = pin!(stop_notify.notified());
    stopped.as_mut().enable();
    if stop_flag.load(Ordering::Acquire) {
        return None;
    }
    let mut fut = pin!(fut);
    std::future::poll_fn(|cx| {
        if stop_flag.load(Ordering::Acquire) {
            return Poll::Ready(None);
        }
        if let Poll::Ready(val) = fut.as_mut().poll(cx) {
            return Poll::Ready(Some(val));
        }
        if stopped.as_mut().poll(cx).is_ready() {
            return Poll::Ready(None);
        }
        Poll::Pending
    })
    .await
}

/// Background worker probing control.sock for EntityBus subscription and forwarding events to Tauri.
pub struct EventBridge {
    paths: OmnideskPaths,
    max_payload_bytes: usize,
    snapshot: RwLock<EventBridgeSnapshot>,
    shutdown: AtomicBool,
    wake: Notify,
    stop_notify: Notify,
}

impl EventBridge {
    pub fn new(paths: OmnideskPaths) -> Arc<Self> {
        Self::with_max_bytes(paths, MAX_PAYLOAD_BYTES)
    }

    pub fn with_max_bytes(paths: OmnideskPaths, max_payload_bytes: usize) -> Arc<Self> {
        Arc::new(Self {
            paths,
            max_payload_bytes,
            snapshot: RwLock::new(EventBridgeSnapshot::default()),
            shutdown: AtomicBool::new(false),
            wake: Notify::new(),
            stop_notify: Notify::new(),
        })
    }

    pub fn snapshot(&self) -> EventBridgeSnapshot {
        self.snapshot
            .read()
            .map(|guard| guard.clone())
            .unwrap_or_default()
    }

    pub fn notify_wakeup(&self) {
        self.wake.notify_one();
    }

    pub fn stop(&self) {
        self.shutdown.store(true, Ordering::Release);
        self.stop_notify.notify_waiters();
        self.wake.notify_waiters();
    }

    /// Spawn a dedicated background OS thread running the async event-bridge loop.
    pub fn start(self: &Arc<Self>, sink: Arc<dyn EventSink>) {
        let bridge = Arc::clone(self);
        let _ = std::thread::Builder::new()
            .name("omnidesk-event-bridge".to_string())
            .spawn(move || {
                let Ok(rt) = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                else {
                    return;
                };
                rt.block_on(bridge.run_loop(sink));
            });
    }

    async fn run_loop(self: Arc<Self>, sink: Arc<dyn EventSink>) {
        while !self.shutdown.load(Ordering::Acquire) {
            let backoff = self.step_once(sink.as_ref()).await;
            if self.shutdown.load(Ordering::Acquire) {
                break;
            }
            let _ = await_or_stop(
                &self.shutdown,
                &self.stop_notify,
                tokio::time::timeout(backoff, self.wake.notified()),
            )
            .await;
        }
    }

    /// Execute one subscription probe / streaming cycle; returns recommended delay before retry.
    pub async fn step_once(&self, sink: &dyn EventSink) -> Duration {
        let sock_path = match self.paths.resolve_active_socket() {
            Ok(path) => path,
            Err(err) => {
                let status = if err.code == "service_offline" { "offline" } else { "degraded" };
                self.update_status(status, false, &err.message, None, sink);
                return Duration::from_secs(if status == "offline" { 5 } else { 10 });
            }
        };

        let sock_str = sock_path.display().to_string();
        let req_id = next_request_id();
        let req_bytes = match serde_json::to_vec(&json!({
            "id": req_id,
            "op": "bus.subscribe",
            "topics": ["*"],
            "caller": "shell-tauri",
        })) {
            Ok(mut bytes) => {
                bytes.push(b'\n');
                bytes
            }
            Err(err) => {
                self.update_status("degraded", false, &format!("Failed to build subscribe frame: {err}"), Some(sock_str), sink);
                return Duration::from_secs(10);
            }
        };

        let mut stream = match tokio::time::timeout(Duration::from_secs(3), UnixStream::connect(&sock_path)).await {
            Ok(Ok(s)) => s,
            Ok(Err(err)) => {
                let mapped = map_connect_io_error(&sock_path, err);
                let st = if mapped.code == "service_offline" { "offline" } else { "degraded" };
                self.update_status(st, false, &mapped.message, None, sink);
                return Duration::from_secs(5);
            }
            Err(_) => {
                self.update_status("degraded", false, "Timed out connecting to control.sock for events", Some(sock_str), sink);
                return Duration::from_secs(5);
            }
        };

        if let Err(err) = verify_peer_uid(&stream, &sock_path) {
            self.update_status("degraded", false, &err.message, Some(sock_str), sink);
            return Duration::from_secs(10);
        }

        if let Err(err) = stream.write_all(&req_bytes).await {
            self.update_status("degraded", false, &redact_text(&format!("Failed writing bus.subscribe: {err}")), Some(sock_str), sink);
            return Duration::from_secs(5);
        }
        let _ = stream.flush().await;

        let mut reader = NdjsonFrameReader::new(self.max_payload_bytes);
        let first_frame = match await_or_stop(
            &self.shutdown,
            &self.stop_notify,
            tokio::time::timeout(Duration::from_secs(5), reader.read_frame(&mut stream)),
        )
        .await
        {
            None => return Duration::from_millis(0),
            Some(Ok(Ok(Some(frame)))) => frame,
            Some(Ok(Ok(None))) => {
                self.update_status("degraded", false, "Control socket closed before acknowledging bus.subscribe", Some(sock_str), sink);
                return Duration::from_secs(10);
            }
            Some(Ok(Err(err))) => {
                self.update_status("degraded", false, &err.message, Some(sock_str), sink);
                return Duration::from_secs(10);
            }
            Some(Err(_)) => {
                self.update_status("degraded", false, "Timed out waiting for bus.subscribe response", Some(sock_str), sink);
                return Duration::from_secs(10);
            }
        };

        let ack = match parse_control_response(&first_frame, &req_id) {
            Ok(val) => val,
            Err(err) => {
                self.update_status("degraded", false, &err.message, Some(sock_str), sink);
                return Duration::from_secs(10);
            }
        };

        if ack.get("ok").and_then(Value::as_bool) != Some(true) {
            let err_code = ack.get("error_code").and_then(Value::as_str).unwrap_or("unknown");
            let err_msg = ack.get("error").and_then(Value::as_str).unwrap_or("Subscription rejected");
            let reason = if err_code == "unknown_operation" {
                "ControlServer does not support EntityBus streaming subscription (unknown_operation: bus.subscribe)".to_string()
            } else {
                redact_text(err_msg)
            };
            self.update_status("degraded", false, &reason, Some(sock_str), sink);
            return Duration::from_secs(30);
        }

        self.update_status("connected", true, "Subscribed to EntityBus event stream", Some(sock_str.clone()), sink);

        loop {
            match await_or_stop(&self.shutdown, &self.stop_notify, reader.read_frame(&mut stream)).await {
                None => return Duration::from_millis(0),
                Some(Ok(Some(frame))) => {
                    if !frame.iter().all(|b| b.is_ascii_whitespace()) {
                        self.forward_raw_frame(&frame, sink);
                    }
                }
                Some(Ok(None)) => {
                    self.update_status("offline", false, "EntityBus stream closed by server", Some(sock_str), sink);
                    return Duration::from_secs(2);
                }
                Some(Err(err)) => {
                    self.update_status("degraded", false, &err.message, Some(sock_str), sink);
                    return Duration::from_secs(5);
                }
            }
        }
    }

    fn forward_raw_frame(&self, frame: &[u8], sink: &dyn EventSink) {
        let Ok(parsed) = serde_json::from_slice::<Value>(frame) else {
            return;
        };
        let Some(obj) = parsed.as_object() else {
            return;
        };
        let params_obj = obj.get("params").and_then(Value::as_object);
        let raw_topic = obj
            .get("topic")
            .and_then(Value::as_str)
            .or_else(|| params_obj.and_then(|p| p.get("topic")).and_then(Value::as_str));
        let Some(topic_str) = raw_topic else {
            return;
        };
        let Ok(valid_topic) = validate_topic(topic_str) else {
            return;
        };

        let raw_payload = obj
            .get("payload")
            .or_else(|| params_obj.and_then(|p| p.get("payload")))
            .cloned()
            .unwrap_or_else(|| json!({}));
        let safe_payload = redact_json_value(&raw_payload);

        let event_id = obj
            .get("event_id")
            .and_then(Value::as_str)
            .or_else(|| params_obj.and_then(|p| p.get("event_id")).and_then(Value::as_str))
            .unwrap_or("");
        let source_plugin_id = obj
            .get("source_plugin_id")
            .and_then(Value::as_str)
            .or_else(|| params_obj.and_then(|p| p.get("source_plugin_id")).and_then(Value::as_str))
            .unwrap_or("");
        let timestamp = obj
            .get("timestamp")
            .filter(|v| !v.is_null())
            .or_else(|| params_obj.and_then(|p| p.get("timestamp")))
            .cloned()
            .unwrap_or(Value::Null);

        let envelope = json!({
            "topic": valid_topic,
            "payload": safe_payload,
            "event_id": event_id,
            "source_plugin_id": source_plugin_id,
            "timestamp": timestamp,
        });

        sink.emit_event(BUS_EVENT_CHANNEL, envelope.clone());
        if let Some(topic_event) = tauri_event_name_for_topic(valid_topic) {
            sink.emit_event(&topic_event, envelope);
        }

        if let Ok(mut guard) = self.snapshot.write() {
            guard.forwarded_count += 1;
            guard.last_event_topic = Some(valid_topic.to_string());
            guard.last_checked_epoch_ms = now_epoch_ms();
        }
    }

    fn update_status(
        &self,
        status: &str,
        supported: bool,
        reason: &str,
        socket_path: Option<String>,
        sink: &dyn EventSink,
    ) {
        let safe_reason = redact_text(reason);
        let changed_snap = if let Ok(mut guard) = self.snapshot.write() {
            let changed = guard.status != status
                || guard.supported != supported
                || guard.reason != safe_reason
                || guard.socket_path != socket_path;
            guard.status = status.to_string();
            guard.supported = supported;
            guard.reason = safe_reason;
            guard.socket_path = socket_path;
            guard.last_checked_epoch_ms = now_epoch_ms();
            if changed {
                Some(guard.clone())
            } else {
                None
            }
        } else {
            None
        };

        if let Some(snap) = changed_snap {
            if let Ok(val) = serde_json::to_value(snap) {
                sink.emit_event(BRIDGE_STATUS_CHANNEL, val);
            }
        }
    }
}
