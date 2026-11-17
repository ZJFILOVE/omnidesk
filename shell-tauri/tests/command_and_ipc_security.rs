use std::fs;
use std::future::Future;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use omnidesk_lib::commands::{
    exec_call_args, exec_call_capability_args, exec_command_invoke_body, exec_core_status,
    exec_emit_event_args, exec_list_plugins_args, exec_read_source, exec_read_source_args,
    RawJsonArgs,
};
use omnidesk_lib::control_client::ControlClient;
use omnidesk_lib::events::{EventBridge, EventSink};
use omnidesk_lib::paths::OmnideskPaths;
use serde_json::{json, Value};
use tauri::ipc::InvokeBody;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixListener;

struct TempEnv {
    root: PathBuf,
    paths: OmnideskPaths,
    socket_path: PathBuf,
}

impl TempEnv {
    fn new(prefix: &str, missing_rt_with_fallback: bool) -> Self {
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let root = std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()));
        let cfg = root.join("cfg").join("omnidesk");
        let data = root.join("data").join("omnidesk");
        let run = data.join("run");
        fs::create_dir_all(&cfg).unwrap();
        fs::create_dir_all(&run).unwrap();
        fs::set_permissions(&cfg, fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(&data, fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(&run, fs::Permissions::from_mode(0o700)).unwrap();

        let rt = if missing_rt_with_fallback {
            Some(root.join("nonexistent-rt").join("omnidesk"))
        } else {
            None
        };
        let sock = run.join("control.sock");
        let paths = OmnideskPaths::for_dirs(rt, data, cfg);
        Self { root, paths, socket_path: sock }
    }
}

