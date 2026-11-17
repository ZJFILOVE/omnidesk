fn main() {
    println!("cargo::rustc-check-cfg=cfg(omnidesk_libmpv_linked)");
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
