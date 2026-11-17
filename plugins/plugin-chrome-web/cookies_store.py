"""Read-only Chromium Cookies SQLite reader and 0600/SecretVault importer."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from omnidesk_sdk import CapabilityResult, PluginContext

if __package__:
    from .cookies_crypto import (
        CookieDecryptor,
        decrypt_cookie_value,
        decryption_scope_summary,
        encryption_scheme,
    )
    from .security import ValidationProblem, validate_http_url, validate_safe_path, validate_secret_key_name
else:  # pragma: no cover - direct module import fallback
    from cookies_crypto import (
        CookieDecryptor,
        decrypt_cookie_value,
        decryption_scope_summary,
        encryption_scheme,
    )
    from security import ValidationProblem, validate_http_url, validate_safe_path, validate_secret_key_name

_CHROMIUM_EPOCH_OFFSET_SEC = 11644473600
_SUPPORTED_ACTIONS = ("list", "get", "get_cookies", "import")


def find_cookies_db(profile_dir: Path) -> Path | None:
    """Locate Chromium `Cookies` SQLite file inside a managed profile without following symlinks."""
    for cand in (profile_dir / "Default" / "Cookies", profile_dir / "Cookies"):
        if cand.is_symlink() or cand.parent.is_symlink():
            raise ValidationProblem("拒绝访问符号链接 Cookie 数据库", error_code="path_safety")
        if cand.is_file():
            return cand
    return None


def _normalize_domain(raw_domain: Any, parsed_host: str) -> str:
    candidate = str(raw_domain or "").strip().lower().lstrip(".")
    if candidate:
        if any(ch.isspace() or ch in {"/", "\\", "\x00"} for ch in candidate):
            raise ValidationProblem("domain 参数格式无效")
        return candidate
    return parsed_host.lower().lstrip(".")


def _domain_matches(host_key: str, *, filter_domain: str, request_host: str, include_subdomains: bool) -> bool:
    hk = host_key.strip().lower()
    if not hk:
        return False
    bare_hk = hk.lstrip(".")
    if not include_subdomains:
        return bare_hk == (request_host or filter_domain)
    if filter_domain and (bare_hk == filter_domain or bare_hk.endswith(f".{filter_domain}")):
        return True
    if request_host and (bare_hk == request_host or (hk.startswith(".") and request_host.endswith(f".{bare_hk}"))):
        return True
    return False


def _path_matches(req_path: str, cookie_path: str) -> bool:
    if not cookie_path or cookie_path == "/" or req_path == cookie_path:
        return True
    return req_path.startswith(cookie_path.rstrip("/") + "/")


def _is_expired(expires_utc: int, has_expires: int, now_unix: float) -> bool:
    if not has_expires or expires_utc <= 0:
        return False
    return ((expires_utc / 1_000_000.0) - _CHROMIUM_EPOCH_OFFSET_SEC) < now_unix


def _copy_sqlite_readonly(db_path: Path, tmp_dir: Path) -> Path:
    dest_db = tmp_dir / "Cookies"
    shutil.copy2(db_path, dest_db)
    os.chmod(dest_db, 0o600)
    for suffix in ("-wal", "-shm", "-journal"):
        sibling = Path(f"{db_path}{suffix}")
        if sibling.is_file() and not sibling.is_symlink():
            dest_sib = tmp_dir / f"Cookies{suffix}"
            shutil.copy2(sibling, dest_sib)
            os.chmod(dest_sib, 0o600)
    return dest_db


def read_cookie_rows(
    db_path: Path,
    *,
    domain: str = "",
    url: str = "",
    include_subdomains: bool = True,
    include_expired: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Read raw cookie rows from a temporary read-only copy of Chromium's `Cookies` SQLite."""
    parsed_host = parsed_scheme = ""
    parsed_path = "/"
    if url:
        parts = urlsplit(validate_http_url(url))
        parsed_host = (parts.hostname or "").lower()
        parsed_scheme = parts.scheme.lower()
        parsed_path = parts.path or "/"
    explicit_domain = _normalize_domain(domain, "")
    now_unix = time.time()

    with tempfile.TemporaryDirectory(prefix="omnidesk-cookies-") as tmp_name:
        copy_path = _copy_sqlite_readonly(db_path, Path(tmp_name))
        conn = sqlite3.connect(f"file:{copy_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(cookies)").fetchall()}
            if not {"host_key", "name"}.issubset(cols):
                raise ValidationProblem("Cookies 数据库缺少必要列", error_code="validation_error")
            db_version = 0
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "meta" in tables:
                meta_row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                if meta_row and str(meta_row[0]).isdigit():
                    db_version = int(meta_row[0])
            select_cols = [
                "host_key",
                "name",
                "value" if "value" in cols else "'' AS value",
                "encrypted_value" if "encrypted_value" in cols else "X'' AS encrypted_value",
                "path" if "path" in cols else "'/' AS path",
                "expires_utc" if "expires_utc" in cols else "0 AS expires_utc",
                "has_expires" if "has_expires" in cols else "0 AS has_expires",
                "is_secure" if "is_secure" in cols else "0 AS is_secure",
                "is_httponly" if "is_httponly" in cols else "0 AS is_httponly",
                "samesite" if "samesite" in cols else "-1 AS samesite",
            ]
            rows = conn.execute(f"SELECT {', '.join(select_cols)} FROM cookies ORDER BY host_key, path, name").fetchall()
        finally:
            conn.close()

    matched: list[dict[str, Any]] = []
    for row in rows:
        host_key = str(row["host_key"] or "")
        if (explicit_domain or parsed_host) and not _domain_matches(
            host_key, filter_domain=explicit_domain, request_host=parsed_host, include_subdomains=include_subdomains
        ):
            continue
        cookie_path = str(row["path"] or "/")
        if url and not _path_matches(parsed_path, cookie_path):
            continue
        is_secure = bool(row["is_secure"])
        if parsed_scheme == "http" and is_secure:
            continue
        expires_utc = int(row["expires_utc"] or 0)
        has_expires = int(row["has_expires"] or 0)
        if not include_expired and _is_expired(expires_utc, has_expires, now_unix):
            continue
        enc_raw = row["encrypted_value"]
        enc_bytes = bytes(enc_raw) if isinstance(enc_raw, (bytes, bytearray, memoryview)) else b""
        matched.append(
            {
                "host_key": host_key,
                "name": str(row["name"] or ""),
                "value": str(row["value"] or ""),
                "encrypted_value": enc_bytes,
                "path": cookie_path,
                "expires_utc": expires_utc,
                "has_expires": bool(has_expires),
                "is_secure": is_secure,
                "is_httponly": bool(row["is_httponly"]),
                "samesite": int(row["samesite"]) if row["samesite"] is not None else -1,
            }
        )
    return matched, db_version