impl Drop for TempEnv {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

#[derive(Default)]
struct NullSink(Mutex<usize>);
impl EventSink for NullSink {
    fn emit_event(&self, _event: &str, _payload: Value) {
        *self.0.lock().unwrap() += 1;
    }
}

fn block_on<F: Future>(fut: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(fut)
}

fn bind_0600(sock_path: &std::path::Path) -> UnixListener {
    let listener = UnixListener::bind(sock_path).unwrap();
    fs::set_permissions(sock_path, fs::Permissions::from_mode(0o600)).unwrap();
    listener
}

#[test]
fn web_cookies_plaintext_reads_blocked_and_responses_scrubbed() {
    block_on(async {
        let env = TempEnv::new("omnidesk-cookies", false);
        let listener = bind_0600(&env.socket_path);
        let server = tokio::spawn(async move {
            let (s, _) = listener.accept().await.unwrap();
            let (r, mut w) = s.into_split();
            let mut br = BufReader::new(r);
            let mut line = String::new();
            br.read_line(&mut line).await.unwrap();
            let req: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(req["capability"], "web.cookies.v1");
            assert_eq!(req["payload"]["action"], "list");
            let resp = json!({
                "id": req["id"], "ok": true, "provider_plugin_id": "plugin-chrome-web",
                "data": {
                    "domain": "example.com", "cookie_header": "sid=must_strip",
                    "cookies": [{"name": "sid", "value": "must_strip", "has_value": true}]
                }, "error": ""
            });
            w.write_all(format!("{resp}\n").as_bytes()).await.unwrap();
        });

        let client = ControlClient::new(env.paths.clone());
        for bad_call in [
            client.call_capability("web.cookies.v1", Some(json!({"action": "get_cookies", "url": "https://example.com"})), None).await,
            client.call_capability("web.cookies.v1", Some(json!({"action": "get", "domain": "example.com"})), None).await,
            client.call_capability("web.cookies.v1", Some(json!({"action": "GET_COOKIES"})), None).await,
            client.call_capability("web.cookies.v1", None, None).await,
            client.call_raw("capability.call", Some(json!({"capability": "web.cookies.v1", "payload": {"action": "get_cookies"}})), None).await,
            client.call_raw("capability.call", Some(json!({"capability_id": "web.cookies.v1", "request": {"action": "get"}})), None).await,
        ] {
            assert_eq!(bad_call["ok"], false);
            assert_eq!(bad_call["error_code"], "permission_denied");
        }

        let list_res = client.call_capability("web.cookies.v1", Some(json!({"action": "list", "domain": "example.com"})), None).await;
        assert_eq!(list_res["ok"], true);
        assert!(list_res["data"].get("cookie_header").is_none());
        assert!(list_res["data"]["cookies"][0].get("value").is_none());
        assert_eq!(list_res["data"]["cookies"][0]["name"], "sid");
        assert_eq!(list_res["data"]["cookies"][0]["has_value"], true);
        server.await.unwrap();
    });
}

#[test]
fn read_source_prevents_payload_injection_and_supports_colon_multi_source_ids() {
    block_on(async {
        let env = TempEnv::new("omnidesk-src-sec", false);
        let listener = bind_0600(&env.socket_path);
        let server = tokio::spawn(async move {
            for _ in 0..2 {
                let (s, _) = listener.accept().await.unwrap();
                let (r, mut w) = s.into_split();
                let mut br = BufReader::new(r);
                let mut line = String::new();
                br.read_line(&mut line).await.unwrap();
                let req: Value = serde_json::from_str(&line).unwrap();
                let resp = json!({
                    "id": req["id"], "ok": true, "provider_plugin_id": "core",
                    "data": {"cap": req["capability"], "payload": req["payload"]}, "error": ""
                });
                w.write_all(format!("{resp}\n").as_bytes()).await.unwrap();
            }
        });

        let client = ControlClient::new(env.paths.clone());
        assert_eq!(exec_read_source(&client, "media", None, Some(json!({"source_id": "../escape"})), None).await["error_code"], "invalid_argument");
        assert_eq!(exec_read_source(&client, "media", Some("plugin-nas"), Some(json!({"source_id": "../escape"})), None).await["error_code"], "invalid_argument");
        assert_eq!(exec_read_source(&client, "media", Some("plugin-nas"), Some(json!({"source_id": "other-nas"})), None).await["error_code"], "invalid_argument");
        assert_eq!(client.call_capability("media.source.v1", Some(json!({"source_id": "../escape"})), None).await["error_code"], "invalid_argument");

        let r1 = exec_read_source(&client, "media", Some("plugin-ai-curator:2"), Some(json!({"action": "list"})), None).await;
        assert_eq!(r1["ok"], true);
        assert_eq!(r1["data"]["payload"]["source_id"], "plugin-ai-curator:2");

        let r2 = exec_read_source(&client, "text", None, Some(json!({"source_id": "plugin-zhihu:1"})), None).await;
        assert_eq!(r2["ok"], true);
        assert_eq!(r2["data"]["cap"], "text.source.v1");
        assert_eq!(r2["data"]["payload"]["source_id"], "plugin-zhihu:1");
        server.await.unwrap();
    });
}

#[test]
fn call_raw_blocks_reserved_keys_disallowed_ops_and_fixes_bus_timeout() {
    block_on(async {
        let env = TempEnv::new("omnidesk-raw-sec", false);
        let listener = bind_0600(&env.socket_path);
        let server = tokio::spawn(async move {
            let (s, _) = listener.accept().await.unwrap();
            let (r, mut w) = s.into_split();
            let mut br = BufReader::new(r);
            let mut line = String::new();
            br.read_line(&mut line).await.unwrap();
            let req: Value = serde_json::from_str(&line).unwrap();
            assert!(req.get("jsonrpc").is_none() && req.get("method").is_none() && req.get("path").is_none());
            let resp = json!({"id": req["id"], "ok": true, "provider_plugin_id": "", "data": {"delivered": 1}, "error": ""});
            w.write_all(format!("{resp}\n").as_bytes()).await.unwrap();
        });

        let client = ControlClient::new(env.paths.clone());
        assert_eq!(client.call_raw("ping", Some(json!({"jsonrpc": "2.0"})), None).await["error_code"], "invalid_argument");
        assert_eq!(client.call_raw("ping", Some(json!({"method": "plugin.unload"})), None).await["error_code"], "invalid_argument");
        assert_eq!(client.call_raw("plugin.load", Some(json!({"plugin_id": "plugin-demo", "path": "/tmp/evil"})), None).await["error_code"], "invalid_argument");
        assert_eq!(client.call_raw("bus.subscribe", None, None).await["error_code"], "invalid_argument");
        assert_eq!(
            client.call_raw("ui.action", Some(json!({"kind": "dialog", "spec_id": "tab", "action_id": "open"})), None).await["error_code"],
            "invalid_argument"
        );
        assert_eq!(
            client.call_raw("ui.action", Some(json!({"kind": "workspace_tab", "spec_id": " ", "action_id": "open"})), None).await["error_code"],
            "invalid_argument"
        );
        assert_eq!(
            client.call_raw("ui.action", Some(json!({"kind": "dock_widget", "spec_id": "dock", "action_id": "refresh", "state": []})), None).await["error_code"],
            "invalid_argument"
        );

        let bad_timeout = client.call_raw("bus.publish", Some(json!({"topic": "entity.media.open", "timeout": 99999.0})), None).await;
        assert_eq!(bad_timeout["error_code"], "invalid_argument");
        assert!(bad_timeout["error"].as_str().unwrap().contains("bus.publish"));
        assert!(!bad_timeout["error"].as_str().unwrap().contains("capability.call"));

        let ok_pub = client.call_raw("bus.publish", Some(json!({"topic": "entity.media.open", "timeout": 3600.0})), None).await;
        assert_eq!(ok_pub["ok"], true);
        server.await.unwrap();
    });
}

#[test]
fn core_status_reports_fallback_socket_and_ok_false_online_false() {
    block_on(async {
        let env = TempEnv::new("omnidesk-status-fb", true);
        let listener = bind_0600(&env.socket_path);
        let server = tokio::spawn(async move {
            for idx in 0..2 {
                let (s, _) = listener.accept().await.unwrap();
                let (r, mut w) = s.into_split();
                let mut br = BufReader::new(r);
                let mut line = String::new();
                br.read_line(&mut line).await.unwrap();
                let req: Value = serde_json::from_str(&line).unwrap();
                let resp = if idx == 0 {
                    json!({"id": req["id"], "ok": true, "provider_plugin_id": "", "data": {"status": "ok"}, "error": ""})
                } else {
                    json!({"id": req["id"], "ok": false, "error_code": "timeout", "error": "server timeout", "data": {}})
                };
                w.write_all(format!("{resp}\n").as_bytes()).await.unwrap();
            }
        });

        let client = ControlClient::new(env.paths.clone());
        let bridge = EventBridge::new(env.paths.clone());
        let expected_sock = env.socket_path.display().to_string();

        let st1 = exec_core_status(&client, &bridge).await;
        assert_eq!(st1["ok"], true);
        assert_eq!(st1["data"]["online"], true);
        assert_eq!(st1["data"]["socket_path"], expected_sock);

        let st2 = exec_core_status(&client, &bridge).await;
        assert_eq!(st2["ok"], false);
        assert_eq!(st2["error_code"], "timeout");
        assert_eq!(st2["data"]["online"], false);
        assert_eq!(st2["data"]["degraded"], true);
        assert_eq!(st2["data"]["socket_path"], expected_sock);
        assert!(st2["data"]["event_bridge"].is_object());
        server.await.unwrap();
    });
}

#[test]
fn command_args_handle_camelcase_snakecase_and_never_fail_open() {
    block_on(async {
        let env = TempEnv::new("omnidesk-cmd-args", false);
        let client = ControlClient::new(env.paths.clone());

        let live_camel = exec_list_plugins_args(&client, &json!({"liveOnly": true})).await;
        assert_eq!(live_camel["ok"], false);
        assert_eq!(live_camel["error_code"], "service_offline");

        let live_snake = exec_list_plugins_args(&client, &json!({"live_only": true})).await;
        assert_eq!(live_snake["ok"], false);
        assert_eq!(live_snake["error_code"], "service_offline");

        let bad_bool = exec_list_plugins_args(&client, &json!({"liveOnly": "true"})).await;
        assert_eq!(bad_bool["error_code"], "invalid_argument");

        let conflict_bool = exec_list_plugins_args(&client, &json!({"liveOnly": true, "live_only": false})).await;
        assert_eq!(conflict_bool["error_code"], "invalid_argument");

        let rs_camel = exec_read_source_args(&client, &json!({"domain": "media", "sourceId": "plugin-ai-curator:2", "timeoutMs": 500})).await;
        assert_eq!(rs_camel["error_code"], "service_offline");

        let rs_bad_to = exec_read_source_args(&client, &json!({"domain": "media", "timeoutMs": 50})).await;
        assert_eq!(rs_bad_to["error_code"], "invalid_argument");

        let rs_conflict = exec_read_source_args(&client, &json!({"domain": "media", "sourceId": "a", "source_id": "b"})).await;
        assert_eq!(rs_conflict["error_code"], "invalid_argument");

        assert_eq!(exec_emit_event_args(&client, &json!({"topic": "entity.media.open", "timeoutMs": 50})).await["error_code"], "invalid_argument");
        assert_eq!(exec_call_capability_args(&client, &json!({"capability": "demo.echo.v1", "timeoutMs": 50})).await["error_code"], "invalid_argument");
        assert_eq!(exec_call_args(&client, &json!({"op": "ping", "timeoutMs": 50})).await["error_code"], "invalid_argument");
        assert_eq!(exec_list_plugins_args(&client, &json!([1, 2])).await["error_code"], "invalid_argument");
    });
}

#[test]
fn event_bridge_stop_stress_race_never_misses_wakeup() {
    block_on(async {
        let env = TempEnv::new("omnidesk-stop-stress", false);
        let listener = bind_0600(&env.socket_path);
        let sink = Arc::new(NullSink::default());

        let server = tokio::spawn(async move {
            for _ in 0..10 {
                let Ok((s, _)) = listener.accept().await else { break; };
                tokio::spawn(async move {
                    let (r, mut w) = s.into_split();
                    let mut br = BufReader::new(r);
                    let mut l = String::new();
                    if br.read_line(&mut l).await.is_ok() {
                        if let Ok(req) = serde_json::from_str::<Value>(&l) {
                            let ack = json!({"id": req["id"], "ok": true, "data": {"subscribed": true}});
                            let _ = w.write_all(format!("{ack}\n").as_bytes()).await;
                            tokio::time::sleep(Duration::from_millis(500)).await;
                        }
                    }
                });
            }
        });

        for _ in 0..10 {
            let bridge = EventBridge::new(env.paths.clone());
            let b2 = Arc::clone(&bridge);
            let s2 = Arc::clone(&sink);
            let task = tokio::spawn(async move { b2.step_once(s2.as_ref()).await });
            tokio::time::sleep(Duration::from_millis(5)).await;
            bridge.stop();
            assert!(tokio::time::timeout(Duration::from_secs(1), task).await.is_ok());
        }
        server.await.unwrap();
    });
}

#[test]
fn command_layer_supports_wrapped_and_flat_args_against_mock_control_socket() {
    block_on(async {
        let env = TempEnv::new("omnidesk-args-roundtrip", false);
        let listener = bind_0600(&env.socket_path);
        // 7 list_plugins + 2 cap_w + 2 ev_w + 2 rs_w + 2 call_w + 4 flat + 1 agree_cap + 1 cookies_list = 21
        let expected_requests = 21;
        let server = tokio::spawn(async move {
            for _ in 0..expected_requests {
                let (s, _) = listener.accept().await.unwrap();
                let (r, mut w) = s.into_split();
                let mut br = BufReader::new(r);
                let mut line = String::new();
                br.read_line(&mut line).await.unwrap();
                let req: Value = serde_json::from_str(&line).unwrap();
                let req_id = req["id"].as_str().unwrap().to_string();
                let resp = match req["op"].as_str().unwrap() {
                    "plugin.list" => json!({
                        "id": req_id,
                        "ok": true,
                        "provider_plugin_id": "",
                        "data": {"plugins": [{"plugin_id": "plugin-demo", "state": "loaded"}]},
                        "error": "",
                    }),
                    "capability.call" if req["capability"] == "web.cookies.v1" => json!({
                        "id": req_id,
                        "ok": true,
                        "provider_plugin_id": "plugin-chrome-web",
                        "data": {
                            "domain": "example.com",
                            "cookie_header": "sid=must_strip",
                            "cookies": [{"name": "sid", "value": "must_strip", "has_value": true}]
                        },
                        "error": "",
                    }),
                    "capability.call" => json!({
                        "id": req_id,
                        "ok": true,
                        "provider_plugin_id": "plugin-demo",
                        "data": {"cap": req["capability"], "payload": req["payload"]},
                        "error": "",
                    }),
                    "bus.publish" => json!({
                        "id": req_id,
                        "ok": true,
                        "provider_plugin_id": "",
                        "data": {"topic": req["topic"], "payload": req["payload"], "delivered": 1},
                        "error": "",
                    }),
                    "plugin.reload" => json!({
                        "id": req_id,
                        "ok": true,
                        "provider_plugin_id": req["plugin_id"],
                        "data": {"plugin_id": req["plugin_id"], "reload_count": 1},
                        "error": "",
                    }),
                    other => json!({
                        "id": req_id,
                        "ok": false,
                        "error_code": "unexpected_op",
                        "error": other,
                        "data": {},
                    }),
                };
                w.write_all(format!("{resp}\n").as_bytes()).await.unwrap();
            }
        });

        let client = ControlClient::new(env.paths.clone());

        let invoke_both = |cmd: &'static str, body: Value| {
            let client = &client;
            async move {
                let via_json = exec_command_invoke_body(client, cmd, &InvokeBody::Json(body.clone()))
                    .await
                    .unwrap();
                let via_raw = exec_command_invoke_body(
                    client,
                    cmd,
                    &InvokeBody::Raw(serde_json::to_vec(&body).unwrap()),
                )
                .await
                .unwrap();
                (via_json, via_raw)
            }
        };

        // 1. Wrapped { args: ... } for all 5 parameterized commands (Json & Raw) + empty {}
        for payload in [json!({"args": {}}), json!({"args": {"liveOnly": true}}), json!({})] {
            let (j, r) = invoke_both("list_plugins", payload).await;
            assert_eq!(j["ok"], true);
            assert_eq!(j["data"]["source"], "control_socket");
            assert_eq!(r, j);
        }
        // Raw empty bytes behaves identically to empty {}
        let raw_empty_lp = exec_command_invoke_body(&client, "list_plugins", &InvokeBody::Raw(vec![]))
            .await
            .unwrap();
        assert_eq!(raw_empty_lp["ok"], true);

        let (cap_w_json, cap_w_raw) = invoke_both(
            "call_capability",
            json!({"args": {"capability": "demo.echo.v1", "payload": {"msg": "wrapped"}, "timeoutMs": 5000}}),
        )
        .await;
        assert_eq!(cap_w_json["ok"], true);
        assert_eq!(cap_w_json["data"]["cap"], "demo.echo.v1");
        assert_eq!(cap_w_json["data"]["payload"]["msg"], "wrapped");
        assert_eq!(cap_w_raw, cap_w_json);

        let (ev_w_json, ev_w_raw) = invoke_both(
            "emit_event",
            json!({"args": {"topic": "entity.media.open", "payload": {"item_id": "m1"}, "timeoutMs": 5000}}),
        )
        .await;
        assert_eq!(ev_w_json["ok"], true);
        assert_eq!(ev_w_json["data"]["topic"], "entity.media.open");
        assert_eq!(ev_w_json["data"]["payload"]["item_id"], "m1");
        assert_eq!(ev_w_raw, ev_w_json);

        let (rs_w_json, rs_w_raw) = invoke_both(
            "read_source",
            json!({"args": {"domain": "media", "sourceId": "plugin-ai-curator:2", "payload": {"page": 1}, "timeoutMs": 5000}}),
        )
        .await;
        assert_eq!(rs_w_json["ok"], true);
        assert_eq!(rs_w_json["data"]["cap"], "media.source.v1");
        assert_eq!(rs_w_json["data"]["payload"]["source_id"], "plugin-ai-curator:2");
        assert_eq!(rs_w_json["data"]["payload"]["page"], 1);
        assert_eq!(rs_w_raw, rs_w_json);

        let (call_w_json, call_w_raw) = invoke_both(
            "call",
            json!({"args": {"op": "plugin.reload", "params": {"plugin_id": "plugin-demo"}, "timeoutMs": 5000}}),
        )
        .await;
        assert_eq!(call_w_json["ok"], true);
        assert_eq!(call_w_json["data"]["plugin_id"], "plugin-demo");
        assert_eq!(call_w_raw, call_w_json);

        // 2. Flat form remains backward-compatible for all commands
        let cap_flat = exec_command_invoke_body(
            &client,
            "call_capability",
            &InvokeBody::Json(json!({"capability": "demo.echo.v1", "payload": {"msg": "flat"}})),
        )
        .await
        .unwrap();
        assert_eq!(cap_flat["ok"], true);
        assert_eq!(cap_flat["data"]["payload"]["msg"], "flat");

        let ev_flat = exec_command_invoke_body(
            &client,
            "emit_event",
            &InvokeBody::Json(json!({"topic": "entity.text.open", "payload": {"id": "t1"}})),
        )
        .await
        .unwrap();
        assert_eq!(ev_flat["ok"], true);
        assert_eq!(ev_flat["data"]["topic"], "entity.text.open");

        let rs_flat = exec_command_invoke_body(
            &client,
            "read_source",
            &InvokeBody::Json(json!({"domain": "text", "source_id": "plugin-zhihu:1"})),
        )
        .await
        .unwrap();
        assert_eq!(rs_flat["ok"], true);
        assert_eq!(rs_flat["data"]["cap"], "text.source.v1");
        assert_eq!(rs_flat["data"]["payload"]["source_id"], "plugin-zhihu:1");

        let call_flat = exec_command_invoke_body(
            &client,
            "call",
            &InvokeBody::Json(json!({"op": "plugin.reload", "params": {"plugin_id": "plugin-flat"}})),
        )
        .await
        .unwrap();
        assert_eq!(call_flat["ok"], true);
        assert_eq!(call_flat["data"]["plugin_id"], "plugin-flat");

        // 3. Non-conflicting agreement between top-level and args succeeds
        let agree_cap = exec_command_invoke_body(
            &client,
            "call_capability",
            &InvokeBody::Json(json!({"args": {"capability": "demo.echo.v1", "payload": {"k": "v"}}, "capability": "demo.echo.v1"})),
        )
        .await
        .unwrap();
        assert_eq!(agree_cap["ok"], true);
        assert_eq!(agree_cap["data"]["payload"]["k"], "v");

        // 4. Wrapped web.cookies.v1 list still scrubs sensitive cookie fields
        let cookies_list = exec_command_invoke_body(
            &client,
            "call_capability",
            &InvokeBody::Json(json!({"args": {"capability": "web.cookies.v1", "payload": {"action": "list", "domain": "example.com"}}})),
        )
        .await
        .unwrap();
        assert_eq!(cookies_list["ok"], true);
        assert!(cookies_list["data"].get("cookie_header").is_none());
        assert!(cookies_list["data"]["cookies"][0].get("value").is_none());
        assert_eq!(cookies_list["data"]["cookies"][0]["has_value"], true);

        server.await.unwrap();
    });
}

