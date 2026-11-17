//! Secret and credential redaction helpers for logs, diagnostics, and RPC errors.

use serde_json::{Map, Value};

const SECRET_EXACT_KEYS: &[&str] = &[
    "secret", "secrets", "token", "cookie", "cookies", "cookie_header", "set_cookie",
    "password", "passwd", "passphrase", "authorization", "proxy_authorization", "auth_header",
    "credential", "credentials", "gateway_key", "api_key", "private_key", "secret_key",
    "client_secret", "session_secret", "access_token", "refresh_token", "auth_token",
    "bearer_token", "api_token", "api_secret", "x_api_key", "x_auth_token", "csrf_token",
    "xsrf_token", "jwt",
];

const SECRET_SUFFIXES: &[&str] = &[
    "_secret", "_secrets", "_token", "_cookie", "_cookies", "_cookie_header",
    "_password", "_passwd", "_passphrase", "_api_key", "_api_secret", "_api_token",
    "_gateway_key", "_private_key", "_secret_key", "_auth_header", "_authorization",
];

const HEADER_LINE_KEYS: &[&str] = &[
    "authorization", "proxy_authorization", "cookie_header", "set_cookie", "cookie",
    "x_api_key", "x_auth_token", "auth_header",
];

const KNOWN_PREFIXES: &[&str] = &[
    "sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "glpat-", "xoxb-", "xoxp-", "xoxa-", "xoxr-", "xoxs-",
];

fn normalize_secret_key(key: &str) -> String {
    let trimmed = key.trim();
    let mut out = String::with_capacity(trimmed.len() + 4);
    let mut chars = trimmed.chars().peekable();
    let mut prev: Option<char> = None;
    while let Some(ch) = chars.next() {
        if ch == '-' {
            out.push('_');
            prev = Some('_');
            continue;
        }
        if ch.is_ascii_uppercase() {
            if let Some(p) = prev {
                let next_lower = chars.peek().is_some_and(char::is_ascii_lowercase);
                if p.is_ascii_lowercase()
                    || p.is_ascii_digit()
                    || (p.is_ascii_uppercase() && next_lower)
                {
                    out.push('_');
                }
            }
        }
        out.push(ch.to_ascii_lowercase());
        prev = Some(ch);
    }
    out
}

/// Return true if a JSON field or parameter name represents a sensitive credential.
pub fn is_secret_key(key: &str) -> bool {
    let norm = normalize_secret_key(key);
    if norm.is_empty() {
        return false;
    }
    SECRET_EXACT_KEYS.contains(&norm.as_str())
        || SECRET_SUFFIXES.iter().any(|suffix| norm.ends_with(suffix))
}

/// Recursively scrub secret keys and credential patterns from a JSON value for safe logging.
pub fn redact_json_value(value: &Value) -> Value {
    match value {
        Value::String(s) => Value::String(redact_text(s)),
        Value::Array(items) => Value::Array(items.iter().map(redact_json_value).collect()),
        Value::Object(map) => {
            let mut out = Map::with_capacity(map.len());
            for (k, v) in map {
                if is_secret_key(k) && !v.is_null() {
                    out.insert(k.clone(), Value::String("[REDACTED]".to_string()));
                } else {
                    out.insert(k.clone(), redact_json_value(v));
                }
            }
            Value::Object(out)
        }
        _ => value.clone(),
    }
}

/// Scrub URL userinfo passwords, Bearer tokens, Cookie/Authorization headers, key=value secrets, and known prefixes.
pub fn redact_text(input: &str) -> String {
    if input.is_empty() {
        return String::new();
    }
    let step1 = scrub_url_userinfo(input);
    let step2 = scrub_bearer_and_prefixes(&step1);
    scrub_key_value_secrets(&step2)
}

fn is_scheme_char(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.')
}

