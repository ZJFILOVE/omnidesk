use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MpvProbeStatus {
    pub available: bool,
    pub initialized: bool,
    pub backend: &'static str,
    pub hwdec: &'static str,
    pub status: &'static str,
    pub detail: &'static str,
}

pub fn probe() -> MpvProbeStatus {
    MpvProbeStatus {
        available: false,
        initialized: false,
        backend: "libmpv",
        hwdec: "auto-safe",
        status: "skeleton",
        detail: "libmpv OpenGL canvas stub (skeleton)",
    }
}
