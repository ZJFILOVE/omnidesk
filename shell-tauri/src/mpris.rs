//! Linux MPRIS2 D-Bus service (`org.mpris.MediaPlayer2.omnidesk`) with honest disabled fallback.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::thread::JoinHandle;
use std::time::Duration;

use dbus::arg::{self, PropMap, RefArg, Variant};
use dbus::blocking::stdintf::org_freedesktop_dbus::{PropertiesPropertiesChanged, RequestNameReply};
use dbus::blocking::Connection;
use dbus::channel::MatchingReceiver;
use dbus::message::{MatchRule, SignalArgs};
use dbus::strings::ErrorName;
use dbus::{Message, Path as DbusPath};
use serde::Serialize;
use serde_json::json;

use crate::events::EventSink;
use crate::mpv::MpvPlayer;
use crate::mpv_ipc::{MediaOpenRequest, MpvPlaybackState};
use crate::redact::redact_text;

pub const DEFAULT_MPRIS_BUS_NAME: &str = "org.mpris.MediaPlayer2.omnidesk";
pub const MPRIS_OBJECT_PATH: &str = "/org/mpris/MediaPlayer2";
pub const MPRIS_ACTION_CHANNEL: &str = "omnidesk:mpris-action";

const MPRIS_INTROSPECT_XML: &str = r#"<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect">
      <arg name="xml_data" type="s" direction="out"/>
    </method>
  </interface>
  <interface name="org.freedesktop.DBus.Peer">
    <method name="Ping"/>
    <method name="GetMachineId">
      <arg name="machine_uuid" type="s" direction="out"/>
    </method>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg name="interface_name" type="s" direction="in"/>
      <arg name="property_name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg name="interface_name" type="s" direction="in"/>
      <arg name="properties" type="a{sv}" direction="out"/>
    </method>
    <method name="Set">
      <arg name="interface_name" type="s" direction="in"/>
      <arg name="property_name" type="s" direction="in"/>
      <arg name="value" type="v" direction="in"/>
    </method>
    <signal name="PropertiesChanged">
      <arg name="interface_name" type="s"/>
      <arg name="changed_properties" type="a{sv}"/>
      <arg name="invalidated_properties" type="as"/>
    </signal>
  </interface>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg name="Offset" type="x" direction="in"/>
    </method>
    <method name="SetPosition">
      <arg name="TrackId" type="o" direction="in"/>
      <arg name="Position" type="x" direction="in"/>
    </method>
    <method name="OpenUri">
      <arg name="Uri" type="s" direction="in"/>
    </method>
    <signal name="Seeked">
      <arg name="Position" type="x"/>
    </signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"#;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct MprisStatus {
    pub enabled: bool,
    pub bus_name: String,
    pub status: String,
    pub reason: String,
}

impl Default for MprisStatus {
    fn default() -> Self {
        Self::disabled("MPRIS2 service not started")
    }
}

