//! Safe read-only plugin manifest scanner for offline `list_plugins` fallback.

use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::Path;

use serde_json::{json, Map, Value};

use crate::paths::OmnideskPaths;
use crate::protocol::{validate_capability_id, validate_plugin_id};
use crate::redact::{is_secret_key, redact_text};

const MAX_MANIFEST_BYTES: u64 = 1024 * 1024;
const VALID_KINDS: &[&str] = &["provider", "workspace", "hybrid", "extension"];
const VALID_DOMAINS: &[&str] = &["core", "media", "text", "book"];
const VALID_FIELD_TYPES: &[&str] = &["string", "secret", "bool", "int", "select"];

/// Scan configured plugin directories for `plugin.json` without executing Python or shell commands.
pub fn scan_plugin_manifests(paths: &OmnideskPaths) -> Vec<Value> {
    scan_plugin_dirs(&paths.plugin_search_dirs())
}

/// Scan explicit plugin root directories, returning sorted `PluginRecord`-compatible JSON objects.
pub fn scan_plugin_dirs(roots: &[std::path::PathBuf]) -> Vec<Value> {
    let mut records: BTreeMap<String, Value> = BTreeMap::new();

    for root in roots {
        let Ok(root_meta) = fs::symlink_metadata(root) else {
            continue;
        };
        if root_meta.file_type().is_symlink() || !root_meta.is_dir() {
            continue;
        }
        let Ok(entries) = fs::read_dir(root) else {
            continue;
        };
        let mut child_paths: Vec<_> = entries.filter_map(Result::ok).map(|e| e.path()).collect();
        child_paths.sort();

        for plugin_dir in child_paths {
            let Some(dir_name) = plugin_dir.file_name().and_then(|n| n.to_str()) else {
                continue;
            };
            if dir_name.starts_with('.') {
                continue;
            }
            let Ok(dir_meta) = fs::symlink_metadata(&plugin_dir) else {
                continue;
            };
            if dir_meta.file_type().is_symlink() || !dir_meta.is_dir() {
                continue;
            }
            let manifest_path = plugin_dir.join("plugin.json");
            let Ok(manifest_meta) = fs::symlink_metadata(&manifest_path) else {
                continue;
            };
            let safe_dir_id = safe_fallback_id(None, dir_name);
            if manifest_meta.file_type().is_symlink() {
                records
                    .entry(safe_dir_id.to_string())
                    .or_insert_with(|| invalid_record(safe_dir_id, &plugin_dir, "Refusing symlinked plugin.json"));
                continue;
            }
            if !manifest_meta.is_file() {
                continue;
            }
            if manifest_meta.len() > MAX_MANIFEST_BYTES {
                records
                    .entry(safe_dir_id.to_string())
                    .or_insert_with(|| invalid_record(safe_dir_id, &plugin_dir, "plugin.json exceeds 1 MiB size limit"));
                continue;
            }

            let record = parse_manifest_record(dir_name, &plugin_dir, &manifest_path);
            let pid = record
                .get("plugin_id")
                .and_then(Value::as_str)
                .unwrap_or(safe_dir_id)
                .to_string();
            records.entry(pid).or_insert(record);
        }
    }

    records.into_values().collect()
}

fn safe_fallback_id<'a>(raw_id: Option<&'a str>, dir_name: &'a str) -> &'a str {
    if let Some(id) = raw_id {
        if validate_plugin_id(id).is_ok() {
            return id;
        }
    }
    if validate_plugin_id(dir_name).is_ok() {
        return dir_name;
    }
    "invalid-plugin"
}

fn is_valid_semver(raw: &str) -> bool {
    let (core_and_pre, build_opt) = match raw.split_once('+') {
        Some((head, build)) => {
            if build.is_empty()
                || !build
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-')
            {
                return false;
            }
            (head, Some(build))
        }
        None => (raw, None),
    };
    let _ = build_opt;
    let core = match core_and_pre.split_once('-') {
        Some((c, pre)) => {
            if pre.is_empty()
                || !pre
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-')
            {
                return false;
            }
            c
        }
        None => core_and_pre,
    };
    let mut nums = core.split('.');
    match (nums.next(), nums.next(), nums.next(), nums.next()) {
        (Some(a), Some(b), Some(c), None) => [a, b, c]
            .iter()
            .all(|seg| !seg.is_empty() && seg.bytes().all(|b| b.is_ascii_digit())),
        _ => false,
    }
}

