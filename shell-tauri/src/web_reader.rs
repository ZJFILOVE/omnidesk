//! An unprivileged native reader: remote content never receives the host IPC bridge.
use tauri::{AppHandle, Manager, Webview};
#[cfg(not(target_os = "linux"))]
use tauri::{LogicalPosition, LogicalSize, WebviewBuilder, WebviewUrl};

#[derive(Clone, Copy)]
enum ReaderSite { Linuxdo, Reddit }

impl ReaderSite {
    fn parse(site: &str) -> Result<Self, String> {
        match site { "linuxdo" => Ok(Self::Linuxdo), "reddit" => Ok(Self::Reddit), _ => Err("Unknown reader site".into()) }
    }
    fn label(self) -> &'static str { match self { Self::Linuxdo => "linuxdo-reader", Self::Reddit => "reddit-reader" } }
    fn home(self) -> &'static str { match self { Self::Linuxdo => "https://linux.do/", Self::Reddit => "https://www.reddit.com/" } }
    fn allows(self, url: &tauri::Url) -> bool {
        url.scheme() == "https" && url.username().is_empty() && url.password().is_none() && url.port_or_known_default() == Some(443)
            && url.host_str().is_some_and(|host| match self {
                Self::Linuxdo => host == "linux.do" || host.ends_with(".linux.do"),
                Self::Reddit => host == "reddit.com" || host.ends_with(".reddit.com") || host == "redd.it",
            })
    }
}

fn allowed_navigation(site: ReaderSite, url: &tauri::Url) -> bool {
    matches!(url.as_str(), "about:blank" | "about:srcdoc") || site.allows(url)
        || (url.scheme() == "https" && url.host_str() == Some("challenges.cloudflare.com"))
}

#[derive(serde::Deserialize)]
pub struct WebTranslationEntry {
    id: String,
    translation: String,
}

const EXTRACT_SEGMENTS_JS: &str = r#"(() => {
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','SVG','CODE','PRE','NAV','HEADER','FOOTER']);
  const els = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,blockquote,[slot="text-body"]'));
  const picked = [];
  const seen = new Set();
  for (const el of els) {
    if (el.closest('[data-omnidesk-tr-injected]') || skip.has(el.tagName)) continue;
    if (els.some(other => other !== el && other.contains(el) && other.matches('p,li,blockquote'))) continue;
    const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (text.length < 2 || seen.has(text)) continue;
    seen.add(text);
    const id = 'w' + picked.length;
    el.setAttribute('data-omnidesk-tr-id', id);
    picked.push({ id, text });
    if (picked.length >= 120) break;
  }
  return JSON.stringify({ title: document.title || '', segments: picked });
})()"#;

fn apply_translations_js(entries: &[WebTranslationEntry]) -> String {
    let json = serde_json::to_string(
        &entries.iter().map(|e| (&e.id, &e.translation)).collect::<Vec<_>>(),
    )
    .unwrap_or_else(|_| "[]".to_string());
    format!(
        r#"(() => {{
  const items = {json};
  for (const [id, tr] of items) {{
    const el = document.querySelector('[data-omnidesk-tr-id="' + CSS.escape(id) + '"]');
    if (!el || !tr) continue;
    let node = el.querySelector(':scope > [data-omnidesk-tr-injected]');
    if (!node) {{
      node = document.createElement('div');
      node.setAttribute('data-omnidesk-tr-injected', '1');
      node.style.cssText = 'margin-top:4px;padding:4px 8px;border-left:2px solid #8b949e;opacity:0.9;font-size:0.95em;line-height:1.5;';
      el.appendChild(node);
    }}
    node.textContent = tr;
  }}
}})()"#
    )
}

