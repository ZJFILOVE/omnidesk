fn main() {
    println!("cargo::rustc-check-cfg=cfg(omnidesk_libmpv_linked)");
    if std::env::var("PROFILE").as_deref() == Ok("release")
        && std::env::var_os("CARGO_FEATURE_CUSTOM_PROTOCOL").is_none()
    {
        panic!(
            "Release builds MUST enable `--features custom-protocol` (or run `scripts/install-v2.sh`) so the app embeds ui/dist instead of connecting to localhost:5173."
        );
    }
    if std::env::var_os("CARGO_FEATURE_LIBMPV").is_some() {
        match pkg_config::Config::new().atleast_version("2.0").probe("mpv") {
            Ok(_) => println!("cargo:rustc-cfg=omnidesk_libmpv_linked"),
            Err(err) => println!(
                "cargo:warning=Feature 'libmpv' enabled but pkg-config 'mpv' was not found ({err}); compiling stub/IPC fallback"
            ),
        }
    }
    tauri_build::build()
}