fn is_valid_entry(raw: &str) -> bool {
    let Some((module, callable)) = raw.split_once(':') else {
        return false;
    };
    if module.is_empty() || callable.is_empty() || callable.contains(':') {
        return false;
    }
    let mb = module.as_bytes();
    let cb = callable.as_bytes();
    (mb[0].is_ascii_alphabetic() || mb[0] == b'_')
        && mb.iter().all(|b| b.is_ascii_alphanumeric() || *b == b'_' || *b == b'.')
        && (cb[0].is_ascii_alphabetic() || cb[0] == b'_')
        && cb.iter().all(|b| b.is_ascii_alphanumeric() || *b == b'_')
}

fn validate_capability_array(obj: &Map<String, Value>, field: &str) -> Result<Value, String> {
    let Some(val) = obj.get(field) else {
        return Ok(json!([]));
    };
    let Some(items) = val.as_array() else {
        return Err(format!("Manifest '{field}' must be an array"));
    };
    for item in items {
        let Some(cap) = item.as_str() else {
            return Err(format!("Manifest '{field}' items must be strings"));
        };
        if validate_capability_id(cap).is_err() {
            return Err(format!("Invalid capability ID '{cap}' in '{field}'"));
        }
    }
    Ok(Value::Array(items.clone()))
}

fn validate_permissions_array(obj: &Map<String, Value>) -> Result<Value, String> {
    let Some(val) = obj.get("permissions") else {
        return Ok(json!([]));
    };
    let Some(items) = val.as_array() else {
        return Err("Manifest 'permissions' must be an array".to_string());
    };
    for item in items {
        if item.as_str().map(str::trim).is_none_or(str::is_empty) {
            return Err("Manifest 'permissions' items must be non-empty strings".to_string());
        }
    }
    Ok(Value::Array(items.clone()))
}

fn parse_manifest_record(dir_name: &str, plugin_dir: &Path, manifest_path: &Path) -> Value {
    let safe_dir_id = safe_fallback_id(None, dir_name);
    let raw = match fs::read_to_string(manifest_path) {
        Ok(text) => text,
        Err(err) => return invalid_record(safe_dir_id, plugin_dir, &format!("Failed to read plugin.json: {err}")),
    };
    let parsed: Value = match serde_json::from_str(&raw) {
        Ok(val) => val,
        Err(err) => return invalid_record(safe_dir_id, plugin_dir, &format!("Invalid JSON in plugin.json: {err}")),
    };
    let Some(obj) = parsed.as_object() else {
        return invalid_record(safe_dir_id, plugin_dir, "plugin.json must be a JSON object");
    };

    let raw_id_opt = obj.get("id").and_then(Value::as_str);
    let safe_id = safe_fallback_id(raw_id_opt, dir_name);
    if obj.get("schema_version").and_then(Value::as_u64) != Some(2) {
        return invalid_record(safe_id, plugin_dir, "Unsupported plugin.json schema_version; expected 2");
    }
    let Some(raw_id) = raw_id_opt else {
        return invalid_record(safe_id, plugin_dir, "Missing required manifest field 'id'");
    };
    let plugin_id = match validate_plugin_id(raw_id) {
        Ok(valid) => valid,
        Err(err) => return invalid_record(safe_id, plugin_dir, &err.message),
    };

    let name = obj
        .get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("");
    let version = obj.get("version").and_then(Value::as_str).unwrap_or("");
    let kind = obj.get("kind").and_then(Value::as_str).unwrap_or("");
    let domain = obj.get("domain").and_then(Value::as_str).unwrap_or("");

    if name.is_empty() || !is_valid_semver(version) || !VALID_KINDS.contains(&kind) || !VALID_DOMAINS.contains(&domain) {
        return invalid_record(plugin_id, plugin_dir, "Invalid required manifest fields (name/version/kind/domain)");
    }
    if let Some(rt_val) = obj.get("runtime") {
        if rt_val.as_str() != Some("python") {
            return invalid_record(plugin_id, plugin_dir, "Invalid manifest 'runtime'; expected 'python'");
        }
    }
    if let Some(entry_val) = obj.get("entry") {
        if !entry_val.as_str().is_some_and(is_valid_entry) {
            return invalid_record(plugin_id, plugin_dir, "Invalid manifest 'entry'; expected '<module>:<callable>'");
        }
    }
    let provides = match validate_capability_array(obj, "provides") {
        Ok(v) => v,
        Err(reason) => return invalid_record(plugin_id, plugin_dir, &reason),
    };
    let consumes = match validate_capability_array(obj, "consumes") {
        Ok(v) => v,
        Err(reason) => return invalid_record(plugin_id, plugin_dir, &reason),
    };
    let permissions = match validate_permissions_array(obj) {
        Ok(v) => v,
        Err(reason) => return invalid_record(plugin_id, plugin_dir, &reason),
    };
    let sanitized_schema = match validate_and_sanitize_config_schema(obj.get("config_schema")) {
        Ok(v) => v,
        Err(reason) => return invalid_record(plugin_id, plugin_dir, &reason),
    };

    let resolved_dir = plugin_dir
        .canonicalize()
        .unwrap_or_else(|_| plugin_dir.to_path_buf());
    let description = redact_text(obj.get("description").and_then(Value::as_str).unwrap_or(""));

    json!({
        "plugin_id": plugin_id,
        "plugin_dir": resolved_dir.display().to_string(),
        "state": "discovered",
        "enabled": true,
        "error": "",
        "loaded_at": Value::Null,
        "reload_count": 0,
        "name": name,
        "version": version,
        "kind": kind,
        "domain": domain,
        "description": description,
        "author": obj.get("author").and_then(Value::as_str).unwrap_or(""),
        "provides": provides,
        "consumes": consumes,
        "permissions": permissions,
        "config_schema": sanitized_schema,
    })
}

