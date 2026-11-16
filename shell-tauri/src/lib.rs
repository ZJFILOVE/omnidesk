pub mod mpris;
pub mod mpv;

use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeStatus {
    pub shell: &'static str,
    pub app_id: &'static str,
    pub version: &'static str,
    pub platform: &'static str,
    pub status: &'static str,
    pub mpv: mpv::MpvProbeStatus,
    pub mpris: mpris::MprisStatus,
}

#[tauri::command]
fn runtime_status() -> RuntimeStatus {
    RuntimeStatus {
        shell: "tauri-v2",
        app_id: "omnidesk",
        version: env!("CARGO_PKG_VERSION"),
        platform: std::env::consts::OS,
        status: "skeleton",
        mpv: mpv::probe(),
        mpris: mpris::skeleton_status(),
    }
}

#[tauri::command]
fn mpv_probe() -> mpv::MpvProbeStatus {
    mpv::probe()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![runtime_status, mpv_probe])
        .run(tauri::generate_context!())
        .expect("error while running OmniDesk Tauri shell");
}
