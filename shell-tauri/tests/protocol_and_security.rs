use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use omnidesk_lib::commands::standalone_runtime_status;
use omnidesk_lib::control_client::NdjsonFrameReader;
use omnidesk_lib::manifest::scan_plugin_dirs;
use omnidesk_lib::mpv;
use omnidesk_lib::paths::{OmnideskPaths, SocketCheck};
use omnidesk_lib::protocol::{
    is_valid_tauri_event_name, normalize_object_payload, parse_control_response,
    resolve_domain_source_capability, tauri_event_name_for_topic, validate_capability_id,
    validate_op, validate_plugin_id, validate_source_id, validate_timeout_ms, validate_topic,
    MAX_PAYLOAD_BYTES, MAX_TIMEOUT_MS,
};
use omnidesk_lib::redact::{is_secret_key, redact_json_value, redact_text};
use serde_json::json;

struct TempDirGuard(PathBuf);

impl TempDirGuard {
    fn new(prefix: &str) -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()));
        fs::create_dir_all(&path).unwrap();
        Self(path)
    }

    fn path(&self) -> &PathBuf {
        &self.0
    }
}

impl Drop for TempDirGuard {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn redact_json_and_text_scrubs_secrets_without_breaking_benign_ids() {
    assert!(is_secret_key("token"));
    assert!(is_secret_key("access_token"));
    assert!(is_secret_key("Cookie_Header"));
    assert!(is_secret_key("set-cookie"));
    assert!(is_secret_key("gateway_key"));
    assert!(!is_secret_key("session_id"));
    assert!(!is_secret_key("plugin_id"));
    assert!(!is_secret_key("total_tokens"));
    assert!(!is_secret_key("requires_auth"));

    let payload = json!({
        "session_id": "sess-42",
        "plugin_id": "plugin-magpie-agent",
        "token": "secret-jwt-999",
        "cookie": "sid=abc123; theme=paper",
        "nested": {
            "gateway_key": "gw-secret-key",
            "usage": { "prompt_tokens": 12, "total_tokens": 30 },
            "message": "Authorization: Bearer abcdef123456 and sk-1234567890abcdef"
        },
        "headers": ["Cookie: a=1; b=2", "X-Normal: ok"]
    });

    let redacted = redact_json_value(&payload);
    assert_eq!(redacted["session_id"], "sess-42");
    assert_eq!(redacted["plugin_id"], "plugin-magpie-agent");
    assert_eq!(redacted["token"], "[REDACTED]");
    assert_eq!(redacted["cookie"], "[REDACTED]");
    assert_eq!(redacted["nested"]["gateway_key"], "[REDACTED]");
    assert_eq!(redacted["nested"]["usage"]["total_tokens"], 30);

    let msg = redacted["nested"]["message"].as_str().unwrap();
    assert!(!msg.contains("abcdef123456"));
    assert!(!msg.contains("sk-1234567890abcdef"));
    assert!(msg.contains("[REDACTED]"));

    let hdr = redacted["headers"][0].as_str().unwrap();
    assert!(!hdr.contains("a=1"));
    assert!(hdr.contains("[REDACTED]"));
    assert_eq!(redacted["headers"][1], "X-Normal: ok");

    let text = redact_text("token=my_secret_123 url=https://example.com ghp_1234567890abcdef");
    assert!(!text.contains("my_secret_123"));
    assert!(!text.contains("ghp_1234567890abcdef"));
    assert!(text.contains("https://example.com"));

    // camelCase secrets, Proxy-Authorization, URL userinfo, without breaking token counters
    for k in ["apiKey", "accessToken", "refreshToken", "clientSecret", "gatewayKey", "cookieHeader", "setCookie", "nasPassword", "Proxy-Authorization"] {
        assert!(is_secret_key(k), "expected secret key: {k}");
    }
    for k in ["totalTokens", "promptTokens", "completionTokens", "maxTokens", "tokenCount"] {
        assert!(!is_secret_key(k), "unexpected secret key: {k}");
    }
    let camel = redact_json_value(&json!({
        "apiKey": "sk_raw", "accessToken": "tok123", "cookieHeader": "sid=abc",
        "Proxy-Authorization": "Basic dXNlcjpwYXNz",
        "usage": { "promptTokens": 11, "totalTokens": 42 }
    }));
    assert_eq!(camel["apiKey"], "[REDACTED]");
    assert_eq!(camel["accessToken"], "[REDACTED]");
    assert_eq!(camel["cookieHeader"], "[REDACTED]");
    assert_eq!(camel["Proxy-Authorization"], "[REDACTED]");
    assert_eq!(camel["usage"]["promptTokens"], 11);
    assert_eq!(camel["usage"]["totalTokens"], 42);

