use std::collections::HashMap;
use std::fs;
use std::future::Future;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener as StdUnixListener;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use omnidesk_lib::commands::{exec_core_status_with_daemon, standalone_runtime_status};
use omnidesk_lib::control_client::ControlClient;
use omnidesk_lib::daemon::{build_daemon_command_spec, DaemonConfig, DaemonManager};
use omnidesk_lib::events::EventBridge;
use omnidesk_lib::paths::OmnideskPaths;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixListener;

struct TempWorkspace {
    root: PathBuf,
    paths: OmnideskPaths,
    repo_root: PathBuf,
    plugins_dir: PathBuf,
    bin_dir: PathBuf,
    socket_path: PathBuf,
}

impl TempWorkspace {
    fn new(prefix: &str) -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()));
        let cfg = root.join("cfg").join("omnidesk");
        let data = root.join("data").join("omnidesk");
        let rt = root.join("rt").join("omnidesk");
        let repo_root = root.join("repo");
        let plugins_dir = repo_root.join("plugins");
        let core_dir = repo_root.join("core").join("omnidesk_core");
        let bin_dir = root.join("bin");

        for dir in [&cfg, &data, &rt, &plugins_dir, &core_dir, &bin_dir] {
            fs::create_dir_all(dir).unwrap();
        }
        for priv_dir in [&cfg, &data, &rt] {
            fs::set_permissions(priv_dir, fs::Permissions::from_mode(0o700)).unwrap();
        }

        let socket_path = rt.join("control.sock");
        let paths = OmnideskPaths::for_dirs(Some(rt), data, cfg);
        Self {
            root,
            paths,
            repo_root,
            plugins_dir,
            bin_dir,
            socket_path,
        }
    }
}