fn validate_and_sanitize_config_schema(raw: Option<&Value>) -> Result<Value, String> {
    let Some(val) = raw else {
        return Ok(json!([]));
    };
    let Some(items) = val.as_array() else {
        return Err("Manifest 'config_schema' must be an array".to_string());
    };
    let mut seen_keys = HashSet::new();
    let mut cleaned = Vec::with_capacity(items.len());
    for entry in items {
        let Some(obj) = entry.as_object() else {
            return Err("config_schema entries must be JSON objects".to_string());
        };
        let key = obj
            .get("key")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| "config_schema entry requires non-empty string 'key'".to_string())?;
        if !seen_keys.insert(key.to_string()) {
            return Err(format!("Duplicate config_schema key '{key}'"));
        }
        if obj.get("label").and_then(Value::as_str).map(str::trim).is_none_or(str::is_empty) {
            return Err(format!("config_schema '{key}' requires non-empty string 'label'"));
        }
        let field_type = obj
            .get("type")
            .and_then(Value::as_str)
            .filter(|t| VALID_FIELD_TYPES.contains(t))
            .ok_or_else(|| format!("config_schema '{key}' has invalid 'type'"))?;
        if field_type == "select" {
            let valid_opts = obj
                .get("options")
                .and_then(Value::as_array)
                .is_some_and(|opts| {
                    !opts.is_empty()
                        && opts.iter().all(|o| o.as_str().map(str::trim).is_some_and(|s| !s.is_empty()))
                });
            if !valid_opts {
                return Err(format!("config_schema '{key}' with type='select' requires non-empty 'options'"));
            }
        }
        let mut copy = obj.clone();
        if let Some(desc) = copy.get("description").and_then(Value::as_str) {
            copy.insert("description".to_string(), Value::String(redact_text(desc)));
        }
        let sensitive = field_type == "secret" || is_secret_key(key);
        if let Some(def) = copy.get("default") {
            if sensitive {
                if !def.is_null() && def.as_str() != Some("") {
                    copy.insert("default".to_string(), Value::String(String::new()));
                }
            } else if let Some(s) = def.as_str() {
                copy.insert("default".to_string(), Value::String(redact_text(s)));
            }
        }
        cleaned.push(Value::Object(copy));
    }
    Ok(Value::Array(cleaned))
}

fn invalid_record(plugin_id: &str, plugin_dir: &Path, reason: &str) -> Value {
    json!({
        "plugin_id": plugin_id,
        "plugin_dir": plugin_dir.display().to_string(),
        "state": "invalid",
        "enabled": true,
        "error": redact_text(reason),
        "loaded_at": Value::Null,
        "reload_count": 0,
        "name": plugin_id,
        "version": "",
        "kind": "",
        "domain": "",
        "provides": [],
        "consumes": [],
    })
}
