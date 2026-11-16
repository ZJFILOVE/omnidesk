use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MprisStatus {
    pub enabled: bool,
    pub bus_name: &'static str,
    pub status: &'static str,
}

pub fn skeleton_status() -> MprisStatus {
    MprisStatus {
        enabled: false,
        bus_name: "org.mpris.MediaPlayer2.omnidesk",
        status: "skeleton",
    }
}
