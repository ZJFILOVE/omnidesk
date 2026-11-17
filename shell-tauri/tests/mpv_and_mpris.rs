use std::collections::BTreeMap;
use std::fs;
use std::io::{BufRead, BufReader as StdBufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener as StdUnixListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use dbus::blocking::stdintf::org_freedesktop_dbus::Properties;
use dbus::blocking::Connection;
use omnidesk_lib::commands::{
    exec_mpv_open_args, exec_mpv_seek_args, exec_mpv_speed_args, exec_mpv_volume_args,
};
use omnidesk_lib::events::EventSink;
use omnidesk_lib::mpris::{
    sanitize_object_id, snapshot_from_mpv_state, MprisService, DEFAULT_MPRIS_BUS_NAME,
    MPRIS_OBJECT_PATH,
};
use omnidesk_lib::mpv::{probe, probe_with, MpvPlayer, MpvProbeConfig};
use omnidesk_lib::mpv_ipc::{
    apply_ipc_message, build_loadfile_options, parse_ipc_message, redact_media_url,
    validate_media_open_request, MediaOpenRequest, MpvIpcMessage, MpvPlaybackState,
    MPV_STATE_CHANNEL,
};
use omnidesk_lib::paths::OmnideskPaths;
use serde_json::{json, Value};

struct TempMediaEnv {
    root: PathBuf,
    paths: OmnideskPaths,
    bin_dir: PathBuf,
}

impl TempMediaEnv {
    fn new(prefix: &str) -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()));
        let cfg = root.join("cfg").join("omnidesk");
        let data = root.join("data").join("omnidesk");
        let rt = root.join("rt").join("omnidesk");
        let bin_dir = root.join("bin");
        for dir in [&cfg, &data, &rt, &bin_dir] {
            fs::create_dir_all(dir).unwrap();
        }
        for p in [&cfg, &data, &rt] {
            fs::set_permissions(p, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let paths = OmnideskPaths::for_dirs(Some(rt), data, cfg);
        Self {
            root,
            paths,
            bin_dir,
        }
    }
}