    let url_msg = redact_text("connect http://admin:pass123@192.168.1.10/api failed with apiKey=secret99 and Proxy-Authorization: Basic dXNlcjpwYXNz");
    assert!(!url_msg.contains("pass123") && !url_msg.contains("secret99") && !url_msg.contains("dXNlcjpwYXNz"));
    assert!(url_msg.contains("http://admin:[REDACTED]@192.168.1.10/api"));
    let port_msg = redact_text("dial https://example.com:8443/path ok");
    assert!(port_msg.contains("https://example.com:8443/path"));
}

#[test]
fn xdg_paths_resolution_fallback_and_containment() {
    let mut env = HashMap::new();
    env.insert("HOME".to_string(), "/home/tester".to_string());
    env.insert("XDG_RUNTIME_DIR".to_string(), "/run/user/1000".to_string());
    env.insert("XDG_DATA_HOME".to_string(), "/home/tester/.share".to_string());
    let paths = OmnideskPaths::from_env_map(&env);
    assert_eq!(
        paths.socket_candidates(),
        vec![
            PathBuf::from("/run/user/1000/omnidesk/control.sock"),
            PathBuf::from("/home/tester/.share/omnidesk/run/control.sock"),
        ]
    );
    assert!(paths.is_allowed_socket_path(std::path::Path::new("/run/user/1000/omnidesk/control.sock")));
    assert!(paths.is_allowed_socket_path(std::path::Path::new("/home/tester/.share/omnidesk/run/control.sock")));
    assert!(!paths.is_allowed_socket_path(std::path::Path::new("/tmp/other.sock")));
    assert!(!paths.is_allowed_socket_path(std::path::Path::new("/run/user/1000/omnidesk/../control.sock")));

    // Relative XDG_RUNTIME_DIR falls back cleanly to XDG_DATA_HOME/omnidesk/run/control.sock
    env.insert("XDG_RUNTIME_DIR".to_string(), "relative/dir".to_string());
    let fb_paths = OmnideskPaths::from_env_map(&env);
    assert_eq!(
        fb_paths.primary_socket_path(),
        PathBuf::from("/home/tester/.share/omnidesk/run/control.sock")
    );

    // Missing HOME uses getpwuid_r on Unix (not hardcoded "/")
    let no_home = OmnideskPaths::from_env_map(&HashMap::new());
    assert!(no_home.home_dir.is_absolute());
    if unsafe { libc::geteuid() } != 0 {
        assert_ne!(no_home.home_dir, PathBuf::from("/"));
    }
    assert!(no_home.data_dir.starts_with(&no_home.home_dir));
}

#[test]
fn socket_security_enforces_0600_no_symlink_and_socket_type() {
    let tmp = TempDirGuard::new("omnidesk-sec");
    let rt_dir = tmp.path().join("rt").join("omnidesk");
    let data_dir = tmp.path().join("data").join("omnidesk");
    fs::create_dir_all(&rt_dir).unwrap();
    fs::set_permissions(&rt_dir, fs::Permissions::from_mode(0o700)).unwrap();
    fs::create_dir_all(&data_dir).unwrap();
    let paths = OmnideskPaths::for_dirs(Some(rt_dir.clone()), data_dir.clone(), tmp.path().join("cfg"));
    let sock_path = rt_dir.join("control.sock");

    // 1. Missing socket -> service_offline
    assert_eq!(paths.resolve_active_socket().unwrap_err().code, "service_offline");

    // 2. Non-socket regular file -> path_safety
    fs::write(&sock_path, b"not a socket").unwrap();
    assert_eq!(paths.resolve_active_socket().unwrap_err().code, "path_safety");
    fs::remove_file(&sock_path).unwrap();

    // 3. Symlink -> path_safety
    let target = tmp.path().join("real.sock");
    fs::write(&target, b"").unwrap();
    std::os::unix::fs::symlink(&target, &sock_path).unwrap();
    assert_eq!(paths.resolve_active_socket().unwrap_err().code, "path_safety");
    fs::remove_file(&sock_path).unwrap();

    // 4. UnixListener with 0644 -> permission_denied; 0600 -> Ready
    let listener = UnixListener::bind(&sock_path).unwrap();
    fs::set_permissions(&sock_path, fs::Permissions::from_mode(0o644)).unwrap();
    assert_eq!(paths.resolve_active_socket().unwrap_err().code, "permission_denied");
    fs::set_permissions(&sock_path, fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(paths.check_socket_path(&sock_path).unwrap(), SocketCheck::Ready(sock_path.clone()));

    // 5. Parent directory with group/other access (0777 or 0755) -> permission_denied
    fs::set_permissions(&rt_dir, fs::Permissions::from_mode(0o777)).unwrap();
    assert_eq!(paths.check_socket_path(&sock_path).unwrap_err().code, "permission_denied");
    fs::set_permissions(&rt_dir, fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(paths.check_socket_path(&sock_path).unwrap_err().code, "permission_denied");
    fs::set_permissions(&rt_dir, fs::Permissions::from_mode(0o700)).unwrap();
    drop(listener);

    // 6. Symlinked data_dir (<XDG_DATA_HOME>/omnidesk) -> path_safety; 0777 data_dir -> permission_denied
    let ext_dir = tmp.path().join("ext-data");
    fs::create_dir_all(ext_dir.join("run")).unwrap();
    fs::set_permissions(&ext_dir, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(ext_dir.join("run"), fs::Permissions::from_mode(0o700)).unwrap();
    let sym_data = tmp.path().join("sym-data").join("omnidesk");
    fs::create_dir_all(sym_data.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink(&ext_dir, &sym_data).unwrap();
    let sym_paths = OmnideskPaths::for_dirs(None, sym_data, tmp.path().join("cfg"));
    assert_eq!(sym_paths.resolve_active_socket().unwrap_err().code, "path_safety");

    fs::create_dir_all(data_dir.join("run")).unwrap();
    fs::set_permissions(&data_dir, fs::Permissions::from_mode(0o777)).unwrap();
    fs::set_permissions(data_dir.join("run"), fs::Permissions::from_mode(0o700)).unwrap();
    let fb_only = OmnideskPaths::for_dirs(None, data_dir.clone(), tmp.path().join("cfg"));
    assert_eq!(fb_only.resolve_active_socket().unwrap_err().code, "permission_denied");
}

#[test]
fn parameter_validation_and_tauri_event_name_mapping() {
    assert_eq!(validate_capability_id("llm.router.v1").unwrap(), "llm.router.v1");
    assert_eq!(validate_capability_id("media.local_match.v2").unwrap(), "media.local_match.v2");
    assert_eq!(validate_capability_id("bad").unwrap_err().code, "invalid_argument");
    assert_eq!(validate_capability_id("llm.router.v").unwrap_err().code, "invalid_argument");
    assert_eq!(validate_capability_id("LLM.router.v1").unwrap_err().code, "invalid_argument");

    assert_eq!(validate_topic("entity.media.open").unwrap(), "entity.media.open");
    assert_eq!(validate_topic("plugin.state_changed").unwrap(), "plugin.state_changed");
    assert_eq!(validate_topic("..").unwrap_err().code, "invalid_argument");
    assert_eq!(validate_topic("a b").unwrap_err().code, "invalid_argument");

    assert_eq!(validate_plugin_id("plugin-demo").unwrap(), "plugin-demo");
    assert_eq!(validate_plugin_id("../evil").unwrap_err().code, "invalid_argument");

    assert_eq!(validate_source_id("plugin-nas").unwrap(), "plugin-nas");
    assert_eq!(validate_source_id("plugin-ai-curator:2").unwrap(), "plugin-ai-curator:2");
    assert_eq!(validate_source_id("../evil").unwrap_err().code, "invalid_argument");
    assert_eq!(validate_source_id(":2").unwrap_err().code, "invalid_argument");
    assert_eq!(validate_source_id("plugin:").unwrap_err().code, "invalid_argument");

    assert_eq!(validate_op("capability.call").unwrap(), "capability.call");
    assert_eq!(validate_op("domain.snapshot").unwrap(), "domain.snapshot");
    assert_eq!(validate_op("domain.invoke").unwrap(), "domain.invoke");
    assert_eq!(validate_op("ui.action").unwrap(), "ui.action");
    assert_eq!(validate_op("ping").unwrap(), "ping");
    assert_eq!(validate_op("bad op").unwrap_err().code, "invalid_argument");
    assert_eq!(validate_op("bus.subscribe").unwrap_err().code, "invalid_argument");
    assert_eq!(validate_op("system.exec").unwrap_err().code, "invalid_argument");

    assert_eq!(resolve_domain_source_capability("media").unwrap().1, "media.source.v1");
    assert_eq!(resolve_domain_source_capability("text").unwrap().1, "text.source.v1");
    assert_eq!(resolve_domain_source_capability("book").unwrap().1, "book.source.v1");
    assert_eq!(resolve_domain_source_capability("recommend").unwrap().1, "recommend.feed.v1");
    assert_eq!(resolve_domain_source_capability("unknown").unwrap_err().code, "invalid_argument");

    assert!(normalize_object_payload(None).unwrap().is_empty());
    assert!(normalize_object_payload(Some(json!({ "a": 1 }))).is_ok());
    assert_eq!(normalize_object_payload(Some(json!([1, 2]))).unwrap_err().code, "invalid_argument");

    assert_eq!(MAX_TIMEOUT_MS, 3_600_000);
    assert!(validate_timeout_ms(None).is_ok());
    assert!(validate_timeout_ms(Some(3_600_000)).is_ok());
    assert_eq!(validate_timeout_ms(Some(0)).unwrap_err().code, "invalid_argument");
    assert_eq!(validate_timeout_ms(Some(3_600_001)).unwrap_err().code, "invalid_argument");

    let ev = tauri_event_name_for_topic("entity.media.open").unwrap();
    assert_eq!(ev, "omnidesk:topic:entity:media:open");
    assert!(is_valid_tauri_event_name(&ev));
    assert!(!is_valid_tauri_event_name("entity.media.open"));
}

#[test]
fn ndjson_framing_carryover_eof_and_size_limit() {
    let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    rt.block_on(async {
        let data = b"{\"ok\":true,\"data\":{\"n\":1}}\r\n{\"topic\":\"a.b\"}\n{\"eof\":true}";
        let mut cursor = std::io::Cursor::new(&data[..]);
        let mut reader = NdjsonFrameReader::new(MAX_PAYLOAD_BYTES);

        assert_eq!(reader.read_frame(&mut cursor).await.unwrap().unwrap(), b"{\"ok\":true,\"data\":{\"n\":1}}");
        assert_eq!(reader.read_frame(&mut cursor).await.unwrap().unwrap(), b"{\"topic\":\"a.b\"}");
        assert_eq!(reader.read_frame(&mut cursor).await.unwrap().unwrap(), b"{\"eof\":true}");
        assert_eq!(reader.read_frame(&mut cursor).await.unwrap(), None);

        let oversized = b"12345678901\n";
        let mut cursor2 = std::io::Cursor::new(&oversized[..]);
        let mut small_reader = NdjsonFrameReader::new(8);
        assert_eq!(small_reader.read_frame(&mut cursor2).await.unwrap_err().code, "internal_error");
    });

    // Response ID mismatch, missing ID on ok:true, non-object data, and malformed JSON
    assert_eq!(parse_control_response(br#"{"id":"other","ok":true,"data":{}}"#, "req-1").unwrap_err().code, "internal_error");
    assert_eq!(parse_control_response(br#"{"ok":true,"data":{}}"#, "req-1").unwrap_err().code, "internal_error");
    assert_eq!(parse_control_response(br#"{"id":null,"ok":true,"data":{}}"#, "req-1").unwrap_err().code, "internal_error");
    assert_eq!(parse_control_response(br#"{"id":"req-1","ok":true,"data":[1,2]}"#, "req-1").unwrap_err().code, "internal_error");
    let null_data = parse_control_response(br#"{"id":"req-1","ok":true,"data":null}"#, "req-1").unwrap();
    assert_eq!(null_data["data"], json!({}));
    let err_null_id = parse_control_response(br#"{"id":null,"ok":false,"error_code":"timeout","error":"t"}"#, "req-1").unwrap();
    assert_eq!(err_null_id["ok"], false);
    assert_eq!(err_null_id["error_code"], "timeout");
    assert_eq!(parse_control_response(b"{not-json", "req-1").unwrap_err().code, "internal_error");
}

#[test]
fn preserved_runtime_status_mpv_probe_and_manifest_scan() {
    let status = serde_json::to_value(standalone_runtime_status()).unwrap();
    assert_eq!(status["shell"], "tauri-v2");
    assert_eq!(status["appId"], "omnidesk");
    assert_ne!(status["status"], "skeleton");
    assert_ne!(status["mpv"]["status"], "skeleton");
    assert_eq!(status["mpris"]["busName"], "org.mpris.MediaPlayer2.omnidesk");

    let mpv = serde_json::to_value(mpv::probe()).unwrap();
    assert_ne!(mpv["status"], "skeleton");
    assert_eq!(mpv["initialized"], false);
    assert_eq!(mpv["hwdecActive"], false);
    if mpv["available"] == true {
        assert_eq!(mpv["backend"], "mpv-ipc");
        assert_eq!(mpv["hwdec"], "auto-safe");
        assert_eq!(mpv["ipcSupported"], true);
    } else {
        assert!(mpv["unavailableReason"].as_str().is_some_and(|s| !s.is_empty()));
    }

    let tmp = TempDirGuard::new("omnidesk-manifests");
    let valid_dir = tmp.path().join("plugin-ok");
    fs::create_dir_all(&valid_dir).unwrap();
    fs::write(
        valid_dir.join("plugin.json"),
        json!({
            "schema_version": 2, "id": "plugin-ok", "name": "OK Plugin", "version": "0.2.0",
            "kind": "provider", "domain": "core", "provides": ["demo.ok.v1"],
            "description": "connect http://admin:pass123@nas/api",
            "config_schema": [
                {"key": "api_key", "label": "API Key", "type": "secret", "default": "should-be-cleared"},
                {"key": "apiKey", "label": "Camel Key", "type": "string", "default": "camel-secret", "description": "Authorization: Bearer tok123456"}
            ]
        })
        .to_string(),
    )
    .unwrap();

    for (dir_name, manifest) in [
        ("plugin-bad", json!({"schema_version": 1, "id": "../../etc/passwd"})),
        ("bad dir", json!({"schema_version": 1, "id": "../bad"})),
        ("plugin-bad-semver", json!({"schema_version": 2, "id": "plugin-bad-semver", "name": "X", "version": "not-semver", "kind": "provider", "domain": "core"})),
        ("plugin-bad-cap", json!({"schema_version": 2, "id": "plugin-bad-cap", "name": "X", "version": "0.1.0", "kind": "provider", "domain": "core", "provides": ["BAD_CAP"]})),
        ("plugin-bad-entry", json!({"schema_version": 2, "id": "plugin-bad-entry", "name": "X", "version": "0.1.0", "kind": "provider", "domain": "core", "entry": "bad entry"})),
        ("plugin-bad-select", json!({"schema_version": 2, "id": "plugin-bad-select", "name": "X", "version": "0.1.0", "kind": "provider", "domain": "core", "config_schema": [{"key": "mode", "label": "Mode", "type": "select", "options": []}]})),
    ] {
        let d = tmp.path().join(dir_name);
        fs::create_dir_all(&d).unwrap();
        fs::write(d.join("plugin.json"), manifest.to_string()).unwrap();
    }

    let scanned = scan_plugin_dirs(&[tmp.path().to_path_buf()]);
    assert_eq!(scanned.len(), 7);
    assert!(scanned.iter().all(|p| !p["plugin_id"].as_str().unwrap().contains("..") && p["plugin_id"] != "bad dir"));
    for id in ["plugin-bad", "invalid-plugin", "plugin-bad-semver", "plugin-bad-cap", "plugin-bad-entry", "plugin-bad-select"] {
        let rec = scanned.iter().find(|p| p["plugin_id"] == id).unwrap();
        assert_eq!(rec["state"], "invalid", "expected invalid for {id}");
    }
    let ok = scanned.iter().find(|p| p["plugin_id"] == "plugin-ok").unwrap();
    assert_eq!(ok["state"], "discovered");
    assert_eq!(ok["provides"][0], "demo.ok.v1");
    assert_eq!(ok["description"], "connect http://admin:[REDACTED]@nas/api");
    assert_eq!(ok["config_schema"][0]["default"], "");
    assert_eq!(ok["config_schema"][1]["default"], "");
    assert!(!ok["config_schema"][1]["description"].as_str().unwrap().contains("tok123456"));
}
