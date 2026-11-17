use std::fs;
use std::future::Future;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use omnidesk_lib::commands::{exec_core_status, exec_list_plugins, exec_read_source};
use omnidesk_lib::control_client::ControlClient;
use omnidesk_lib::events::{EventBridge, EventSink, BRIDGE_STATUS_CHANNEL, BUS_EVENT_CHANNEL};
use omnidesk_lib::paths::OmnideskPaths;
use omnidesk_lib::protocol::MAX_PAYLOAD_BYTES;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixListener;

struct TempXdg {
    root: PathBuf,
    paths: OmnideskPaths,
    socket_path: PathBuf,
}

impl TempXdg {
    fn new(prefix: &str, use_data_fallback: bool) -> Self {
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let root = std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()));
        let cfg = root.join("cfg").join("omnidesk");
        let data = root.join("data").join("omnidesk");
        fs::create_dir_all(&cfg).unwrap();
        fs::set_permissions(&cfg, fs::Permissions::from_mode(0o700)).unwrap();

        let (rt_opt, sock) = if use_data_fallback {
            let run = data.join("run");
            fs::create_dir_all(&run).unwrap();
            fs::set_permissions(&data, fs::Permissions::from_mode(0o700)).unwrap();
            fs::set_permissions(&run, fs::Permissions::from_mode(0o700)).unwrap();
            (None, run.join("control.sock"))
        } else {
            let rt = root.join("rt").join("omnidesk");
            fs::create_dir_all(&rt).unwrap();
            fs::set_permissions(&rt, fs::Permissions::from_mode(0o700)).unwrap();
            (Some(rt.clone()), rt.join("control.sock"))
        };

        let paths = OmnideskPaths::for_dirs(rt_opt, data, cfg);
        Self { root, paths, socket_path: sock }
    }
}