def format_cookie_header(cookies: Sequence[Mapping[str, Any]]) -> str:
    """Format decrypted cookies into an HTTP `Cookie` header string."""
    return "; ".join(f"{item['name']}={item['value']}" for item in cookies if item.get("name") is not None)


def write_cookies_file_0600(target_path: Path, payload: Mapping[str, Any]) -> Path:
    """Atomically write cookie payload to a 0600 file inside a 0700 directory (refusing symlinks)."""
    if target_path.is_symlink() or target_path.parent.is_symlink():
        raise ValidationProblem("拒绝写入符号链接路径", error_code="path_safety")
    target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    tmp_path: Path | None = None
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target_path.parent, prefix=f".{target_path.name}.", suffix=".tmp", delete=False
    )
    try:
        tmp_path = Path(tmp.name)
        os.fchmod(tmp.fileno(), 0o600)
        tmp.write(serialized)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, target_path)
        os.chmod(target_path, 0o600)
    except BaseException:
        if not tmp.closed:
            tmp.close()
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    return target_path


def _resolve_import_file_path(
    ctx: PluginContext, *, req_output_path: Any, cfg_cookie_file_path: str, domain: str
) -> Path:
    raw = str(req_output_path or cfg_cookie_file_path or "").strip()
    if raw:
        candidate = validate_safe_path(raw, check_symlinks=True)
        if not candidate.is_absolute():
            candidate = validate_safe_path(candidate, base_dir=ctx.data_dir, check_symlinks=True)
        return candidate
    safe_dom = re.sub(r"[^A-Za-z0-9._-]+", "_", domain).strip("._") or "cookies"
    return validate_safe_path(f"cookies-{safe_dom}.json", base_dir=ctx.data_dir, check_symlinks=True)


