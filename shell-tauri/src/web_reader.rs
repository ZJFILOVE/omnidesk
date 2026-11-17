//! An unprivileged native reader: remote content never receives the host IPC bridge.
use tauri::{AppHandle, Manager, Webview};
#[cfg(not(target_os = "linux"))]
use tauri::{LogicalPosition, LogicalSize, WebviewBuilder, WebviewUrl};

const LABEL: &str = "linuxdo-reader";

fn allowed_navigation(url: &tauri::Url) -> bool {
    matches!(url.as_str(), "about:blank" | "about:srcdoc") || (url.scheme() == "https"
        && url.host_str().is_some_and(|host| host == "linux.do" || host.ends_with(".linux.do") || host == "challenges.cloudflare.com"))
}

#[cfg(target_os = "linux")]
async fn linux_reader(main: &Webview, profile: std::path::PathBuf, action: String, bounds: [f64; 4]) -> Result<(), String> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    let host = main.clone();
    main.with_webview(move |native| {
        use gtk::prelude::*;
        use webkit2gtk::{CookieManagerExt, NavigationPolicyDecisionExt, PolicyDecisionExt, URIRequestExt, WebContextExt, WebViewExt};
        let result = (|| -> Result<(), String> {
            let main_widget = native.inner();
            let parent = main_widget.parent().ok_or("Main view has no GTK parent")?;
            let overlay = if let Ok(overlay) = parent.clone().downcast::<gtk::Overlay>() {
                overlay
            } else {
                if action != "show" { return Ok(()); }
                // GtkBox packs child webviews vertically. An overlay keeps the
                // main application full size and places the reader in its slot.
                let container = parent.downcast::<gtk::Box>().map_err(|_| "Unexpected main view parent")?;
                container.remove(&main_widget);
                let overlay = gtk::Overlay::new();
                overlay.set_hexpand(true);
                overlay.set_vexpand(true);
                overlay.add(&main_widget);
                container.pack_start(&overlay, true, true, 0);
                overlay.show();
                overlay
            };
            let existing = overlay.children().into_iter().find(|w| w.widget_name() == LABEL)
                .and_then(|w| w.downcast::<webkit2gtk::WebView>().ok());
            if action != "show" {
                if let Some(reader) = existing {
                    match action.as_str() {
                        "hide" => { reader.hide(); main_widget.grab_focus(); },
                        "reload" => reader.reload(),
                        "back" => reader.go_back(),
                        _ => return Err("Unknown reader action".into()),
                    }
                }
                return Ok(());
            }
            let reader = if let Some(reader) = existing { reader } else {
                std::fs::create_dir_all(&profile).map_err(|e| e.to_string())?;
                let storage = webkit2gtk::WebsiteDataManager::builder()
                    .base_data_directory(profile.join("data").to_string_lossy().as_ref())
                    .base_cache_directory(profile.join("cache").to_string_lossy().as_ref()).build();
                let context = webkit2gtk::WebContext::with_website_data_manager(&storage);
                if let Some(cookies) = context.cookie_manager() {
                    cookies.set_persistent_storage(profile.join("cookies.sqlite").to_string_lossy().as_ref(), webkit2gtk::CookiePersistentStorage::Sqlite);
                }
                // No Tauri initialization scripts, handlers or capabilities are
                // installed in this remote view. Only native keys are forwarded.
                let reader = webkit2gtk::WebView::with_context(&context);
                reader.set_widget_name(LABEL);
                reader.connect_decide_policy(|_, decision, kind| {
                    if kind == webkit2gtk::PolicyDecisionType::NavigationAction {
                        if let Some(nav) = decision.downcast_ref::<webkit2gtk::NavigationPolicyDecision>() {
                            #[allow(deprecated)]
                            let allowed = nav.request().and_then(|req| req.uri()).and_then(|uri| tauri::Url::parse(&uri).ok()).is_some_and(|url| allowed_navigation(&url));
                            if !allowed { decision.ignore(); return true; }
                        }
                    }
                    false
                });
                reader.connect_key_press_event(move |_, event| {
                    let flags = event.state();
                    let ctrl = flags.contains(gtk::gdk::ModifierType::CONTROL_MASK);
                    let alt = flags.contains(gtk::gdk::ModifierType::MOD1_MASK);
                    let shift = flags.contains(gtk::gdk::ModifierType::SHIFT_MASK);
                    let key = event.keyval().to_unicode().map(|c| c.to_string())
                        .unwrap_or_else(|| event.keyval().name().map(|s| s.to_string()).unwrap_or_default());
                    let host_shortcut = (!shift && !alt && ctrl && matches!(key.as_str(), "1"|"2"|"3"|"4"|"5"|"k"))
                        || (!shift && !ctrl && alt && key.len() == 1 && key.chars().all(|c| c.is_ascii_alphanumeric()))
                        || (!shift && !alt && !ctrl && key == "F8");
                    if !host_shortcut { return gtk::glib::Propagation::Proceed; }
                    let options = serde_json::json!({"key":key,"ctrlKey":ctrl,"altKey":alt,"shiftKey":shift,"bubbles":true});
                    let _ = host.set_focus();
                    let _ = host.eval(format!("window.dispatchEvent(new KeyboardEvent('keydown', {options}))"));
                    gtk::glib::Propagation::Stop
                });
                overlay.add_overlay(&reader);
                reader.load_uri("https://linux.do/");
                reader
            };
            let [x, y, width, height] = bounds;
            reader.set_halign(gtk::Align::Start);
            reader.set_valign(gtk::Align::Start);
            reader.set_hexpand(false);
            reader.set_vexpand(false);
            reader.set_margin_start(x.round() as i32);
            reader.set_margin_top(y.round() as i32);
            reader.set_size_request(width.round() as i32, height.round() as i32);
            overlay.set_overlay_pass_through(&reader, false);
            reader.show();
            Ok(())
        })();
        let _ = tx.send(result);
    }).map_err(|e| e.to_string())?;
    rx.await.map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn web_reader(app: AppHandle, webview: Webview, action: String, x: Option<f64>, y: Option<f64>, width: Option<f64>, height: Option<f64>) -> Result<(), String> {
    if webview.label() != "main" { return Err("Only the main app can control the reader".into()); }
    if !matches!(action.as_str(), "show" | "hide" | "back" | "reload") { return Err("Unknown reader action".into()); }
    let bounds = [x.unwrap_or(0.0), y.unwrap_or(0.0), width.unwrap_or(0.0), height.unwrap_or(0.0)];
    if action == "show" && (bounds.iter().any(|v| !v.is_finite() || *v < 0.0) || bounds[2] < 1.0 || bounds[3] < 1.0) {
        return Err("Invalid reader bounds".into());
    }
    let profile = app.path().app_data_dir().map_err(|e| e.to_string())?.join(LABEL);
    #[cfg(target_os = "linux")]
    return linux_reader(&webview, profile, action, bounds).await;
    #[cfg(not(target_os = "linux"))]
    {
        if let Some(reader) = app.get_webview(LABEL) {
            return match action.as_str() {
                "hide" => reader.hide(), "reload" => reader.reload(), "back" => reader.eval("history.back()"),
                _ => {
                    reader.set_position(LogicalPosition::new(bounds[0], bounds[1])).map_err(|e| e.to_string())?;
                    reader.set_size(LogicalSize::new(bounds[2], bounds[3])).map_err(|e| e.to_string())?;
                    reader.show()
                }
            }.map_err(|e| e.to_string());
        }
        if action != "show" { return Ok(()); }
        let window = app.get_window("main").ok_or("Main window unavailable")?;
        let builder = WebviewBuilder::new(LABEL, WebviewUrl::External("https://linux.do/".parse().unwrap()))
            .data_directory(profile).on_navigation(allowed_navigation);
        window.add_child(builder, LogicalPosition::new(bounds[0], bounds[1]), LogicalSize::new(bounds[2], bounds[3])).map_err(|e| e.to_string())?;
        Ok(())
    }
}

#[tauri::command]
pub async fn player_fullscreen(webview: Webview) -> Result<(), String> {
    if webview.label() != "main" { return Err("Only the main app can control fullscreen".into()); }
    let window = webview.window();
    let fullscreen = window.is_fullscreen().map_err(|e| e.to_string())?;
    window.set_fullscreen(!fullscreen).map_err(|e| e.to_string())
}