impl Drop for TempXdg {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

#[derive(Default)]
struct RecordingSink(Mutex<Vec<(String, Value)>>);

impl RecordingSink {
    fn events(&self) -> Vec<(String, Value)> {
        self.0.lock().unwrap().clone()
    }
}

impl EventSink for RecordingSink {
    fn emit_event(&self, event: &str, payload: Value) {
        self.0.lock().unwrap().push((event.to_string(), payload));
    }
}

fn block_on<F: Future>(fut: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(fut)
}

fn bind_0600_listener(sock_path: &std::path::Path) -> UnixListener {
    let listener = UnixListener::bind(sock_path).unwrap();
    fs::set_permissions(sock_path, fs::Permissions::from_mode(0o600)).unwrap();
    listener
}

#[test]
fn offline_socket_returns_service_offline_never_fakes_online() {
    block_on(async {
        let env = TempXdg::new("omnidesk-offline", false);
        let client = ControlClient::new(env.paths.clone());
        let bridge = EventBridge::new(env.paths.clone());
        let sink = RecordingSink::default();

        let status = exec_core_status(&client, &bridge).await;
        assert_eq!(status["ok"], false);
        assert_eq!(status["error_code"], "service_offline");
        assert_eq!(status["data"]["online"], false);
        assert_eq!(status["data"]["degraded"], true);

        let cap = client.call_capability("demo.echo.v1", Some(json!({ "text": "hi" })), None).await;
        assert_eq!(cap["ok"], false);
        assert_eq!(cap["error_code"], "service_offline");

        let ev = client.emit_event("entity.media.open", Some(json!({ "item_id": "m1" })), None).await;
        assert_eq!(ev["ok"], false);
        assert_eq!(ev["error_code"], "service_offline");

        let src = exec_read_source(&client, "media", None, None, None).await;
        assert_eq!(src["ok"], false);
        assert_eq!(src["error_code"], "service_offline");

        let live_plugins = exec_list_plugins(&client, Some(true)).await;
        assert_eq!(live_plugins["ok"], false);
        assert_eq!(live_plugins["error_code"], "service_offline");

        let fallback_plugins = exec_list_plugins(&client, None).await;
        assert_eq!(fallback_plugins["ok"], true);
        assert_eq!(fallback_plugins["data"]["core_online"], false);
        assert_eq!(fallback_plugins["data"]["degraded"], true);
        assert_eq!(fallback_plugins["data"]["source"], "manifest_scan");

        let _ = bridge.step_once(&sink).await;
        assert_eq!(bridge.snapshot().status, "offline");
        assert_eq!(sink.events().iter().filter(|(name, _)| name == BUS_EVENT_CHANNEL).count(), 0);
    });
}

#[test]
fn rpc_roundtrip_over_data_fallback_socket() {
    block_on(async {
        let env = TempXdg::new("omnidesk-rpc", true);
        let listener = bind_0600_listener(&env.socket_path);

        let server = tokio::spawn(async move {
            for _ in 0..6 {
                let (stream, _) = listener.accept().await.unwrap();
                let (read_half, mut write_half) = stream.into_split();
                let mut reader = BufReader::new(read_half);
                let mut line = String::new();
                reader.read_line(&mut line).await.unwrap();
                let req: Value = serde_json::from_str(&line).unwrap();
                let req_id = req["id"].as_str().unwrap().to_string();
                let op = req["op"].as_str().unwrap();

                let resp = match op {
                    "ping" => json!({"id": req_id, "ok": true, "provider_plugin_id": "", "data": {"status": "ok", "capabilities": 2, "plugins": 1}, "error": ""}),
                    "plugin.list" => json!({"id": req_id, "ok": true, "provider_plugin_id": "", "data": {"plugins": [{"plugin_id": "plugin-demo", "state": "loaded"}]}, "error": ""}),
                    "capability.call" => json!({"id": req_id, "ok": true, "provider_plugin_id": "plugin-demo", "data": {"cap": req["capability"], "echo": req["payload"]}, "error": ""}),
                    "bus.publish" => json!({"id": req_id, "ok": true, "provider_plugin_id": "", "data": {"event_id": "e-1", "topic": req["topic"], "delivered": 1, "failed": 0}, "error": ""}),
                    "plugin.reload" => json!({"id": req_id, "ok": true, "provider_plugin_id": req["plugin_id"], "data": {"plugin_id": req["plugin_id"], "state": "loaded", "reload_count": 1}, "error": ""}),
                    _ => json!({"id": req_id, "ok": false, "error_code": "unknown_operation", "error": "unknown", "data": {}}),
                };
                write_half.write_all(format!("{resp}\n").as_bytes()).await.unwrap();
            }
        });

        let client = ControlClient::new(env.paths.clone());
        let bridge = EventBridge::new(env.paths.clone());

        let status = exec_core_status(&client, &bridge).await;
        assert_eq!(status["ok"], true);
        assert_eq!(status["data"]["online"], true);
        assert_eq!(status["data"]["capabilities"], 2);

        let plugins = exec_list_plugins(&client, None).await;
        assert_eq!(plugins["ok"], true);
        assert_eq!(plugins["data"]["source"], "control_socket");
        assert_eq!(plugins["data"]["plugins"][0]["plugin_id"], "plugin-demo");

        let cap = client.call_capability("demo.echo.v1", Some(json!({ "text": "hello" })), None).await;
        assert_eq!(cap["ok"], true);
        assert_eq!(cap["provider_plugin_id"], "plugin-demo");
        assert_eq!(cap["data"]["echo"]["text"], "hello");

        let src = exec_read_source(&client, "media", Some("plugin-nas"), Some(json!({"action": "list"})), None).await;
        assert_eq!(src["ok"], true);
        assert_eq!(src["data"]["cap"], "media.source.v1");
        assert_eq!(src["data"]["echo"]["source_id"], "plugin-nas");

        let pub_res = client.emit_event("entity.media.open", Some(json!({"item_id": "v1"})), None).await;
        assert_eq!(pub_res["ok"], true);
        assert_eq!(pub_res["data"]["delivered"], 1);

        let reload = client.call_raw("plugin.reload", Some(json!({"plugin_id": "plugin-demo"})), None).await;
        assert_eq!(reload["ok"], true);
        assert_eq!(reload["data"]["reload_count"], 1);

        // Input validation prevents invalid calls before touching control.sock
        assert_eq!(exec_read_source(&client, "bad_domain", None, None, None).await["error_code"], "invalid_argument");
        assert_eq!(exec_read_source(&client, "text", Some("../escape"), None, None).await["error_code"], "invalid_argument");
        assert_eq!(client.call_raw("plugin.reload", Some(json!({"plugin_id": "../bad"})), None).await["error_code"], "invalid_argument");
        assert_eq!(client.call_raw("capability.call", Some(json!({"capability": "bad"})), None).await["error_code"], "invalid_argument");

        server.await.unwrap();
    });
}

#[test]
fn timeout_oversized_and_error_redaction_mapping() {
    block_on(async {
        let env = TempXdg::new("omnidesk-err", false);
        let listener = bind_0600_listener(&env.socket_path);

        let server = tokio::spawn(async move {
            // 1. Timeout test: accept and delay longer than client timeout
            let (s1, _) = listener.accept().await.unwrap();
            tokio::time::sleep(Duration::from_millis(350)).await;
            drop(s1);

            // 2. Server error with secret leak -> client must scrub secret
            let (s2, _) = listener.accept().await.unwrap();
            let (r2, mut w2) = s2.into_split();
            let mut br2 = BufReader::new(r2);
            let mut l2 = String::new();
            br2.read_line(&mut l2).await.unwrap();
            let req2: Value = serde_json::from_str(&l2).unwrap();
            let resp2 = json!({
                "id": req2["id"],
                "ok": false,
                "provider_plugin_id": "plugin-faulty",
                "error_code": "plugin_fault",
                "error": "Upstream failed with Bearer secret-token-abc123",
                "data": { "error_code": "plugin_fault", "cookie": "sid=leak" }
            });
            w2.write_all(format!("{resp2}\n").as_bytes()).await.unwrap();

            // 3. Oversized > 8 MiB response
            let (s3, _) = listener.accept().await.unwrap();
            let (r3, mut w3) = s3.into_split();
            let mut br3 = BufReader::new(r3);
            let mut l3 = String::new();
            br3.read_line(&mut l3).await.unwrap();
            let huge = format!("{{\"ok\":true,\"data\":{{\"pad\":\"{}\"}}}}\n", "x".repeat(MAX_PAYLOAD_BYTES + 128));
            let _ = w3.write_all(huge.as_bytes()).await;

            // 4. Mismatched response ID
            let (s4, _) = listener.accept().await.unwrap();
            let (r4, mut w4) = s4.into_split();
            let mut br4 = BufReader::new(r4);
            let mut l4 = String::new();
            br4.read_line(&mut l4).await.unwrap();
            let _ = w4.write_all(b"{\"id\":\"wrong-id\",\"ok\":true,\"data\":{}}\n").await;
        });

        let short_client = ControlClient::with_limits(env.paths.clone(), Duration::from_millis(120), MAX_PAYLOAD_BYTES);
        let t_out = short_client.call_capability("demo.echo.v1", None, Some(120)).await;
        assert_eq!(t_out["ok"], false);
        assert_eq!(t_out["error_code"], "timeout");

        let fault = short_client.call_capability("demo.echo.v1", None, Some(2000)).await;
        assert_eq!(fault["ok"], false);
        assert_eq!(fault["error_code"], "plugin_fault");
        assert!(!fault["error"].as_str().unwrap().contains("secret-token-abc123"));
        assert_eq!(fault["data"]["cookie"], "[REDACTED]");

        let oversized_resp = short_client.call_capability("demo.echo.v1", None, Some(5000)).await;
        assert_eq!(oversized_resp["ok"], false);
        assert_eq!(oversized_resp["error_code"], "internal_error");

        let bad_id = short_client.call_capability("demo.echo.v1", None, Some(2000)).await;
        assert_eq!(bad_id["ok"], false);
        assert_eq!(bad_id["error_code"], "internal_error");

        // Outgoing request exceeding 8 MiB rejected locally before socket I/O
        let big_req = short_client
            .call_capability("demo.echo.v1", Some(json!({ "blob": "a".repeat(MAX_PAYLOAD_BYTES + 32) })), None)
            .await;
        assert_eq!(big_req["ok"], false);
        assert_eq!(big_req["error_code"], "invalid_argument");

        server.await.unwrap();
    });
}

#[test]
fn event_bridge_degraded_vs_streaming_forwarding() {
    block_on(async {
        let env = TempXdg::new("omnidesk-ev", false);
        let listener = bind_0600_listener(&env.socket_path);
        let sink = Arc::new(RecordingSink::default());

        let server = tokio::spawn(async move {
            // Connection 1: Core without bus.subscribe -> unknown_operation
            let (s1, _) = listener.accept().await.unwrap();
            let (r1, mut w1) = s1.into_split();
            let mut br1 = BufReader::new(r1);
            let mut l1 = String::new();
            br1.read_line(&mut l1).await.unwrap();
            let req1: Value = serde_json::from_str(&l1).unwrap();
            let err_ack = json!({
                "id": req1["id"],
                "ok": false,
                "error_code": "unknown_operation",
                "error": "Unknown control operation: bus.subscribe",
                "data": {"error_code": "unknown_operation"}
            });
            w1.write_all(format!("{err_ack}\n").as_bytes()).await.unwrap();

            // Connection 2: Streaming-capable server pushes ack + 2 events in one write
            let (s2, _) = listener.accept().await.unwrap();
            let (r2, mut w2) = s2.into_split();
            let mut br2 = BufReader::new(r2);
            let mut l2 = String::new();
            br2.read_line(&mut l2).await.unwrap();
            let req2: Value = serde_json::from_str(&l2).unwrap();
            assert_eq!(req2["op"], "bus.subscribe");

            let ack = json!({"id": req2["id"], "ok": true, "data": {"subscribed": true}});
            let ev1 = json!({
                "topic": "entity.media.open",
                "event_id": "evt-1",
                "source_plugin_id": "plugin-nas",
                "timestamp": 1700000000.0,
                "payload": {"item_id": "nas-1", "token": "must-not-leak"}
            });
            let ev2 = json!({
                "jsonrpc": "2.0",
                "method": "bus.event",
                "params": {
                    "topic": "asr.job.progress",
                    "event_id": "evt-2",
                    "source_plugin_id": "plugin-asr",
                    "timestamp": 1700000001.0,
                    "payload": {"job_id": "j1", "progress": 0.5}
                }
            });
            let batch = format!("{ack}\n{ev1}\n{ev2}\n");
            w2.write_all(batch.as_bytes()).await.unwrap();
            w2.shutdown().await.unwrap();
        });

        let bridge = EventBridge::new(env.paths.clone());

        // Cycle 1 -> degraded with zero fake bus events
        let retry = bridge.step_once(sink.as_ref()).await;
        assert_eq!(retry, Duration::from_secs(30));
        let snap1 = bridge.snapshot();
        assert_eq!(snap1.status, "degraded");
        assert!(!snap1.supported);
        assert_eq!(snap1.forwarded_count, 0);

        // Cycle 2 -> subscribed, forwards 2 real events, scrubs secret token
        let _ = bridge.step_once(sink.as_ref()).await;
        let snap2 = bridge.snapshot();
        assert_eq!(snap2.forwarded_count, 2);
        assert_eq!(snap2.last_event_topic.as_deref(), Some("asr.job.progress"));

        let events = sink.events();
        assert!(events.iter().any(|(name, v)| name == BRIDGE_STATUS_CHANNEL && v["status"] == "degraded"));
        assert!(events.iter().any(|(name, v)| name == BRIDGE_STATUS_CHANNEL && v["status"] == "connected"));

        let topic_ev = events.iter().find(|(n, _)| n == "omnidesk:topic:entity:media:open").map(|(_, v)| v).unwrap();
        assert_eq!(topic_ev["payload"]["item_id"], "nas-1");
        assert_eq!(topic_ev["payload"]["token"], "[REDACTED]");
        let asr_ev = events.iter().find(|(n, _)| n == "omnidesk:topic:asr:job:progress").map(|(_, v)| v).unwrap();
        assert_eq!(asr_ev["event_id"], "evt-2");
        assert_eq!(asr_ev["source_plugin_id"], "plugin-asr");
        assert_eq!(asr_ev["timestamp"], 1700000001.0);
        assert_eq!(events.iter().filter(|(name, _)| name == BUS_EVENT_CHANNEL).count(), 2);

        server.await.unwrap();
    });
}

#[test]
fn event_bridge_stop_wakes_idle_subscription_promptly() {
    block_on(async {
        let env = TempXdg::new("omnidesk-stop-race", false);
        let listener = bind_0600_listener(&env.socket_path);
        let sink = Arc::new(RecordingSink::default());
        let (hold_tx, hold_rx) = tokio::sync::oneshot::channel::<()>();
        let server = tokio::spawn(async move {
            let (s, _) = listener.accept().await.unwrap();
            let (r, mut w) = s.into_split();
            let mut br = BufReader::new(r);
            let mut l = String::new();
            br.read_line(&mut l).await.unwrap();
            let req: Value = serde_json::from_str(&l).unwrap();
            let ack = json!({"id": req["id"], "ok": true, "data": {"subscribed": true}});
            w.write_all(format!("{ack}\n").as_bytes()).await.unwrap();
            let _ = hold_rx.await;
        });

        let bridge = EventBridge::new(env.paths.clone());
        let b2 = Arc::clone(&bridge);
        let s2 = Arc::clone(&sink);
        let task = tokio::spawn(async move { b2.step_once(s2.as_ref()).await });
        for _ in 0..100 {
            if bridge.snapshot().status == "connected" { break; }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert_eq!(bridge.snapshot().status, "connected");
        bridge.stop();
        assert!(tokio::time::timeout(Duration::from_secs(2), task).await.is_ok());
        let _ = hold_tx.send(());
        server.await.unwrap();
    });
}