impl Drop for TempWorkspace {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn block_on<F: Future>(fut: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(fut)
}

fn write_exec_script(path: &Path, body: &str) {
    fs::write(path, body).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

#[test]
fn daemon_command_spec_uses_argv_array_prefers_uv_falls_back_to_python3_and_curates_env() {
    let ws = TempWorkspace::new("omnidesk-cmd-spec");
    let fake_uv = ws.bin_dir.join("uv");
    let fake_py = ws.bin_dir.join("python3");
    write_exec_script(&fake_uv, "#!/bin/sh\nexit 0\n");
    write_exec_script(&fake_py, "#!/bin/sh\nexit 0\n");

    let mut host_env = HashMap::new();
    host_env.insert("PATH".to_string(), ws.bin_dir.display().to_string());
    host_env.insert("OPENAI_API_KEY".to_string(), "sk-secret-must-not-leak".to_string());
    host_env.insert("AWS_SECRET_ACCESS_KEY".to_string(), "aws-secret".to_string());
    host_env.insert("COOKIE".to_string(), "sid=secret".to_string());
    host_env.insert("LANG".to_string(), "en_US.UTF-8".to_string());
    host_env.insert("HTTPS_PROXY".to_string(), "http://127.0.0.1:7897".to_string());
    host_env.insert("NO_PROXY".to_string(), "localhost,127.0.0.1".to_string());

    // 1. Prefer uv run when uv is present in PATH
    let uv_spec = build_daemon_command_spec(
        &ws.repo_root,
        &ws.plugins_dir,
        &ws.paths,
        &host_env,
        None,
        None,
    )
    .unwrap();
    assert_eq!(uv_spec.launcher, "uv");
    assert_eq!(uv_spec.program, fake_uv);
    assert_eq!(uv_spec.cwd, ws.repo_root);
    assert_eq!(
        uv_spec.args,
        vec![
            "run",
            "python3",
            "-m",
            "omnidesk_core",
            "serve",
            "--plugins-dir",
            ws.plugins_dir.to_str().unwrap(),
        ]
    );
    assert!(!uv_spec.env.contains_key("OPENAI_API_KEY"));
    assert!(!uv_spec.env.contains_key("AWS_SECRET_ACCESS_KEY"));
    assert!(!uv_spec.env.contains_key("COOKIE"));
    assert_eq!(uv_spec.env.get("PYTHONUNBUFFERED").map(String::as_str), Some("1"));
    assert_eq!(
        uv_spec.env.get("OMNIDESK_PLUGINS_DIR").map(String::as_str),
        Some(ws.plugins_dir.to_str().unwrap())
    );
    assert_eq!(
        uv_spec.env.get("HTTPS_PROXY").map(String::as_str),
        Some("http://127.0.0.1:7897")
    );
    assert_eq!(
        uv_spec.env.get("NO_PROXY").map(String::as_str),
        Some("localhost,127.0.0.1")
    );
    assert_eq!(
        uv_spec.env.get("XDG_RUNTIME_DIR").map(String::as_str),
        ws.paths
            .runtime_dir
            .as_deref()
            .and_then(Path::parent)
            .and_then(Path::to_str)
    );

    // 2. Fallback to python3 when uv is unavailable
    fs::remove_file(&fake_uv).unwrap();
    let py_spec = build_daemon_command_spec(
        &ws.repo_root,
        &ws.plugins_dir,
        &ws.paths,
        &host_env,
        None,
        None,
    )
    .unwrap();
    assert_eq!(py_spec.launcher, "python3");
    assert_eq!(py_spec.program, fake_py);
    assert_eq!(
        py_spec.args,
        vec![
            "-m",
            "omnidesk_core",
            "serve",
            "--plugins-dir",
            ws.plugins_dir.to_str().unwrap(),
        ]
    );
    let pythonpath = py_spec.env.get("PYTHONPATH").unwrap();
    assert!(pythonpath.contains(ws.repo_root.join("core").to_str().unwrap()));

    // 3. Metacharacters in plugins path stay in single argv slot (never shell-evaluated) and traversal fails closed
    let special_plugins = ws.root.join("plugins ; touch should_not_exist && echo hi");
    fs::create_dir_all(&special_plugins).unwrap();
    let special_spec = build_daemon_command_spec(
        &ws.repo_root,
        &special_plugins,
        &ws.paths,
        &host_env,
        None,
        None,
    )
    .unwrap();
    assert_eq!(
        special_spec.args.last().map(String::as_str),
        Some(special_plugins.to_str().unwrap())
    );

    let traversal = ws.repo_root.join("..").join("plugins");
    assert_eq!(
        build_daemon_command_spec(&ws.repo_root, &traversal, &ws.paths, &host_env, None, None)
            .unwrap_err()
            .code,
        "path_safety"
    );
    let nul_path = PathBuf::from("/tmp/plugins\0bad");
    assert_eq!(
        build_daemon_command_spec(&ws.repo_root, &nul_path, &ws.paths, &host_env, None, None)
            .unwrap_err()
            .code,
        "path_safety"
    );
}

#[test]
fn daemon_does_not_respawn_when_external_control_sock_exists_and_stop_leaves_it_alive() {
    block_on(async {
        let ws = TempWorkspace::new("omnidesk-ext-daemon");
        let spawn_marker = ws.root.join("uv-was-invoked.marker");
        let fake_uv = ws.bin_dir.join("uv");
        write_exec_script(
            &fake_uv,
            &format!("#!/bin/sh\ntouch '{}'\nexit 0\n", spawn_marker.display()),
        );

        let listener = UnixListener::bind(&ws.socket_path).unwrap();
        fs::set_permissions(&ws.socket_path, fs::Permissions::from_mode(0o600)).unwrap();

        let server = tokio::spawn(async move {
            // 1. Accept initial socket-liveness probe (connect & close without sending RPC)
            let (probe_stream, _) = listener.accept().await.unwrap();
            drop(probe_stream);

            // 2. Accept real core_status ping BEFORE and AFTER daemon.stop()
            for _ in 0..2 {
                let (s, _) = listener.accept().await.unwrap();
                let (r, mut w) = s.into_split();
                let mut br = BufReader::new(r);
                let mut line = String::new();
                br.read_line(&mut line).await.unwrap();
                let req: Value = serde_json::from_str(&line).unwrap();
                let resp = json!({
                    "id": req["id"],
                    "ok": true,
                    "provider_plugin_id": "",
                    "data": {"status": "ok", "plugins": 3},
                    "error": "",
                });
                w.write_all(format!("{resp}\n").as_bytes()).await.unwrap();
            }
        });

        let mut env = HashMap::new();
        env.insert("PATH".to_string(), ws.bin_dir.display().to_string());
        let cfg = DaemonConfig {
            repo_root: Some(ws.repo_root.clone()),
            plugins_dir: Some(ws.plugins_dir.clone()),
            uv_bin: Some(fake_uv),
            python_bin: None,
            startup_timeout: Duration::from_secs(2),
            shutdown_grace: Duration::from_millis(200),
            poll_interval: Duration::from_millis(20),
        };
        let daemon = DaemonManager::with_env(ws.paths.clone(), cfg, env);
        let client = ControlClient::new(ws.paths.clone());
        let bridge = EventBridge::new(ws.paths.clone());

        let snap = daemon.start_or_attach(Some(&bridge));
        assert_eq!(snap.status, "external");
        assert!(!snap.owned_by_shell);
        assert!(snap.pid.is_none());
        assert!(!spawn_marker.exists(), "must not spawn when external daemon is running");

        let status_before = exec_core_status_with_daemon(&client, &bridge, Some(&daemon)).await;
        assert_eq!(status_before["ok"], true);
        assert_eq!(status_before["data"]["online"], true);
        assert_eq!(status_before["data"]["daemon"]["status"], "external");

        daemon.stop();
        assert!(!spawn_marker.exists());

        let status_after = exec_core_status_with_daemon(&client, &bridge, Some(&daemon)).await;
        assert_eq!(status_after["ok"], true);
        assert_eq!(status_after["data"]["online"], true);
        server.await.unwrap();
    });
}

#[test]
fn daemon_spawns_via_uv_logs_output_connects_and_cleans_up_with_sigterm_then_sigkill() {
    let ws = TempWorkspace::new("omnidesk-managed-lifecycle");
    let record_file = ws.root.join("argv-record.txt");
    let sock_str = ws.socket_path.display().to_string();

    // Case 1: Fake `uv` runs a python script that binds 0600 control.sock, answers `ping`, and handles SIGTERM cleanly.
    let real_py = "/usr/sbin/python3";
    let uv_script = format!(
        r#"#!{real_py}
import json, os, signal, socket, sys

with open({rec:?}, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({{
        "cwd": os.getcwd(),
        "argv": sys.argv[1:],
        "secret_leaked": "SECRET_SHOULD_NOT_PASS" in os.environ,
        "plugins_env": os.environ.get("OMNIDESK_PLUGINS_DIR", ""),
    }}))

print("fake uv stdout ready", flush=True)
print("fake uv stderr diagnostic", file=sys.stderr, flush=True)

sock_path = {sock:?}
os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
if os.path.exists(sock_path):
    os.unlink(sock_path)
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
os.chmod(sock_path, 0o600)
srv.listen(4)
srv.settimeout(0.05)

stop = False
def on_term(_sig, _frame):
    global stop
    stop = True
signal.signal(signal.SIGTERM, on_term)

while not stop:
    try:
        conn, _ = srv.accept()
    except TimeoutError:
        continue
    data = conn.recv(4096)
    if data:
        req = json.loads(data.decode("utf-8").splitlines()[0])
        resp = json.dumps({{"id": req["id"], "ok": True, "provider_plugin_id": "", "data": {{"status": "ok"}}, "error": ""}}) + "\n"
        conn.sendall(resp.encode("utf-8"))
    conn.close()

srv.close()
if os.path.exists(sock_path):
    os.unlink(sock_path)
sys.exit(0)
"#,
        real_py = real_py,
        rec = record_file.display().to_string(),
        sock = sock_str,
    );
    let fake_uv = ws.bin_dir.join("uv");
    write_exec_script(&fake_uv, &uv_script);

    let mut host_env = HashMap::new();
    host_env.insert("PATH".to_string(), ws.bin_dir.display().to_string());
    host_env.insert("SECRET_SHOULD_NOT_PASS".to_string(), "leak".to_string());

    let cfg = DaemonConfig {
        repo_root: Some(ws.repo_root.clone()),
        plugins_dir: Some(ws.plugins_dir.clone()),
        uv_bin: None,
        python_bin: None,
        startup_timeout: Duration::from_secs(3),
        shutdown_grace: Duration::from_millis(500),
        poll_interval: Duration::from_millis(20),
    };
    let daemon = DaemonManager::with_env(ws.paths.clone(), cfg, host_env);
    let client = ControlClient::new(ws.paths.clone());
    let bridge = EventBridge::new(ws.paths.clone());

    let snap = daemon.start_or_attach(Some(&bridge));
    assert_eq!(snap.status, "running");
    assert!(snap.owned_by_shell);
    assert_eq!(snap.launcher.as_deref(), Some("uv"));
    let child_pid = snap.pid.expect("pid recorded");

    let recorded: Value = serde_json::from_str(&fs::read_to_string(&record_file).unwrap()).unwrap();
    assert_eq!(recorded["cwd"], ws.repo_root.display().to_string());
    assert_eq!(recorded["secret_leaked"], false);
    assert_eq!(
        recorded["argv"],
        json!(["run", "python3", "-m", "omnidesk_core", "serve", "--plugins-dir", ws.plugins_dir.display().to_string()])
    );

    let core_st = block_on(exec_core_status_with_daemon(&client, &bridge, Some(&daemon)));
    assert_eq!(core_st["ok"], true);
    assert_eq!(core_st["data"]["online"], true);

    let log_text = fs::read_to_string(ws.paths.daemon_log_path()).unwrap();
    assert!(log_text.contains("fake uv stdout ready"));
    assert!(log_text.contains("fake uv stderr diagnostic"));

    daemon.stop();
    assert_eq!(daemon.snapshot().status, "stopped");
    assert_eq!(unsafe { libc::kill(child_pid as libc::pid_t, 0) }, -1);

    // Case 2: Child ignores SIGTERM -> must escalate to SIGKILL after grace timeout and reap process
    fs::remove_file(&fake_uv).unwrap();
    let ignore_term_py = format!(
        r#"#!{real_py}
import os, signal, socket, time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
sock_path = {sock:?}
os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
if os.path.exists(sock_path):
    os.unlink(sock_path)
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
os.chmod(sock_path, 0o600)
srv.listen(4)
while True:
    time.sleep(0.1)
"#,
        real_py = real_py,
        sock = sock_str,
    );
    let fake_py = ws.bin_dir.join("python3");
    write_exec_script(&fake_py, &ignore_term_py);

    let cfg2 = DaemonConfig {
        repo_root: Some(ws.repo_root.clone()),
        plugins_dir: Some(ws.plugins_dir.clone()),
        uv_bin: None,
        python_bin: None,
        startup_timeout: Duration::from_secs(3),
        shutdown_grace: Duration::from_millis(120),
        poll_interval: Duration::from_millis(20),
    };
    let daemon2 = DaemonManager::with_env(ws.paths.clone(), cfg2, HashMap::from([("PATH".into(), ws.bin_dir.display().to_string())]));
    let snap2 = daemon2.start_or_attach(None);
    assert_eq!(snap2.status, "running");
    assert_eq!(snap2.launcher.as_deref(), Some("python3"));
    let pid2 = snap2.pid.unwrap();

    daemon2.stop();
    assert_eq!(unsafe { libc::kill(pid2 as libc::pid_t, 0) }, -1);
}

#[test]
fn daemon_startup_failure_and_timeout_honestly_report_offline_never_fake_connected() {
    let ws = TempWorkspace::new("omnidesk-fail-offline");
    let fake_uv = ws.bin_dir.join("uv");
    write_exec_script(
        &fake_uv,
        "#!/bin/sh\necho 'ImportError: simulated core failure Bearer tok1234567' >&2\nexit 1\n",
    );

    let mut host_env = HashMap::new();
    host_env.insert("PATH".to_string(), ws.bin_dir.display().to_string());
    let cfg = DaemonConfig {
        repo_root: Some(ws.repo_root.clone()),
        plugins_dir: Some(ws.plugins_dir.clone()),
        uv_bin: Some(fake_uv.clone()),
        python_bin: None,
        startup_timeout: Duration::from_secs(3),
        shutdown_grace: Duration::from_millis(100),
        poll_interval: Duration::from_millis(20),
    };
    let daemon = DaemonManager::with_env(ws.paths.clone(), cfg, host_env.clone());
    let client = ControlClient::new(ws.paths.clone());
    let bridge = EventBridge::new(ws.paths.clone());

    let snap = daemon.start_or_attach(Some(&bridge));
    assert_eq!(snap.status, "offline");
    assert!(!snap.owned_by_shell);
    let err_msg = snap.last_error.as_deref().unwrap_or("");
    assert!(err_msg.contains("simulated core failure"));
    assert!(!err_msg.contains("tok1234567"), "log tail error must be redacted");

    let status = block_on(exec_core_status_with_daemon(&client, &bridge, Some(&daemon)));
    assert_eq!(status["ok"], false);
    assert_eq!(status["error_code"], "service_offline");
    assert_eq!(status["data"]["online"], false);
    assert_eq!(status["data"]["degraded"], true);
    assert_eq!(status["data"]["daemon"]["status"], "offline");

    // Timeout case: child stays alive without binding control.sock -> killed and core_status stays offline
    write_exec_script(&fake_uv, "#!/usr/sbin/python3\nimport time\ntime.sleep(10)\n");
    let cfg_timeout = DaemonConfig {
        repo_root: Some(ws.repo_root.clone()),
        plugins_dir: Some(ws.plugins_dir.clone()),
        uv_bin: Some(fake_uv),
        python_bin: None,
        startup_timeout: Duration::from_millis(180),
        shutdown_grace: Duration::from_millis(80),
        poll_interval: Duration::from_millis(20),
    };
    let daemon_to = DaemonManager::with_env(ws.paths.clone(), cfg_timeout, host_env);
    let snap_to = daemon_to.start_or_attach(None);
    assert_eq!(snap_to.status, "offline");
    assert!(snap_to.last_error.as_deref().unwrap_or("").contains("Timed out"));

    let status_to = block_on(exec_core_status_with_daemon(&client, &bridge, Some(&daemon_to)));
    assert_eq!(status_to["ok"], false);
    assert_eq!(status_to["error_code"], "service_offline");
    assert_eq!(status_to["data"]["online"], false);

    // Ensure stale non-listening control.sock does not fool probe into "external"
    let _stale = StdUnixListener::bind(&ws.socket_path).unwrap();
    drop(_stale);
    fs::set_permissions(&ws.socket_path, fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(ws.paths.resolve_listening_socket().unwrap(), None);

    let rt = serde_json::to_value(standalone_runtime_status()).unwrap();
    assert_ne!(rt["status"], "skeleton");
}

#[test]
fn real_repo_daemon_launch_reports_honest_status_and_writes_log_file() {
    let ws = TempWorkspace::new("omnidesk-real-repo-probe");
    let real_repo = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("shell-tauri has repo root parent")
        .to_path_buf();

    let mut host_env: HashMap<String, String> = std::env::vars().collect();
    // Isolate XDG inside temp dir so real user runtime sockets are untouched.
    host_env.remove("XDG_RUNTIME_DIR");
    host_env.remove("XDG_DATA_HOME");
    host_env.remove("XDG_STATE_HOME");
    host_env.remove("XDG_CONFIG_HOME");

    let cfg = DaemonConfig {
        repo_root: Some(real_repo.clone()),
        plugins_dir: Some(real_repo.join("plugins")),
        uv_bin: None,
        python_bin: None,
        startup_timeout: Duration::from_secs(2),
        shutdown_grace: Duration::from_millis(200),
        poll_interval: Duration::from_millis(30),
    };
    let daemon = DaemonManager::with_env(ws.paths.clone(), cfg, host_env);
    let client = ControlClient::new(ws.paths.clone());
    let bridge = EventBridge::new(ws.paths.clone());

    let snap = daemon.start_or_attach(Some(&bridge));
    let log_path = ws.paths.daemon_log_path();
    assert!(log_path.is_file(), "daemon log file must be created");
    let log_contents = fs::read_to_string(&log_path).unwrap();
    assert!(log_contents.contains("[omnidesk-shell"));

    let status = block_on(exec_core_status_with_daemon(&client, &bridge, Some(&daemon)));
    if snap.status == "running" {
        assert_eq!(status["ok"], true);
        assert_eq!(status["data"]["online"], true);
    } else {
        assert_eq!(snap.status, "offline");
        assert_eq!(status["ok"], false);
        assert_eq!(status["error_code"], "service_offline");
        assert_eq!(status["data"]["online"], false);
    }
    daemon.stop();
}