#[test]
fn command_layer_fail_closed_on_invalid_wrapped_args_conflicts_and_missing_fields() {
    block_on(async {
        let env = TempEnv::new("omnidesk-args-fail-closed", false);
        let client = ControlClient::new(env.paths.clone());

        let assert_fail_closed = |cmd: &'static str, body: Value, expected_code: &'static str| {
            let client = &client;
            async move {
                for invoke_body in [
                    InvokeBody::Json(body.clone()),
                    InvokeBody::Raw(serde_json::to_vec(&body).unwrap()),
                ] {
                    let res = exec_command_invoke_body(client, cmd, &invoke_body).await.unwrap();
                    assert_eq!(res["ok"], false, "expected ok=false for {cmd} {body}");
                    assert_eq!(
                        res["error_code"], expected_code,
                        "unexpected error_code for {cmd} {body}: {res}"
                    );
                }
            }
        };

        // 1. Non-object `args` and double-wrapped `args` must fail closed across all 5 commands
        for cmd in ["list_plugins", "call_capability", "emit_event", "read_source", "call"] {
            for bad_args in [
                json!({"args": null}),
                json!({"args": []}),
                json!({"args": [1, 2]}),
                json!({"args": "bad"}),
                json!({"args": 42}),
                json!({"args": true}),
                json!({"args": {"args": {"liveOnly": true}}}),
            ] {
                assert_fail_closed(cmd, bad_args, "invalid_argument").await;
            }
        }

        // 2. Top-level vs `args` conflicts must fail closed (both direct key and dual-case alias)
        assert_fail_closed(
            "list_plugins",
            json!({"args": {"liveOnly": true}, "liveOnly": false}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "list_plugins",
            json!({"args": {"liveOnly": true}, "live_only": false}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "call_capability",
            json!({"args": {"capability": "demo.echo.v1"}, "capability": "other.cap.v1"}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "call_capability",
            json!({"args": {"capability": "demo.echo.v1", "payload": {"a": 1}}, "payload": {"a": 2}}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "call_capability",
            json!({"args": {"capability": "demo.echo.v1", "timeoutMs": 500}, "timeout_ms": 1000}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "call_capability",
            json!({"args": {"capability": "demo.echo.v1"}, "capability": null}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "emit_event",
            json!({"args": {"topic": "entity.media.open"}, "topic": "entity.text.open"}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "read_source",
            json!({"args": {"domain": "media"}, "domain": "text"}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "read_source",
            json!({"args": {"domain": "media", "sourceId": "plugin-a"}, "source_id": "plugin-b"}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "read_source",
            json!({"args": {"domain": "media", "payload": {"source_id": "plugin-a"}}, "sourceId": "plugin-b"}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "call",
            json!({"args": {"op": "ping"}, "op": "health"}),
            "invalid_argument",
        )
        .await;

        // 3. Missing required fields fail closed in both wrapped and flat forms
        for (cmd, key) in [
            ("call_capability", "capability"),
            ("emit_event", "topic"),
            ("read_source", "domain"),
            ("call", "op"),
        ] {
            assert_fail_closed(cmd, json!({}), "invalid_argument").await;
            assert_fail_closed(cmd, json!({"args": {}}), "invalid_argument").await;
            assert_fail_closed(cmd, json!({"args": {key: null}}), "invalid_argument").await;
        }

        // 4. Existing security policies remain strictly enforced for wrapped { args }
        assert_fail_closed(
            "call_capability",
            json!({"args": {"capability": "web.cookies.v1", "payload": {"action": "get_cookies"}}}),
            "permission_denied",
        )
        .await;
        assert_fail_closed(
            "call",
            json!({"args": {"op": "capability.call", "params": {"capability": "web.cookies.v1", "payload": {"action": "get"}}}}),
            "permission_denied",
        )
        .await;
        assert_fail_closed(
            "read_source",
            json!({"args": {"domain": "media", "sourceId": "../escape"}}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "call",
            json!({"args": {"op": "bus.subscribe"}}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "call",
            json!({"args": {"op": "plugin.load", "params": {"plugin_id": "plugin-demo", "path": "/tmp/evil"}}}),
            "invalid_argument",
        )
        .await;
        assert_fail_closed(
            "call",
            json!({"args": {"op": "ping", "params": {"jsonrpc": "2.0"}}}),
            "invalid_argument",
        )
        .await;
    });
}

#[test]
fn raw_json_args_raw_and_json_invoke_body_parity() {
    block_on(async {
        let env = TempEnv::new("omnidesk-raw-parity", false);
        let client = ControlClient::new(env.paths.clone());

        let assert_raw_eq = |body_json: Value, raw_bytes: &[u8]| {
            let from_json = RawJsonArgs::from_invoke_body(&InvokeBody::Json(body_json));
            let from_raw = RawJsonArgs::from_invoke_body(&InvokeBody::Raw(raw_bytes.to_vec()));
            match (from_json, from_raw) {
                (RawJsonArgs::Value(a), RawJsonArgs::Value(b)) => assert_eq!(a, b),
                other => panic!("expected Value parity, got {other:?}"),
            }
        };

        assert_raw_eq(json!({}), b"");
        assert_raw_eq(json!({}), b"{}");
        assert_raw_eq(Value::Null, b"null");
        assert_raw_eq(
            json!({"args": {"capability": "demo.echo.v1", "payload": {"n": 1}}}),
            b" \n\t {\"args\":{\"capability\":\"demo.echo.v1\",\"payload\":{\"n\":1}}} ",
        );
        assert_raw_eq(json!([1, 2]), b"[1, 2]");

        for bad_bytes in [b"{not-json".as_slice(), b"   ".as_slice(), b"\xff\xfe".as_slice()] {
            assert!(matches!(
                RawJsonArgs::from_invoke_body(&InvokeBody::Raw(bad_bytes.to_vec())),
                RawJsonArgs::Invalid(_)
            ));
            let res = exec_command_invoke_body(
                &client,
                "call_capability",
                &InvokeBody::Raw(bad_bytes.to_vec()),
            )
            .await
            .unwrap();
            assert_eq!(res["ok"], false);
            assert_eq!(res["error_code"], "invalid_argument");
        }
    });
}