def handle_cookies_action(
    ctx: PluginContext,
    req: Mapping[str, Any],
    *,
    profile_dir: Path,
    default_import_target: str,
    default_cookie_file_path: str,
    decryptor: CookieDecryptor | None = None,
) -> CapabilityResult:
    """Dispatch `web.cookies.v1` actions (`list`, `get`/`get_cookies`, `import`)."""
    action = str(req.get("action") or "").strip()
    if action not in _SUPPORTED_ACTIONS:
        return CapabilityResult.fail(
            f"不支持的 web.cookies.v1 动作: {action!r}",
            data={"supported_actions": list(_SUPPORTED_ACTIONS)},
            error_code="invalid_argument",
        )
    raw_url = str(req.get("url") or "").strip()
    raw_domain = str(req.get("domain") or "").strip()
    include_subdomains = bool(req.get("include_subdomains", True))
    include_expired = bool(req.get("include_expired", False))
    if action in {"get", "get_cookies", "import"} and not (raw_url or raw_domain):
        return CapabilityResult.fail("get / import 动作必须提供 domain 或 url", error_code="invalid_argument")

    try:
        db_path = find_cookies_db(profile_dir)
    except ValidationProblem as exc:
        return CapabilityResult.fail(str(exc), error_code=exc.error_code)
    if db_path is None:
        return CapabilityResult.fail(
            "未找到受管 Chrome Cookies 数据库",
            data={"profile_dir": str(profile_dir), "cookies": [], "count": 0},
            error_code="not_found",
        )
    try:
        rows, db_version = read_cookie_rows(
            db_path, domain=raw_domain, url=raw_url, include_subdomains=include_subdomains, include_expired=include_expired
        )
    except ValidationProblem as exc:
        return CapabilityResult.fail(str(exc), error_code=exc.error_code)
    except sqlite3.OperationalError as exc:
        return CapabilityResult.fail(
            f"读取 Cookie 数据库失败（数据库被占用）: {type(exc).__name__}",
            error_code="cookie_db_busy",
        )
    except sqlite3.DatabaseError as exc:
        return CapabilityResult.fail(f"读取 Cookie 数据库失败: {type(exc).__name__}", error_code="validation_error")
    except OSError as exc:
        return CapabilityResult.fail(
            f"读取 Cookie 数据库失败（文件不可读）: {type(exc).__name__}",
            error_code="cookie_db_unavailable",
        )

    scope = decryption_scope_summary()
    resolved_domain = raw_domain.lower().lstrip(".") or (urlsplit(raw_url).hostname or "").lower()
    if action == "list":
        metadata = [
            {
                "domain": r["host_key"],
                "name": r["name"],
                "path": r["path"],
                "secure": r["is_secure"],
                "httponly": r["is_httponly"],
                "expires_utc": r["expires_utc"],
                "encrypted": bool(r["encrypted_value"]) and not bool(r["value"]),
                "encryption_scheme": (
                    encryption_scheme(r["encrypted_value"]) if r["encrypted_value"] and not r["value"] else "plaintext"
                ),
                "has_value": bool(r["value"]) or bool(r["encrypted_value"]),
            }
            for r in rows
        ]
        return CapabilityResult.ok({"domain": resolved_domain, "count": len(metadata), "cookies": metadata, "decryption_scope": scope})

    if not rows:
        return CapabilityResult.fail(
            f"未找到域 {resolved_domain or '(any)'} 的可用 Cookie",
            data={"domain": resolved_domain, "count": 0, "cookies": []},
            error_code="not_found",
        )

    decrypted_cookies: list[dict[str, Any]] = []
    unsupported_schemes: set[str] = set()
    for row in rows:
        val, fail_scheme = decrypt_cookie_value(
            plain_value=row["value"],
            encrypted_blob=row["encrypted_value"],
            host_key=row["host_key"],
            name=row["name"],
            db_version=db_version,
            decryptor=decryptor,
        )
        if val is None:
            unsupported_schemes.add(fail_scheme or "unknown")
            continue
        decrypted_cookies.append(
            {
                "name": row["name"],
                "value": val,
                "domain": row["host_key"],
                "path": row["path"],
                "secure": row["is_secure"],
                "httponly": row["is_httponly"],
                "expires_utc": row["expires_utc"],
            }
        )
    if unsupported_schemes:
        return CapabilityResult.fail(
            "受管 Chrome Cookie 已加密且当前环境无法解密（禁止伪造或返回密文）",
            data={"domain": resolved_domain, "unsupported_schemes": sorted(unsupported_schemes), "decryption_scope": scope},
            error_code="cookie_decryption_unavailable",
        )

    cookie_header = format_cookie_header(decrypted_cookies)
    if action in {"get", "get_cookies"}:
        return CapabilityResult.ok(
            {"domain": resolved_domain, "count": len(decrypted_cookies), "cookies": decrypted_cookies, "cookie_header": cookie_header, "decryption_scope": scope}
        )

    if "target" not in req and str(req.get("output_path") or "").strip():
        target = "file"
    else:
        target = str(req.get("target") or default_import_target or "vault").strip().lower()
    if target not in {"vault", "file", "both"}:
        return CapabilityResult.fail(f"无效的导入目标: {target!r}（仅支持 vault/file/both）", error_code="invalid_argument")

    try:
        secret_key = validate_secret_key_name(str(req.get("secret_key") or f"cookies:{resolved_domain}").strip())
    except ValidationProblem as exc:
        return CapabilityResult.fail(str(exc), error_code=exc.error_code)

    persist_payload = {"domain": resolved_domain, "cookie_header": cookie_header, "cookies": decrypted_cookies}
    stored_vault = False
    stored_file_path = ""
    if target in {"vault", "both"}:
        stored_val = cookie_header if req.get("format") == "header" else json.dumps(persist_payload, ensure_ascii=False)
        ctx.set_secret(secret_key, stored_val)
        stored_vault = True
    if target in {"file", "both"}:
        try:
            out_path = _resolve_import_file_path(
                ctx, req_output_path=req.get("output_path"), cfg_cookie_file_path=default_cookie_file_path, domain=resolved_domain
            )
            stored_file_path = str(write_cookies_file_0600(out_path, persist_payload))
        except ValidationProblem as exc:
            return CapabilityResult.fail(str(exc), error_code=exc.error_code)

    return CapabilityResult.ok(
        {
            "imported": True,
            "domain": resolved_domain,
            "count": len(decrypted_cookies),
            "cookie_names": [c["name"] for c in decrypted_cookies],
            "target": target,
            "vault_stored": stored_vault,
            "secret_key": secret_key if stored_vault else "",
            "file_path": stored_file_path,
            "file_mode": "0600" if stored_file_path else "",
        }
    )