impl Drop for TempMediaEnv {
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

fn write_silent_wav(path: &Path, duration_ms: u32) {
    let sample_rate = 8000u32;
    let num_samples = (sample_rate * duration_ms) / 1000;
    let data_bytes = num_samples * 2;
    let riff_size = 36 + data_bytes;

    let mut buf = Vec::with_capacity(44 + data_bytes as usize);
    buf.extend_from_slice(b"RIFF");
    buf.extend_from_slice(&riff_size.to_le_bytes());
    buf.extend_from_slice(b"WAVEfmt ");
    buf.extend_from_slice(&16u32.to_le_bytes());
    buf.extend_from_slice(&1u16.to_le_bytes()); // PCM
    buf.extend_from_slice(&1u16.to_le_bytes()); // Mono
    buf.extend_from_slice(&sample_rate.to_le_bytes());
    buf.extend_from_slice(&(sample_rate * 2).to_le_bytes());
    buf.extend_from_slice(&2u16.to_le_bytes());
    buf.extend_from_slice(&16u16.to_le_bytes());
    buf.extend_from_slice(b"data");
    buf.extend_from_slice(&data_bytes.to_le_bytes());
    buf.resize(44 + data_bytes as usize, 0);
    fs::write(path, buf).unwrap();
}

#[test]
fn mpv_probe_reports_clear_unavailable_diagnostics_and_never_fakes_hwdec() {
    // 1. Missing binary + disabled libmpv probe -> unavailable with explicit reason
    let missing_cfg = MpvProbeConfig {
        mpv_bin_override: Some(PathBuf::from("/nonexistent/omnidesk-no-mpv")),
        path_env: Some(String::new()),
        allow_dlopen_libmpv: false,
        extra_spawn_args: Vec::new(),
    };
    let st_missing = probe_with(&missing_cfg, None);
    assert!(!st_missing.available);
    assert!(!st_missing.initialized);
    assert!(!st_missing.hwdec_active);
    assert_eq!(st_missing.hwdec, "unavailable");
    assert_eq!(st_missing.status, "unavailable");
    assert_ne!(st_missing.status, "skeleton");
    let reason = st_missing.unavailable_reason.unwrap();
    assert!(reason.contains("not found"));

    // 2. Binary present without --input-ipc-server -> unavailable with clear IPC diagnostic
    let env = TempMediaEnv::new("omnidesk-no-ipc");
    let fake_mpv = env.bin_dir.join("mpv");
    fs::write(
        &fake_mpv,
        "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'mpv 0.1.0-stub'; exit 0; fi\necho ' --quiet'\nexit 0\n",
    )
    .unwrap();
    fs::set_permissions(&fake_mpv, fs::Permissions::from_mode(0o755)).unwrap();
    let no_ipc_cfg = MpvProbeConfig {
        mpv_bin_override: Some(fake_mpv),
        path_env: None,
        allow_dlopen_libmpv: false,
        extra_spawn_args: Vec::new(),
    };
    let st_no_ipc = probe_with(&no_ipc_cfg, None);
    assert!(!st_no_ipc.available);
    assert!(!st_no_ipc.ipc_supported);
    assert_eq!(st_no_ipc.executable_version.as_deref(), Some("mpv 0.1.0-stub"));
    assert!(st_no_ipc
        .unavailable_reason
        .unwrap()
        .contains("--input-ipc-server"));

    // 3. Real host probe must never return hardcoded skeleton and never claim initialized/hwdecActive before playback
    let real = probe();
    assert_ne!(real.status, "skeleton");
    assert!(!real.initialized);
    assert!(!real.hwdec_active);
}

#[test]
fn mpv_loadfile_options_redaction_and_ipc_message_parsing_match_v1() {
    let mut headers = BTreeMap::new();
    headers.insert("Referer".to_string(), "https://www.bilibili.com".to_string());
    headers.insert("Cookie".to_string(), "SESSDATA=secret".to_string());
    headers.insert("X-Custom".to_string(), "val,1\"2".to_string());

    let req = MediaOpenRequest {
        url: "http://127.0.0.1:8765/api/v1/playback/track1?token=abc123".to_string(),
        audio_url: Some("https://cdn.example.com/audio.m4a".to_string()),
        subtitle_urls: vec!["/tmp/sub,1.srt".to_string(), "/tmp/sub2.vtt".to_string()],
        http_headers: headers,
        start_seconds: Some(12.5),
        end_seconds: Some(90.0),
        hwdec: Some(true),
        ..MediaOpenRequest::default()
    };
    validate_media_open_request(&req).unwrap();

    // Header injection with newline must fail validation, while build_loadfile_options also strips defensively
    let mut bad_hdr_req = req.clone();
    bad_hdr_req
        .http_headers
        .insert("X-Inject".to_string(), "v\nInjected".to_string());
    assert_eq!(
        validate_media_open_request(&bad_hdr_req).unwrap_err().code,
        "invalid_argument"
    );
    assert!(!build_loadfile_options(&bad_hdr_req).contains('\n'));
    let opts = build_loadfile_options(&req);
    assert!(opts.contains("referrer=\"https://www.bilibili.com\""));
    assert!(opts.contains("user-agent=\"OmniDesk/2.0\""));
    assert!(opts.contains("http-header-fields=\""));
    assert!(opts.contains("network-timeout=30,tls-verify=no,http-proxy=\"\""));
    assert!(opts.contains("stream-lavf-o=\"seekable=0\",force-seekable=yes"));
    assert!(opts.contains("hwdec=\"auto-safe\""));
    assert!(opts.contains("start=12.5"));
    assert!(opts.contains("end=90"));
    assert!(opts.contains("audio-file=\"https://cdn.example.com/audio.m4a\""));
    assert!(opts.contains(r#"sub-files="/tmp/sub\\,1.srt,/tmp/sub2.vtt""#));

    // Empty headers still emit explicit resets so prior stream headers cannot leak
    let clean_req = MediaOpenRequest {
        url: "https://example.com/video.mp4".to_string(),
        hwdec: Some(false),
        ..MediaOpenRequest::default()
    };
    let clean_opts = build_loadfile_options(&clean_req);
    assert!(clean_opts.contains("referrer=\"\",user-agent=\"OmniDesk/2.0\",http-header-fields=\"\""));
    assert!(clean_opts.contains("hwdec=\"no\",vid=no,audio-display=no"));

    // URL redaction scrubs query secrets
    let redacted = redact_media_url("https://example.com/stream.mp4?token=secret_123&sig=xyz");
    assert_eq!(redacted, "https://example.com/stream.mp4?<redacted>");

    // Unsafe targets rejected
    for bad in [
        "",
        "javascript:alert(1)",
        "file:///tmp/../etc/passwd",
        "../relative/video.mp4",
        "https://example.com/a\nb",
    ] {
        let bad_req = MediaOpenRequest {
            url: bad.to_string(),
            ..MediaOpenRequest::default()
        };
        assert!(validate_media_open_request(&bad_req).is_err(), "should reject {bad}");
    }

    // IPC message parsing & state reduction
    let reply = parse_ipc_message(r#"{"request_id":7,"error":"success","data":42}"#).unwrap();
    assert!(matches!(reply, MpvIpcMessage::Reply(ref r) if r.request_id == 7 && r.error == "success" && r.data == 42));

    let mut st = MpvPlaybackState::default();
    let restart = parse_ipc_message(r#"{"event":"playback-restart"}"#).unwrap();
    let _ = apply_ipc_message(&mut st, &restart);
    assert!(!st.stopped);

    let pause_false = parse_ipc_message(r#"{"event":"property-change","name":"pause","data":false}"#).unwrap();
    let _ = apply_ipc_message(&mut st, &pause_false);
    assert!(!st.paused && st.playing);

    let hwdec = parse_ipc_message(r#"{"event":"property-change","name":"hwdec-current","data":"vaapi"}"#).unwrap();
    let _ = apply_ipc_message(&mut st, &hwdec);
    assert_eq!(st.hwdec_current, "vaapi");
    assert!(st.hwdec_active);

    let hwdec_off = parse_ipc_message(r#"{"event":"property-change","name":"hwdec-current","data":"no"}"#).unwrap();
    let _ = apply_ipc_message(&mut st, &hwdec_off);
    assert!(!st.hwdec_active);

    let err_end = parse_ipc_message(r#"{"event":"end-file","reason":"error","file_error":"loading failed Bearer tok123456"}"#).unwrap();
    let _ = apply_ipc_message(&mut st, &err_end);
    assert!(!st.playing && st.stopped);
    assert!(!st.last_error.contains("tok123456"));
    assert!(st.last_error.contains("loading failed"));
}

#[test]
fn mpv_ipc_playback_control_roundtrip_and_honest_failure_states() {
    let env = TempMediaEnv::new("omnidesk-mpv-mock-ipc");
    let sink = Arc::new(RecordingSink::default());
    let player = MpvPlayer::new(env.paths.clone());
    player.set_event_sink(sink.clone());

    // Before initialization, pause/resume/seek/volume MUST fail honestly (never fake success)
    assert_eq!(player.pause().unwrap_err().code, "service_offline");
    assert_eq!(player.resume().unwrap_err().code, "service_offline");
    assert_eq!(player.seek(10.0, false).unwrap_err().code, "service_offline");
    assert_eq!(player.set_volume(50).unwrap_err().code, "service_offline");

    // Also fail honestly if probe says unavailable and open is attempted
    let unavailable_player = MpvPlayer::with_config(
        env.paths.clone(),
        MpvProbeConfig {
            mpv_bin_override: Some(PathBuf::from("/nonexistent/mpv")),
            path_env: Some(String::new()),
            allow_dlopen_libmpv: false,
            extra_spawn_args: Vec::new(),
        },
    );
    let open_fail = exec_mpv_open_args(&unavailable_player, &json!({"url": "https://example.com/a.mp4"}));
    assert_eq!(open_fail["ok"], false);
    assert_eq!(open_fail["error_code"], "service_offline");
    assert!(!unavailable_player.status().playing);

    // Start mock mpv IPC Unix socket server
    let mock_sock = env.paths.mpv_socket_path();
    fs::create_dir_all(mock_sock.parent().unwrap()).unwrap();
    fs::set_permissions(mock_sock.parent().unwrap(), fs::Permissions::from_mode(0o700)).unwrap();
    let listener = StdUnixListener::bind(&mock_sock).unwrap();
    let recorded_cmds: Arc<Mutex<Vec<Value>>> = Arc::new(Mutex::new(Vec::new()));
    let cmds_srv = Arc::clone(&recorded_cmds);

    let server_thread = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut reader = StdBufReader::new(stream.try_clone().unwrap());
        let mut cur_pos = 0.0_f64;
        let mut line = String::new();
        while reader.read_line(&mut line).unwrap() > 0 {
            let msg: Value = serde_json::from_str(&line).unwrap();
            let req_id = msg["request_id"].as_u64().unwrap();
            let cmd = msg["command"].as_array().unwrap();
            let name = cmd[0].as_str().unwrap();

            if name != "observe_property" {
                cmds_srv.lock().unwrap().push(msg["command"].clone());
            }

            let resp = match name {
                "observe_property" => json!({"request_id": req_id, "error": "success", "data": null}),
                "get_property" if cmd[1] == "command-list" => json!({
                    "request_id": req_id,
                    "error": "success",
                    "data": [{"name": "loadfile", "args": [{"name": "url"}, {"name": "flags"}, {"name": "index"}, {"name": "options"}]}],
                }),
                "get_property" if cmd[1] == "playback-time" => json!({"request_id": req_id, "error": "success", "data": cur_pos}),
                "set_property" => {
                    if cmd[1] == "volume" {
                        let ev = json!({"event": "property-change", "name": "volume", "data": cmd[2]});
                        let _ = writeln!(stream, "{ev}");
                    }
                    json!({"request_id": req_id, "error": "success", "data": null})
                }
                "loadfile" => {
                    let ack = json!({"request_id": req_id, "error": "success", "data": null});
                    let _ = writeln!(stream, "{ack}");
                    let dur_ev = json!({"event": "property-change", "name": "duration", "data": 180.0});
                    let hw_ev = json!({"event": "property-change", "name": "hwdec-current", "data": "vaapi"});
                    let restart_ev = json!({"event": "playback-restart"});
                    let _ = writeln!(stream, "{dur_ev}\n{hw_ev}\n{restart_ev}");
                    line.clear();
                    continue;
                }
                "seek" => {
                    let secs = cmd[1].as_f64().unwrap();
                    let mode = cmd[2].as_str().unwrap();
                    cur_pos = if mode == "relative" { (cur_pos + secs).max(0.0) } else { secs.max(0.0) };
                    json!({"request_id": req_id, "error": "success", "data": null})
                }
                "stop" => json!({"request_id": req_id, "error": "success", "data": null}),
                "quit" => {
                    let ack = json!({"request_id": req_id, "error": "success", "data": null});
                    let _ = writeln!(stream, "{ack}");
                    break;
                }
                _ => json!({"request_id": req_id, "error": "unknown command"}),
            };
            let _ = writeln!(stream, "{resp}");
            let _ = stream.flush();
            line.clear();
        }
    });

    player.attach_socket(&mock_sock).unwrap();

    let open_res = exec_mpv_open_args(
        &player,
        &json!({"args": {"url": "https://cdn.example.com/movie.mp4?token=secret99", "title": "Test Video", "artist": "Director"}}),
    );
    assert_eq!(open_res["ok"], true);
    assert_eq!(open_res["data"]["playing"], true);
    assert_eq!(
        open_res["data"]["currentUrl"],
        "https://cdn.example.com/movie.mp4?<redacted>"
    );

    // Wait briefly for duration/hwdec events to drain from reader thread
    for _ in 0..40 {
        let s = player.status();
        if s.duration_sec > 0.0 && s.hwdec_active {
            break;
        }
        std::thread::sleep(Duration::from_millis(15));
    }
    let live = player.status();
    assert_eq!(live.duration_sec, 180.0);
    assert_eq!(live.hwdec_current, "vaapi");
    assert!(live.hwdec_active);

    let paused = player.pause().unwrap();
    assert!(paused.paused && !paused.playing);

    let resumed = player.resume().unwrap();
    assert!(!resumed.paused && resumed.playing);

    let seek_abs = exec_mpv_seek_args(&player, &json!({"seconds": 42.5, "mode": "absolute"}));
    assert_eq!(seek_abs["ok"], true);
    assert_eq!(seek_abs["data"]["positionSec"], 42.5);

    let seek_rel = exec_mpv_seek_args(&player, &json!({"args": {"seconds": 10.0, "relative": true}}));
    assert_eq!(seek_rel["ok"], true);
    assert_eq!(seek_rel["data"]["positionSec"], 52.5);

    let vol_res = exec_mpv_volume_args(&player, &json!({"volume": 65}));
    assert_eq!(vol_res["ok"], true);
    assert_eq!(vol_res["data"]["volume"], 65);

    let spd_res = exec_mpv_speed_args(&player, &json!({"speed": 1.5}));
    assert_eq!(spd_res["ok"], true);
    assert_eq!(spd_res["data"]["speed"], 1.5);

    // Invalid command arguments fail closed
    assert_eq!(exec_mpv_seek_args(&player, &json!({})), json!({
        "ok": false,
        "error_code": "invalid_argument",
        "error": "Missing required argument 'seconds'",
        "provider_plugin_id": "",
        "data": {"error_code": "invalid_argument"}
    }));
    assert_eq!(
        exec_mpv_seek_args(&player, &json!({"seconds": 5.0, "relative": true, "mode": "absolute"}))["error_code"],
        "invalid_argument"
    );
    assert_eq!(
        exec_mpv_volume_args(&player, &json!({"volume": "loud"}))["error_code"],
        "invalid_argument"
    );

    let stopped = player.stop_playback().unwrap();
    assert!(stopped.stopped && !stopped.playing);

    player.shutdown();
    server_thread.join().unwrap();

    let cmds = recorded_cmds.lock().unwrap();
    assert!(cmds.iter().any(|c| c[0] == "loadfile" && c[1] == "https://cdn.example.com/movie.mp4?token=secret99"));
    assert!(cmds.iter().any(|c| c == &json!(["set_property", "pause", true])));
    assert!(cmds.iter().any(|c| c == &json!(["set_property", "pause", false])));
    assert!(cmds.iter().any(|c| c == &json!(["seek", 42.5, "absolute"])));
    assert!(cmds.iter().any(|c| c == &json!(["seek", 10.0, "relative"])));
    assert!(cmds.iter().any(|c| c == &json!(["set_property", "volume", 65])));
    assert!(cmds.iter().any(|c| c == &json!(["set_property", "speed", 1.5])));
    assert!(cmds.iter().any(|c| c == &json!(["stop"])));

    let emitted = sink.events();
    assert!(emitted.iter().any(|(ch, v)| ch == MPV_STATE_CHANNEL && v["playing"] == true));
    assert!(emitted.iter().any(|(ch, v)| ch == MPV_STATE_CHANNEL && v["hwdecCurrent"] == "vaapi"));
}

#[test]
fn real_mpv_binary_ipc_playback_smoke_if_installed() {
    let probe_res = probe();
    if !probe_res.available {
        return;
    }

    let env = TempMediaEnv::new("omnidesk-real-mpv");
    let wav_path = env.root.join("silence.wav");
    write_silent_wav(&wav_path, 800);

    let cfg = MpvProbeConfig {
        extra_spawn_args: vec!["--ao=null".to_string(), "--vo=null".to_string()],
        ..MpvProbeConfig::default()
    };
    let player = MpvPlayer::with_config(env.paths.clone(), cfg);

    let st_open = player
        .open(MediaOpenRequest {
            url: wav_path.display().to_string(),
            hwdec: Some(false),
            title: Some("Silence WAV".to_string()),
            ..MediaOpenRequest::default()
        })
        .unwrap();
    assert!(st_open.initialized);

    // Wait briefly for real mpv to decode duration & audio codec
    for _ in 0..40 {
        let st = player.status();
        if st.duration_sec > 0.0 && !st.audio_codec.is_empty() {
            break;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    let live = player.status();
    assert!(live.duration_sec > 0.4, "real mpv should report wav duration: {live:?}");
    assert!(!live.audio_codec.is_empty());

    let p = player.pause().unwrap();
    assert!(p.paused);
    let r = player.resume().unwrap();
    assert!(!r.paused);
    let s = player.seek(0.3, false).unwrap();
    assert!(s.position_sec >= 0.0);
    let v = player.set_volume(60).unwrap();
    assert_eq!(v.volume, 60);

    // Nonexistent media must emit end-file error and set stopped=true (never stay stuck playing)
    let _ = player.open(MediaOpenRequest {
        url: env.root.join("missing.mp4").display().to_string(),
        hwdec: Some(false),
        ..MediaOpenRequest::default()
    });
    for _ in 0..40 {
        let st = player.status();
        if st.stopped && !st.last_error.is_empty() {
            break;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    let failed_st = player.status();
    assert!(!failed_st.playing);
    assert!(failed_st.stopped);
    assert!(!failed_st.last_error.is_empty());

    player.shutdown();
    assert!(!env.paths.mpv_socket_path().exists());
}

#[test]
fn mpris2_snapshot_disabled_fallback_and_isolated_dbus_control() {
    // 1. Pure snapshot verification
    assert_eq!(sanitize_object_id("track:123/abc"), "track_3a123_2fabc");
    let mut st = MpvPlaybackState::default();
    st.initialized = true;
    st.stopped = false;
    st.paused = false;
    st.playing = true;
    st.volume = 70;
    st.speed = 1.25;
    st.position_sec = 12.5;
    st.duration_sec = 120.0;
    st.item_id = "am-us-42".to_string();
    st.title = "Track Title".to_string();
    st.artist = "Artist".to_string();
    st.album = "Album".to_string();

    let snap = snapshot_from_mpv_state(&st);
    assert_eq!(snap.playback_status, "Playing");
    assert_eq!(snap.track_object_path, "/org/mpris/MediaPlayer2/Track/am_2dus_2d42");
    assert!((snap.volume - 0.7).abs() < 1e-6);
    assert_eq!(snap.rate, 1.25);
    assert_eq!(snap.length_us, 120_000_000);
    assert_eq!(snap.position_us, 12_500_000);
    assert!(snap.can_play && snap.can_pause && snap.can_seek);

    st.paused = true;
    st.playing = false;
    assert_eq!(snapshot_from_mpv_state(&st).playback_status, "Paused");
    st.stopped = true;
    assert_eq!(snapshot_from_mpv_state(&st).playback_status, "Stopped");

    // 2. Honest disabled fallback when D-Bus address is unavailable
    let env = TempMediaEnv::new("omnidesk-mpris-test");
    let player = MpvPlayer::new(env.paths.clone());
    let mpris_bad = MprisService::new();
    let bad_status = mpris_bad.start_with_address(
        Some("unix:path=/nonexistent/omnidesk-missing-dbus.sock"),
        Arc::clone(&player),
        None,
    );
    assert!(!bad_status.enabled);
    assert_eq!(bad_status.status, "disabled");
    assert!(!bad_status.reason.is_empty());

    // 3. End-to-end isolated dbus-daemon verification if `dbus-daemon` binary is installed
    let Ok(dbus_bin) = which_dbus_daemon() else {
        return;
    };
    let dbus_sock = env.root.join("dbus.sock");
    let dbus_addr = format!("unix:path={}", dbus_sock.display());
    let mut daemon = Command::new(dbus_bin)
        .args([
            "--session",
            "--nofork",
            "--nopidfile",
            &format!("--address={dbus_addr}"),
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();

    for _ in 0..40 {
        if dbus_sock.exists() {
            break;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    if !dbus_sock.exists() {
        let _ = daemon.kill();
        let _ = daemon.wait();
        return;
    }

    // Mock mpv IPC socket so MPRIS Play/Pause triggers real IPC commands
    let mpv_sock = env.paths.mpv_socket_path();
    fs::create_dir_all(mpv_sock.parent().unwrap()).unwrap();
    fs::set_permissions(mpv_sock.parent().unwrap(), fs::Permissions::from_mode(0o700)).unwrap();
    let mpv_listener = StdUnixListener::bind(&mpv_sock).unwrap();
    let received_cmds: Arc<Mutex<Vec<Value>>> = Arc::new(Mutex::new(Vec::new()));
    let cmds_copy = Arc::clone(&received_cmds);
    let mpv_srv = std::thread::spawn(move || {
        let (mut s, _) = mpv_listener.accept().unwrap();
        let mut r = StdBufReader::new(s.try_clone().unwrap());
        let mut line = String::new();
        while r.read_line(&mut line).unwrap() > 0 {
            let req: Value = serde_json::from_str(&line).unwrap();
            let req_id = req["request_id"].as_u64().unwrap();
            let cmd = req["command"].as_array().unwrap();
            if cmd[0] != "observe_property" {
                cmds_copy.lock().unwrap().push(req["command"].clone());
            }
            let resp = if cmd[0] == "get_property" && cmd[1] == "command-list" {
                json!({"request_id": req_id, "error": "success", "data": []})
            } else {
                json!({"request_id": req_id, "error": "success", "data": null})
            };
            let _ = writeln!(s, "{resp}");
            let _ = s.flush();
            if cmd[0] == "quit" {
                break;
            }
            line.clear();
        }
    });

    player.attach_socket(&mpv_sock).unwrap();
    let mpris = MprisService::new();
    let started = mpris.start_with_address(Some(&dbus_addr), Arc::clone(&player), None);
    assert!(started.enabled, "MPRIS2 should start against isolated dbus-daemon: {started:?}");
    assert_eq!(started.status, "active");
    assert_eq!(started.bus_name, DEFAULT_MPRIS_BUS_NAME);

    // Client proxy queries Properties & calls Pause/Play/Volume
    let client_conn = Connection::new_address(&dbus_addr).unwrap();
    let proxy = client_conn.with_proxy(
        DEFAULT_MPRIS_BUS_NAME,
        MPRIS_OBJECT_PATH,
        Duration::from_secs(2),
    );
    let identity: String = proxy.get("org.mpris.MediaPlayer2", "Identity").unwrap();
    assert_eq!(identity, "OmniDesk");
    let playback_status: String = proxy
        .get("org.mpris.MediaPlayer2.Player", "PlaybackStatus")
        .unwrap();
    assert_eq!(playback_status, "Stopped");

    let _: () = proxy
        .method_call("org.mpris.MediaPlayer2.Player", "Pause", ())
        .unwrap();
    let _: () = proxy
        .method_call("org.mpris.MediaPlayer2.Player", "Play", ())
        .unwrap();
    let _: () = proxy
        .method_call("org.mpris.MediaPlayer2.Player", "Seek", (5_000_000_i64,))
        .unwrap();
    proxy
        .set("org.mpris.MediaPlayer2.Player", "Volume", 0.4_f64)
        .unwrap();

    mpris.stop();
    player.shutdown();
    mpv_srv.join().unwrap();
    let _ = daemon.kill();
    let _ = daemon.wait();

    let cmds = received_cmds.lock().unwrap();
    assert!(cmds.iter().any(|c| c == &json!(["set_property", "pause", true])));
    assert!(cmds.iter().any(|c| c == &json!(["set_property", "pause", false])));
    assert!(cmds.iter().any(|c| c == &json!(["seek", 5.0, "relative"])));
    assert!(cmds.iter().any(|c| c == &json!(["set_property", "volume", 40])));
}

fn which_dbus_daemon() -> Result<PathBuf, ()> {
    if let Ok(path_var) = std::env::var("PATH") {
        for dir in std::env::split_paths(&path_var) {
            let cand = dir.join("dbus-daemon");
            if cand.is_file() {
                return Ok(cand);
            }
        }
    }
    Err(())
}