impl MprisStatus {
    pub fn disabled(reason: impl Into<String>) -> Self {
        Self {
            enabled: false,
            bus_name: DEFAULT_MPRIS_BUS_NAME.to_string(),
            status: "disabled".to_string(),
            reason: redact_text(&reason.into()),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct MprisSnapshot {
    pub playback_status: String,
    pub loop_status: String,
    pub track_object_path: String,
    pub title: String,
    pub artists: Vec<String>,
    pub album: String,
    pub art_url: String,
    pub length_us: i64,
    pub position_us: i64,
    pub volume: f64,
    pub rate: f64,
    pub shuffle: bool,
    pub can_go_next: bool,
    pub can_go_previous: bool,
    pub can_play: bool,
    pub can_pause: bool,
    pub can_seek: bool,
}

impl Default for MprisSnapshot {
    fn default() -> Self {
        Self {
            playback_status: "Stopped".to_string(),
            loop_status: "None".to_string(),
            track_object_path: "/org/mpris/MediaPlayer2/TrackList/NoTrack".to_string(),
            title: String::new(),
            artists: Vec::new(),
            album: String::new(),
            art_url: String::new(),
            length_us: 0,
            position_us: 0,
            volume: 0.85,
            rate: 1.0,
            shuffle: false,
            can_go_next: false,
            can_go_previous: false,
            can_play: false,
            can_pause: false,
            can_seek: false,
        }
    }
}

pub fn sanitize_object_id(raw: &str) -> String {
    if raw.is_empty() {
        return "track_0".to_string();
    }
    let mut out = String::with_capacity(raw.len() + 4);
    for ch in raw.bytes() {
        if ch.is_ascii_alphanumeric() || ch == b'_' {
            out.push(ch as char);
        } else {
            out.push_str(&format!("_{ch:02x}"));
        }
        if out.len() >= 96 {
            break;
        }
    }
    if out.is_empty() {
        "track_0".to_string()
    } else {
        out
    }
}

pub fn snapshot_from_mpv_state(st: &MpvPlaybackState) -> MprisSnapshot {
    let playback_status = if st.stopped {
        "Stopped"
    } else if st.paused || !st.playing {
        "Paused"
    } else {
        "Playing"
    };
    let track_id_src = if !st.item_id.is_empty() {
        st.item_id.as_str()
    } else if !st.current_url.is_empty() {
        st.current_url.as_str()
    } else {
        ""
    };
    let track_object_path = if track_id_src.is_empty() {
        "/org/mpris/MediaPlayer2/TrackList/NoTrack".to_string()
    } else {
        format!("/org/mpris/MediaPlayer2/Track/{}", sanitize_object_id(track_id_src))
    };
    let artists = if st.artist.trim().is_empty() {
        Vec::new()
    } else {
        vec![st.artist.trim().to_string()]
    };
    let has_media = !st.current_url.is_empty() || !st.item_id.is_empty();
    let pos_us = (st.position_sec.max(0.0) * 1_000_000.0) as i64;
    let len_us = (st.duration_sec.max(0.0) * 1_000_000.0) as i64;
    MprisSnapshot {
        playback_status: playback_status.to_string(),
        loop_status: "None".to_string(),
        track_object_path,
        title: st.title.clone(),
        artists,
        album: st.album.clone(),
        art_url: st.cover_url.clone(),
        length_us: len_us,
        position_us: pos_us,
        volume: ((st.volume as f64) / 100.0).clamp(0.0, 1.0),
        rate: if st.speed > 0.0 && st.speed.is_finite() {
            st.speed
        } else {
            1.0
        },
        shuffle: false,
        can_go_next: false,
        can_go_previous: false,
        can_play: has_media && st.initialized,
        can_pause: has_media && st.initialized && !st.stopped,
        can_seek: has_media && st.initialized && !st.stopped && st.duration_sec > 0.0,
    }
}

pub struct MprisService {
    status: RwLock<MprisStatus>,
    dirty: Arc<AtomicBool>,
    stop_flag: Arc<AtomicBool>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

impl MprisService {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            status: RwLock::new(MprisStatus::default()),
            dirty: Arc::new(AtomicBool::new(false)),
            stop_flag: Arc::new(AtomicBool::new(false)),
            thread: Mutex::new(None),
        })
    }

    pub fn status(&self) -> MprisStatus {
        self.status
            .read()
            .map(|guard| guard.clone())
            .unwrap_or_default()
    }

    pub fn notify_state_changed(&self) {
        self.dirty.store(true, Ordering::Release);
    }

    pub fn start(&self, mpv: Arc<MpvPlayer>, sink: Option<Arc<dyn EventSink>>) -> MprisStatus {
        self.start_with_address(None, mpv, sink)
    }

    pub fn start_with_address(
        &self,
        bus_address: Option<&str>,
        mpv: Arc<MpvPlayer>,
        sink: Option<Arc<dyn EventSink>>,
    ) -> MprisStatus {
        let mut thread_guard = self.thread.lock().unwrap_or_else(|e| e.into_inner());
        if thread_guard.is_some() {
            return self.status();
        }

        let conn_res = match bus_address {
            Some(addr) if !addr.trim().is_empty() => Connection::new_address(addr),
            Some(_) => Err(dbus::Error::new_failed("Empty D-Bus session address")),
            None => Connection::new_session(),
        };
        let conn = match conn_res {
            Ok(c) => c,
            Err(err) => {
                let st = MprisStatus::disabled(format!("D-Bus session bus unavailable: {err}"));
                self.set_status(st.clone());
                return st;
            }
        };

        let mut chosen_name = DEFAULT_MPRIS_BUS_NAME.to_string();
        match conn.request_name(&chosen_name, false, false, true) {
            Ok(RequestNameReply::PrimaryOwner | RequestNameReply::AlreadyOwner) => {}
            Ok(_) | Err(_) => {
                chosen_name = format!("{DEFAULT_MPRIS_BUS_NAME}.instance{}", std::process::id());
                if let Err(err) = conn.request_name(&chosen_name, false, false, true) {
                    let st = MprisStatus::disabled(format!(
                        "Failed acquiring MPRIS2 D-Bus name '{chosen_name}': {err}"
                    ));
                    self.set_status(st.clone());
                    return st;
                }
            }
        }

        self.stop_flag.store(false, Ordering::Release);
        self.dirty.store(false, Ordering::Release);

        let dirty_obs = Arc::clone(&self.dirty);
        mpv.set_state_observer(Arc::new(move |_| {
            dirty_obs.store(true, Ordering::Release);
        }));

        let dirty_loop = Arc::clone(&self.dirty);
        let stop_loop = Arc::clone(&self.stop_flag);
        let bus_name_owned = chosen_name.clone();

        let handle = std::thread::Builder::new()
            .name("omnidesk-mpris2".to_string())
            .spawn(move || {
                run_mpris_loop(conn, &bus_name_owned, mpv, sink, dirty_loop, stop_loop);
            });

        match handle {
            Ok(h) => {
                *thread_guard = Some(h);
                let st = MprisStatus {
                    enabled: true,
                    bus_name: chosen_name,
                    status: "active".to_string(),
                    reason: "MPRIS2 D-Bus media player registered".to_string(),
                };
                self.set_status(st.clone());
                st
            }
            Err(err) => {
                let st = MprisStatus::disabled(format!("Failed spawning MPRIS2 worker thread: {err}"));
                self.set_status(st.clone());
                st
            }
        }
    }

    pub fn stop(&self) {
        self.stop_flag.store(true, Ordering::Release);
        if let Ok(mut guard) = self.thread.lock() {
            if let Some(handle) = guard.take() {
                let _ = handle.join();
            }
        }
        let was_enabled = self.status().enabled;
        if was_enabled {
            self.set_status(MprisStatus::disabled("MPRIS2 service stopped"));
        }
    }

    fn set_status(&self, st: MprisStatus) {
        if let Ok(mut guard) = self.status.write() {
            *guard = st;
        }
    }
}

impl Drop for MprisService {
    fn drop(&mut self) {
        self.stop();
    }
}

fn run_mpris_loop(
    conn: Connection,
    bus_name: &str,
    mpv: Arc<MpvPlayer>,
    sink: Option<Arc<dyn EventSink>>,
    dirty: Arc<AtomicBool>,
    stop_flag: Arc<AtomicBool>,
) {
    let mpv_handler = Arc::clone(&mpv);
    let sink_handler = sink.clone();
    let rule = MatchRule::new_method_call().with_path(MPRIS_OBJECT_PATH);
    let token = conn.start_receive(
        rule,
        Box::new(move |msg, c| {
            handle_method_call(&msg, c, &mpv_handler, sink_handler.as_deref());
            true
        }),
    );

    let mut last_snap = snapshot_from_mpv_state(&mpv.status());
    while !stop_flag.load(Ordering::Acquire) {
        if dirty.swap(false, Ordering::AcqRel) {
            let next_snap = snapshot_from_mpv_state(&mpv.status());
            if snapshot_needs_signal(&last_snap, &next_snap) {
                emit_properties_changed(&conn, &next_snap);
                last_snap = next_snap;
            }
        }
        let _ = conn.process(Duration::from_millis(100));
    }

    let _ = conn.stop_receive(token);
    let _ = conn.release_name(bus_name);
}

fn snapshot_needs_signal(a: &MprisSnapshot, b: &MprisSnapshot) -> bool {
    a.playback_status != b.playback_status
        || a.title != b.title
        || a.track_object_path != b.track_object_path
        || (a.volume - b.volume).abs() > 0.005
        || (a.rate - b.rate).abs() > 0.005
        || a.can_play != b.can_play
        || a.can_pause != b.can_pause
        || a.can_seek != b.can_seek
}

fn emit_properties_changed(conn: &Connection, snap: &MprisSnapshot) {
    let changed = player_properties_map(snap);
    let sig = PropertiesPropertiesChanged {
        interface_name: "org.mpris.MediaPlayer2.Player".to_string(),
        changed_properties: changed,
        invalidated_properties: Vec::new(),
    };
    let msg = sig.to_emit_message(&DbusPath::from(MPRIS_OBJECT_PATH));
    let _ = conn.channel().send(msg);
}

fn handle_method_call(
    msg: &Message,
    conn: &Connection,
    mpv: &MpvPlayer,
    sink: Option<&dyn EventSink>,
) {
    let iface = msg.interface().map(|i| i.to_string()).unwrap_or_default();
    let member = msg.member().map(|m| m.to_string()).unwrap_or_default();

    let reply = match (iface.as_str(), member.as_str()) {
        ("org.freedesktop.DBus.Introspectable", "Introspect") => {
            msg.method_return().append1(MPRIS_INTROSPECT_XML)
        }
        ("org.freedesktop.DBus.Peer", "Ping") => msg.method_return(),
        ("org.freedesktop.DBus.Peer", "GetMachineId") => {
            let mid = std::fs::read_to_string("/etc/machine-id")
                .map(|s| s.trim().to_string())
                .unwrap_or_else(|_| "omnidesk".to_string());
            msg.method_return().append1(mid)
        }
        ("org.freedesktop.DBus.Properties", "Get") => handle_props_get(msg, mpv),
        ("org.freedesktop.DBus.Properties", "GetAll") => handle_props_get_all(msg, mpv),
        ("org.freedesktop.DBus.Properties", "Set") => handle_props_set(msg, mpv),
        ("org.mpris.MediaPlayer2", "Raise") => {
            if let Some(s) = sink {
                s.emit_event(MPRIS_ACTION_CHANNEL, json!({ "action": "raise" }));
            }
            msg.method_return()
        }
        ("org.mpris.MediaPlayer2", "Quit") => msg.method_return(),
        ("org.mpris.MediaPlayer2.Player", "Play") => match mpv.resume() {
            Ok(_) => msg.method_return(),
            Err(err) => dbus_err(msg, "org.mpris.MediaPlayer2.Error", &err.message),
        },
        ("org.mpris.MediaPlayer2.Player", "Pause") => match mpv.pause() {
            Ok(_) => msg.method_return(),
            Err(err) => dbus_err(msg, "org.mpris.MediaPlayer2.Error", &err.message),
        },
        ("org.mpris.MediaPlayer2.Player", "PlayPause") => match mpv.toggle_pause() {
            Ok(_) => msg.method_return(),
            Err(err) => dbus_err(msg, "org.mpris.MediaPlayer2.Error", &err.message),
        },
        ("org.mpris.MediaPlayer2.Player", "Stop") => match mpv.stop_playback() {
            Ok(_) => msg.method_return(),
            Err(err) => dbus_err(msg, "org.mpris.MediaPlayer2.Error", &err.message),
        },
        ("org.mpris.MediaPlayer2.Player", "Next") => {
            if let Some(s) = sink {
                s.emit_event(MPRIS_ACTION_CHANNEL, json!({ "action": "next" }));
            }
            msg.method_return()
        }
        ("org.mpris.MediaPlayer2.Player", "Previous") => {
            if let Some(s) = sink {
                s.emit_event(MPRIS_ACTION_CHANNEL, json!({ "action": "previous" }));
            }
            msg.method_return()
        }
        ("org.mpris.MediaPlayer2.Player", "Seek") => {
            let offset_us: i64 = match msg.read1() {
                Ok(v) => v,
                Err(err) => return send_reply(conn, dbus_err(msg, "org.freedesktop.DBus.Error.InvalidArgs", &err.to_string())),
            };
            match mpv.seek((offset_us as f64) / 1_000_000.0, true) {
                Ok(_) => msg.method_return(),
                Err(err) => dbus_err(msg, "org.mpris.MediaPlayer2.Error", &err.message),
            }
        }
        ("org.mpris.MediaPlayer2.Player", "SetPosition") => {
            let (_track_id, pos_us): (DbusPath<'_>, i64) = match msg.read2() {
                Ok(v) => v,
                Err(err) => return send_reply(conn, dbus_err(msg, "org.freedesktop.DBus.Error.InvalidArgs", &err.to_string())),
            };
            if pos_us >= 0 {
                match mpv.seek((pos_us as f64) / 1_000_000.0, false) {
                    Ok(_) => msg.method_return(),
                    Err(err) => dbus_err(msg, "org.mpris.MediaPlayer2.Error", &err.message),
                }
            } else {
                msg.method_return()
            }
        }
        ("org.mpris.MediaPlayer2.Player", "OpenUri") => {
            let uri: String = match msg.read1() {
                Ok(v) => v,
                Err(err) => return send_reply(conn, dbus_err(msg, "org.freedesktop.DBus.Error.InvalidArgs", &err.to_string())),
            };
            let req = MediaOpenRequest {
                url: uri,
                ..MediaOpenRequest::default()
            };
            match mpv.open(req) {
                Ok(_) => msg.method_return(),
                Err(err) => dbus_err(msg, "org.mpris.MediaPlayer2.Error", &err.message),
            }
        }
        _ => dbus_err(
            msg,
            "org.freedesktop.DBus.Error.UnknownMethod",
            &format!("Unknown method {iface}.{member}"),
        ),
    };

    send_reply(conn, reply);
}

fn handle_props_get(msg: &Message, mpv: &MpvPlayer) -> Message {
    let Ok((iface, prop)): Result<(&str, &str), _> = msg.read2() else {
        return dbus_err(
            msg,
            "org.freedesktop.DBus.Error.InvalidArgs",
            "Expected (interface_name, property_name)",
        );
    };
    let snap = snapshot_from_mpv_state(&mpv.status());
    let mut map = match iface {
        "org.mpris.MediaPlayer2" => root_properties_map(),
        "org.mpris.MediaPlayer2.Player" => player_properties_map(&snap),
        _ => {
            return dbus_err(
                msg,
                "org.freedesktop.DBus.Error.UnknownInterface",
                &format!("Unknown interface {iface}"),
            );
        }
    };
    if let Some(val) = map.remove(prop) {
        msg.method_return().append1(val)
    } else {
        dbus_err(
            msg,
            "org.freedesktop.DBus.Error.UnknownProperty",
            &format!("Unknown property {iface}.{prop}"),
        )
    }
}

fn handle_props_get_all(msg: &Message, mpv: &MpvPlayer) -> Message {
    let Ok(iface): Result<&str, _> = msg.read1() else {
        return dbus_err(
            msg,
            "org.freedesktop.DBus.Error.InvalidArgs",
            "Expected (interface_name)",
        );
    };
    let snap = snapshot_from_mpv_state(&mpv.status());
    let map = match iface {
        "org.mpris.MediaPlayer2" => root_properties_map(),
        "org.mpris.MediaPlayer2.Player" => player_properties_map(&snap),
        _ => {
            return dbus_err(
                msg,
                "org.freedesktop.DBus.Error.UnknownInterface",
                &format!("Unknown interface {iface}"),
            );
        }
    };
    msg.method_return().append1(map)
}

fn handle_props_set(msg: &Message, mpv: &MpvPlayer) -> Message {
    let mut iter = msg.iter_init();
    let Ok(iface): Result<&str, _> = iter.read() else {
        return dbus_err(msg, "org.freedesktop.DBus.Error.InvalidArgs", "Missing interface");
    };
    let Ok(prop): Result<&str, _> = iter.read() else {
        return dbus_err(msg, "org.freedesktop.DBus.Error.InvalidArgs", "Missing property");
    };
    let Ok(val): Result<arg::Variant<Box<dyn RefArg>>, _> = iter.read() else {
        return dbus_err(msg, "org.freedesktop.DBus.Error.InvalidArgs", "Missing variant value");
    };
    if iface != "org.mpris.MediaPlayer2.Player" {
        return dbus_err(
            msg,
            "org.freedesktop.DBus.Error.PropertyReadOnly",
            &format!("Property {iface}.{prop} is read-only"),
        );
    }
    match prop {
        "Volume" => {
            if let Some(v) = val.0.as_f64() {
                let pct = (v.clamp(0.0, 1.0) * 100.0).round() as i64;
                if let Err(err) = mpv.set_volume(pct) {
                    return dbus_err(msg, "org.mpris.MediaPlayer2.Error", &err.message);
                }
            }
            msg.method_return()
        }
        "Rate" => {
            if let Some(r) = val.0.as_f64() {
                if let Err(err) = mpv.set_speed(r) {
                    return dbus_err(msg, "org.mpris.MediaPlayer2.Error", &err.message);
                }
            }
            msg.method_return()
        }
        "LoopStatus" | "Shuffle" => msg.method_return(),
        _ => dbus_err(
            msg,
            "org.freedesktop.DBus.Error.PropertyReadOnly",
            &format!("Property {prop} is read-only"),
        ),
    }
}

fn prop_box<T: RefArg + 'static>(val: T) -> Variant<Box<dyn RefArg>> {
    Variant(Box::new(val))
}

