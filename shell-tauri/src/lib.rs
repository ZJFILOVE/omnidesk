pub mod commands;
pub mod control_client;
pub mod daemon;
pub mod embedded_mpv;
pub mod events;
pub mod manifest;
pub mod mpris;
pub mod mpv;
pub mod mpv_ipc;
pub mod paths;
pub mod protocol;
pub mod redact;

use std::sync::Arc;

use tauri::Manager;

use crate::commands::ShellState;
use crate::control_client::ControlClient;
use crate::daemon::{DaemonConfig, DaemonManager};
use crate::events::{EventBridge, EventSink};
use crate::mpris::MprisService;
use crate::mpv::MpvPlayer;
use crate::paths::OmnideskPaths;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let paths = OmnideskPaths::from_env();
    let client = ControlClient::new(paths.clone());
    let bridge = EventBridge::new(paths.clone());
    let daemon = DaemonManager::new(paths.clone(), DaemonConfig::default());
    let mpv = MpvPlayer::new(paths);
    let mpris = MprisService::new();

    let bridge_for_setup = Arc::clone(&bridge);
    let daemon_for_setup = Arc::clone(&daemon);
    let mpv_for_setup = Arc::clone(&mpv);
    let mpris_for_setup = Arc::clone(&mpris);

    let app = tauri::Builder::default()
        .manage(ShellState::new(client, bridge, daemon, mpv, mpris))
        .setup(move |app| {
            let sink: Arc<dyn EventSink> = Arc::new(app.handle().clone());
            mpv_for_setup.set_event_sink(Arc::clone(&sink));
            bridge_for_setup.start(Arc::clone(&sink));
            daemon_for_setup.start_in_background(Arc::clone(&bridge_for_setup));
            mpris_for_setup.start(mpv_for_setup, Some(sink));
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == "main" && matches!(event, tauri::WindowEvent::Destroyed) {
                if let Some(state) = window.try_state::<ShellState>() {
                    state.shutdown();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::runtime_status,
            commands::host_paths,
            commands::mpv_probe,
            commands::mpv_embed_probe,
            commands::mpv_embed_enable,
            commands::mpv_embed_disable,
            commands::mpv_embed_set_viewport,
            commands::mpv_embed_set_active,
            commands::mpv_open,
            commands::mpv_pause,
            commands::mpv_resume,
            commands::mpv_toggle_pause,
            commands::mpv_stop,
            commands::mpv_seek,
            commands::mpv_volume,
            commands::mpv_set_speed,
            commands::mpv_status,
            commands::mpris_status,
            commands::daemon_status,
            commands::core_status,
            commands::list_plugins,
            commands::call_capability,
            commands::emit_event,
            commands::read_source,
            commands::call,
            commands::event_bridge_status,
        ])
        .build(tauri::generate_context!())
        .expect("error while building OmniDesk Tauri shell");

    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            if let Some(state) = app_handle.try_state::<ShellState>() {
                state.shutdown();
            }
        }
    });
}