#[cfg(target_os = "linux")]
async fn linux_reader(main: &Webview, profile: std::path::PathBuf, site: ReaderSite, action: String, target: Option<tauri::Url>, bounds: [f64; 4], translations: Option<Vec<WebTranslationEntry>>) -> Result<serde_json::Value, String> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    let host = main.clone();
    main.with_webview(move |native| {
        use gtk::prelude::*;
        use javascriptcore::ValueExt;
        use webkit2gtk::{CookieManagerExt, NavigationPolicyDecisionExt, PolicyDecisionExt, URIRequestExt, WebContextExt, WebViewExt};
        let result = (|| -> Result<Option<webkit2gtk::WebView>, String> {
            let main_widget = native.inner();
            let parent = main_widget.parent().ok_or("Main view has no GTK parent")?;
            let overlay = if let Ok(overlay) = parent.clone().downcast::<gtk::Overlay>() {
                overlay
            } else {
                if action != "show" { return Ok(None); }
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
            let existing = overlay.children().into_iter().find(|w| w.widget_name() == site.label())
                .and_then(|w| w.downcast::<webkit2gtk::WebView>().ok());
            if action != "show" {
                if let Some(reader) = existing {
                    match action.as_str() {
                        "hide" => { if reader.is_visible() { reader.hide(); main_widget.grab_focus(); } },
                        "reload" => reader.reload(),
                        "back" => reader.go_back(),
                        "extract_segments" => return Ok(Some(reader)),
                        "apply_translations" => {
                            if let Some(entries) = translations.as_deref() {
                                let script = apply_translations_js(entries);
                                reader.evaluate_javascript(&script, None, None, None::<&webkit2gtk::gio::Cancellable>, |_| {});
                            }
                        },
                        _ => return Err("Unknown reader action".into()),
                    }
                }
                return Ok(None);
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
                reader.set_widget_name(site.label());
                reader.connect_decide_policy(move |reader, decision, kind| {
                    if kind == webkit2gtk::PolicyDecisionType::NavigationAction || kind == webkit2gtk::PolicyDecisionType::NewWindowAction {
                        if let Some(nav) = decision.downcast_ref::<webkit2gtk::NavigationPolicyDecision>() {
                            #[allow(deprecated)]
                            let target = nav.request().and_then(|req| req.uri()).and_then(|uri| tauri::Url::parse(&uri).ok());
                            if !target.as_ref().is_some_and(|url| allowed_navigation(site, url)) { decision.ignore(); return true; }
                            if kind == webkit2gtk::PolicyDecisionType::NewWindowAction {
                                decision.ignore();
                                if let Some(url) = target { reader.load_uri(url.as_str()); }
                                return true;
                            }
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
                    let is_translate_shortcut = shift && alt && !ctrl && matches!(key.as_str(), "t" | "T");
                    let host_shortcut = is_translate_shortcut
                        || (!shift && !alt && ctrl && matches!(key.as_str(), "1"|"2"|"3"|"4"|"5"|"k"))
                        || (!shift && !ctrl && alt && key.len() == 1 && key.chars().all(|c| c.is_ascii_alphanumeric()))
                        || (!shift && !alt && !ctrl && key == "F8");
                    if !host_shortcut { return gtk::glib::Propagation::Proceed; }
                    let options = if is_translate_shortcut {
                        serde_json::json!({"key":key,"code":"KeyT","ctrlKey":ctrl,"altKey":alt,"shiftKey":shift,"bubbles":true})
                    } else {
                        serde_json::json!({"key":key,"ctrlKey":ctrl,"altKey":alt,"shiftKey":shift,"bubbles":true})
                    };
                    let _ = host.set_focus();
                    let _ = host.eval(format!("window.dispatchEvent(new KeyboardEvent('keydown', {options}))"));
                    gtk::glib::Propagation::Stop
                });
                overlay.add_overlay(&reader);
                if target.is_none() { reader.load_uri(site.home()); }
                reader
            };
            if let Some(url) = target { reader.load_uri(url.as_str()); }
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
            Ok(None)
        })();
        match result {
            Ok(Some(reader)) => {
                let mut tx = Some(tx);
                reader.evaluate_javascript(EXTRACT_SEGMENTS_JS, None, None, None::<&webkit2gtk::gio::Cancellable>, move |eval_res| {
                    let payload = match eval_res {
                        Ok(val) => serde_json::from_str::<serde_json::Value>(&val.to_str())
                            .map_err(|e| e.to_string()),
                        Err(e) => Err(e.to_string()),
                    };
                    if let Some(sender) = tx.take() {
                        let _ = sender.send(payload);
                    }
                });
            }
            Ok(None) => { let _ = tx.send(Ok(serde_json::Value::Null)); }
            Err(e) => { let _ = tx.send(Err(e)); }
        }
    }).map_err(|e| e.to_string())?;
    rx.await.map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn web_reader(
    app: AppHandle,
    webview: Webview,
    action: String,
    site: Option<String>,
    url: Option<String>,
    x: Option<f64>,
    y: Option<f64>,
    width: Option<f64>,
    height: Option<f64>,
    translations: Option<Vec<WebTranslationEntry>>,
) -> Result<serde_json::Value, String> {
    if webview.label() != "main" { return Err("Only the main app can control the reader".into()); }
    if !matches!(action.as_str(), "show" | "hide" | "back" | "reload" | "extract_segments" | "apply_translations") {
        return Err("Unknown reader action".into());
    }
    let site = ReaderSite::parse(site.as_deref().unwrap_or("linuxdo"))?;
    let target = url.map(|value| tauri::Url::parse(&value).map_err(|e| e.to_string())).transpose()?;
    if target.as_ref().is_some_and(|url| !site.allows(url)) { return Err("URL is outside the selected community".into()); }
    let bounds = [x.unwrap_or(0.0), y.unwrap_or(0.0), width.unwrap_or(0.0), height.unwrap_or(0.0)];
    if action == "show" && (bounds.iter().any(|v| !v.is_finite() || *v < 0.0) || bounds[2] < 1.0 || bounds[3] < 1.0) {
        return Err("Invalid reader bounds".into());
    }
    let profile = app.path().app_data_dir().map_err(|e| e.to_string())?.join(site.label());
    #[cfg(target_os = "linux")]
    return linux_reader(&webview, profile, site, action, target, bounds, translations).await;
    #[cfg(not(target_os = "linux"))]
    {
        if let Some(reader) = app.get_webview(site.label()) {
            match action.as_str() {
                "hide" => reader.hide(),
                "reload" => reader.reload(),
                "back" => reader.eval("history.back()"),
                "extract_segments" => return Ok(serde_json::json!({ "title": "", "segments": [] })),
                "apply_translations" => {
                    if let Some(entries) = translations.as_deref() {
                        reader.eval(&apply_translations_js(entries)).map_err(|e| e.to_string())?;
                    }
                    return Ok(serde_json::Value::Null);
                }
                _ => {
                    if let Some(url) = target { reader.navigate(url).map_err(|e| e.to_string())?; }
                    reader.set_position(LogicalPosition::new(bounds[0], bounds[1])).map_err(|e| e.to_string())?;
                    reader.set_size(LogicalSize::new(bounds[2], bounds[3])).map_err(|e| e.to_string())?;
                    reader.show()
                }
            }.map_err(|e| e.to_string())?;
            return Ok(serde_json::Value::Null);
        }
        if action != "show" { return Ok(serde_json::Value::Null); }
        let window = app.get_window("main").ok_or("Main window unavailable")?;
        let builder = WebviewBuilder::new(site.label(), WebviewUrl::External(target.unwrap_or_else(|| site.home().parse().unwrap())))
            .data_directory(profile).on_navigation(move |url| allowed_navigation(site, url));
        window.add_child(builder, LogicalPosition::new(bounds[0], bounds[1]), LogicalSize::new(bounds[2], bounds[3])).map_err(|e| e.to_string())?;
        Ok(serde_json::Value::Null)
    }
}

#[tauri::command]
pub async fn player_fullscreen(webview: Webview) -> Result<(), String> {
    if webview.label() != "main" { return Err("Only the main app can control fullscreen".into()); }
    let window = webview.window();
    let fullscreen = window.is_fullscreen().map_err(|e| e.to_string())?;
    window.set_fullscreen(!fullscreen).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn community_readers_only_accept_their_own_https_urls() {
        for (site, own, other) in [
            (ReaderSite::Reddit, "https://www.reddit.com/r/bicycling/comments/abc/title/", "https://linux.do/t/123"),
            (ReaderSite::Linuxdo, "https://linux.do/t/topic/123/4", "https://www.reddit.com/"),
        ] {
            assert!(site.allows(&tauri::Url::parse(own).unwrap()));
            for rejected in [other, "https://reddit.com.evil.test/", "http://reddit.com/", "https://name@reddit.com/", "https://reddit.com:8443/", "file:///tmp/page"] {
                assert!(!site.allows(&tauri::Url::parse(rejected).unwrap()));
            }
        }
        assert!(ReaderSite::Reddit.allows(&tauri::Url::parse("https://redd.it/abc").unwrap()));
        assert!(ReaderSite::parse("unknown").is_err());
    }
}