fn root_properties_map() -> PropMap {
    let mut map: PropMap = HashMap::new();
    map.insert("CanQuit".to_string(), prop_box(false));
    map.insert("CanRaise".to_string(), prop_box(true));
    map.insert("HasTrackList".to_string(), prop_box(false));
    map.insert("Identity".to_string(), prop_box("OmniDesk".to_string()));
    map.insert("DesktopEntry".to_string(), prop_box("omnidesk".to_string()));
    map.insert(
        "SupportedUriSchemes".to_string(),
        prop_box(vec!["http".to_string(), "https".to_string(), "file".to_string()]),
    );
    map.insert(
        "SupportedMimeTypes".to_string(),
        prop_box(vec![
            "audio/flac".to_string(),
            "audio/mpeg".to_string(),
            "audio/mp4".to_string(),
            "audio/ogg".to_string(),
            "video/mp4".to_string(),
            "video/x-matroska".to_string(),
            "video/webm".to_string(),
        ]),
    );
    map
}

fn player_properties_map(snap: &MprisSnapshot) -> PropMap {
    let mut map: PropMap = HashMap::new();
    map.insert("PlaybackStatus".to_string(), prop_box(snap.playback_status.clone()));
    map.insert("LoopStatus".to_string(), prop_box(snap.loop_status.clone()));
    map.insert("Rate".to_string(), prop_box(snap.rate));
    map.insert("Shuffle".to_string(), prop_box(snap.shuffle));
    map.insert("Volume".to_string(), prop_box(snap.volume));
    map.insert("Position".to_string(), prop_box(snap.position_us));
    map.insert("MinimumRate".to_string(), prop_box(0.25_f64));
    map.insert("MaximumRate".to_string(), prop_box(4.0_f64));
    map.insert("CanGoNext".to_string(), prop_box(snap.can_go_next));
    map.insert("CanGoPrevious".to_string(), prop_box(snap.can_go_previous));
    map.insert("CanPlay".to_string(), prop_box(snap.can_play));
    map.insert("CanPause".to_string(), prop_box(snap.can_pause));
    map.insert("CanSeek".to_string(), prop_box(snap.can_seek));
    map.insert("CanControl".to_string(), prop_box(true));

    let mut meta: PropMap = HashMap::new();
    meta.insert(
        "mpris:trackid".to_string(),
        prop_box(DbusPath::from(snap.track_object_path.clone())),
    );
    if snap.length_us > 0 {
        meta.insert("mpris:length".to_string(), prop_box(snap.length_us));
    }
    if !snap.title.is_empty() {
        meta.insert("xesam:title".to_string(), prop_box(snap.title.clone()));
    }
    if !snap.artists.is_empty() {
        meta.insert("xesam:artist".to_string(), prop_box(snap.artists.clone()));
    }
    if !snap.album.is_empty() {
        meta.insert("xesam:album".to_string(), prop_box(snap.album.clone()));
    }
    if !snap.art_url.is_empty() {
        meta.insert("mpris:artUrl".to_string(), prop_box(snap.art_url.clone()));
    }
    map.insert("Metadata".to_string(), prop_box(meta));
    map
}

fn dbus_err(msg: &Message, name: &str, text: &str) -> Message {
    let err_name = ErrorName::from(name);
    msg.error(&err_name, &std::ffi::CString::new(text.replace('\0', "")).unwrap_or_default())
}

fn send_reply(conn: &Connection, reply: Message) {
    let _ = conn.channel().send(reply);
}