fn scrub_url_userinfo(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = String::with_capacity(input.len());
    let mut i = 0usize;

    while i < bytes.len() {
        if bytes[i].is_ascii_alphabetic() && (i == 0 || !is_scheme_char(bytes[i - 1])) {
            let mut s_end = i + 1;
            while s_end < bytes.len() && is_scheme_char(bytes[s_end]) {
                s_end += 1;
            }
            if s_end + 3 <= bytes.len() && &bytes[s_end..s_end + 3] == b"://" {
                let mut pos = s_end + 3;
                let mut colon_pos: Option<usize> = None;
                let mut at_pos: Option<usize> = None;
                while pos < bytes.len() {
                    let b = bytes[pos];
                    if b == b'@' {
                        at_pos = Some(pos);
                        break;
                    }
                    if b == b':' && colon_pos.is_none() {
                        colon_pos = Some(pos);
                    }
                    if b.is_ascii_whitespace()
                        || matches!(
                            b,
                            b'/' | b'?' | b'#' | b'"' | b'\'' | b'<' | b'>' | b')' | b']' | b'}' | b',' | b';'
                        )
                    {
                        break;
                    }
                    pos += 1;
                }
                if let (Some(c_pos), Some(a_pos)) = (colon_pos, at_pos) {
                    if a_pos > c_pos + 1 {
                        out.push_str(&input[i..=c_pos]);
                        out.push_str("[REDACTED]@");
                        i = a_pos + 1;
                        continue;
                    }
                }
            }
        }
        let ch = input[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

fn is_word_boundary_before(bytes: &[u8], idx: usize) -> bool {
    if idx == 0 {
        return true;
    }
    let prev = bytes[idx - 1];
    !(prev.is_ascii_alphanumeric() || prev == b'_' || prev == b'-')
}

fn is_token_char(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'~' | b'+' | b'/' | b'=' | b'-')
}

fn scrub_bearer_and_prefixes(input: &str) -> String {
    let bytes = input.as_bytes();
    let lower = input.to_ascii_lowercase();
    let lower_bytes = lower.as_bytes();
    let mut out = String::with_capacity(input.len());
    let mut i = 0usize;

    while i < bytes.len() {
        if is_word_boundary_before(bytes, i) {
            if lower_bytes[i..].starts_with(b"bearer") {
                let after_kw = i + 6;
                let mut ws = after_kw;
                while ws < bytes.len() && (bytes[ws] == b' ' || bytes[ws] == b'\t') {
                    ws += 1;
                }
                if ws > after_kw {
                    let mut end = ws;
                    while end < bytes.len() && is_token_char(bytes[end]) {
                        end += 1;
                    }
                    if end - ws >= 6 && &bytes[ws..end] != b"[REDACTED]" {
                        out.push_str(&input[i..ws]);
                        out.push_str("[REDACTED]");
                        i = end;
                        continue;
                    }
                }
            }

            if let Some(prefix) = KNOWN_PREFIXES
                .iter()
                .find(|p| bytes[i..].starts_with(p.as_bytes()))
            {
                let mut end = i + prefix.len();
                while end < bytes.len() && (bytes[end].is_ascii_alphanumeric() || matches!(bytes[end], b'_' | b'-')) {
                    end += 1;
                }
                if end - (i + prefix.len()) >= 8 {
                    out.push_str("[REDACTED]");
                    i = end;
                    continue;
                }
            }
        }

        let ch = input[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

fn scrub_key_value_secrets(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = String::with_capacity(input.len());
    let mut i = 0usize;

    while i < bytes.len() {
        if is_word_boundary_before(bytes, i) && (bytes[i].is_ascii_alphabetic() || bytes[i] == b'_') {
            let mut key_end = i;
            while key_end < bytes.len()
                && (bytes[key_end].is_ascii_alphanumeric() || matches!(bytes[key_end], b'_' | b'-'))
            {
                key_end += 1;
            }
            let key = &input[i..key_end];
            if is_secret_key(key) {
                if let Some((delim_end, is_colon, quote_end)) = match_kv_separator(bytes, key_end) {
                    if let Some(q_idx) = quote_end {
                        // Quoted value right after separator, e.g. "token": "abc" or token='abc'
                        let quote_ch = bytes[q_idx] as char;
                        let val_start = q_idx + 1;
                        if let Some(val_end) = find_closing_quote(bytes, val_start, bytes[q_idx]) {
                            out.push_str(&input[i..q_idx]);
                            out.push(quote_ch);
                            out.push_str("[REDACTED]");
                            out.push(quote_ch);
                            i = val_end + 1;
                            continue;
                        }
                    } else if !is_url_scheme(bytes, delim_end - 1) {
                        out.push_str(&input[i..delim_end]);
                        let mut val_start = delim_end;
                        while val_start < bytes.len() && (bytes[val_start] == b' ' || bytes[val_start] == b'\t') {
                            out.push(bytes[val_start] as char);
                            val_start += 1;
                        }
                        let norm_key = normalize_secret_key(key);
                        let val_end = if is_colon && HEADER_LINE_KEYS.contains(&norm_key.as_str()) {
                            find_header_value_end(bytes, val_start)
                        } else {
                            find_unquoted_value_end(bytes, val_start)
                        };
                        let red_len = b"[REDACTED]".len();
                        if bytes[val_start..].starts_with(b"[REDACTED]")
                            && bytes.get(val_start + red_len) == Some(&b'@')
                        {
                            out.push_str("[REDACTED]");
                            i = val_start + red_len;
                            continue;
                        }
                        if val_end > val_start {
                            out.push_str("[REDACTED]");
                            i = val_end;
                            continue;
                        }
                    }
                }
            }
        }

        let ch = input[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

fn match_kv_separator(bytes: &[u8], after_key: usize) -> Option<(usize, bool, Option<usize>)> {
    let mut pos = after_key;
    // Handle optional closing quote on JSON keys like `"token": "val"`
    let had_closing_quote = pos < bytes.len() && (bytes[pos] == b'"' || bytes[pos] == b'\'');
    if had_closing_quote {
        pos += 1;
    }
    while pos < bytes.len() && (bytes[pos] == b' ' || bytes[pos] == b'\t') {
        pos += 1;
    }
    if pos >= bytes.len() {
        return None;
    }
    let sep = bytes[pos];
    if sep != b'=' && sep != b':' {
        return None;
    }
    if had_closing_quote && sep != b':' {
        return None;
    }
    let delim_end = pos + 1;
    let mut after_ws = delim_end;
    while after_ws < bytes.len() && (bytes[after_ws] == b' ' || bytes[after_ws] == b'\t') {
        after_ws += 1;
    }
    let quote_idx = if after_ws < bytes.len() && (bytes[after_ws] == b'"' || bytes[after_ws] == b'\'') {
        Some(after_ws)
    } else {
        None
    };
    Some((delim_end, sep == b':', quote_idx))
}

fn is_url_scheme(bytes: &[u8], colon_idx: usize) -> bool {
    colon_idx + 2 < bytes.len() && bytes[colon_idx] == b':' && &bytes[colon_idx + 1..colon_idx + 3] == b"//"
}

fn find_closing_quote(bytes: &[u8], start: usize, quote: u8) -> Option<usize> {
    let mut idx = start;
    while idx < bytes.len() {
        if bytes[idx] == b'\\' {
            idx += 1;
            if idx < bytes.len() {
                // Skip full UTF-8 scalar after backslash.
                let ch_len = std::str::from_utf8(&bytes[idx..])
                    .ok()
                    .and_then(|s| s.chars().next())
                    .map_or(1, char::len_utf8);
                idx += ch_len;
            }
            continue;
        }
        if bytes[idx] == quote {
            return Some(idx);
        }
        if bytes[idx] == b'\n' || bytes[idx] == b'\r' {
            return None;
        }
        idx += 1;
    }
    None
}

fn find_header_value_end(bytes: &[u8], start: usize) -> usize {
    let mut idx = start;
    while idx < bytes.len() && !matches!(bytes[idx], b'\n' | b'\r') {
        idx += 1;
    }
    idx
}

fn find_unquoted_value_end(bytes: &[u8], start: usize) -> usize {
    let mut idx = start;
    while idx < bytes.len()
        && !bytes[idx].is_ascii_whitespace()
        && !matches!(bytes[idx], b',' | b';' | b'&' | b'"' | b'\'' | b')' | b'}' | b']')
    {
        idx += 1;
    }
    idx
}

/// Defense-in-depth scrubbing of sensitive cookie fields in capability responses for WebView.
pub fn scrub_capability_response(cap_id: &str, mut env: Value) -> Value {
    if cap_id == "web.cookies.v1" && env.get("ok").and_then(Value::as_bool) == Some(true) {
        if let Some(data) = env.get_mut("data").and_then(Value::as_object_mut) {
            data.remove("cookie_header");
            data.remove("cookieHeader");
            if let Some(Value::Array(cookies)) = data.get_mut("cookies") {
                for item in cookies {
                    if let Some(obj) = item.as_object_mut() {
                        obj.remove("value");
                        obj.remove("encrypted_value");
                    }
                }
            }
        }
    }
    env
}
