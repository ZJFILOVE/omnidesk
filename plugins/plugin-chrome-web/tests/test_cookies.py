"""Offline tests for plugin-chrome-web `web.cookies.v1`."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omnidesk_sdk import CapabilityResult, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_chrome_web_cookies_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_cookies_db(profile_dir: Path, rows: list[tuple[Any, ...]]) -> Path:
    db_path = profile_dir / "Default" / "Cookies"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('version', '23')")
    conn.execute(
        """
        CREATE TABLE cookies (
            host_key TEXT NOT NULL,
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            encrypted_value BLOB NOT NULL,
            path TEXT NOT NULL,
            expires_utc INTEGER NOT NULL,
            has_expires INTEGER NOT NULL,
            is_secure INTEGER NOT NULL,
            is_httponly INTEGER NOT NULL,
            samesite INTEGER NOT NULL
        )
        """
    )
    conn.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path


def _future_chromium_utc(days: int = 30) -> int:
    return int((time.time() + days * 86400 + 11644473600) * 1_000_000)


def _past_chromium_utc() -> int:
    return int((time.time() - 3600 + 11644473600) * 1_000_000)


@pytest.mark.asyncio
async def test_cookies_readonly_copy_domain_filter_vault_and_0600_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    profile_dir = tmp_path / "xdg-data" / "omnidesk" / "chrome-profile"
    db_path = _create_cookies_db(
        profile_dir,
        [
            (".example.com", "sid", "secret-sid-val", b"", "/", _future_chromium_utc(), 1, 1, 1, 1),
            ("api.example.com", "csrf", "csrf-tok-val", b"", "/v1", _future_chromium_utc(), 1, 0, 0, 1),
            (".example.com", "expired", "old-val", b"", "/", _past_chromium_utc(), 1, 0, 0, 1),
            (".other.org", "other", "other-val", b"", "/", _future_chromium_utc(), 1, 0, 0, 1),
        ],
    )

    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "plugin-data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # Hold an exclusive write lock on the original DB to verify read-only copy avoids locking.
    lock_conn = sqlite3.connect(db_path)
    lock_conn.isolation_level = None
    lock_conn.execute("BEGIN EXCLUSIVE")
    try:
        list_res = await ctx.capabilities.call("web.cookies.v1", {"action": "list", "domain": "example.com"})
        assert list_res.ok is True
        assert list_res.data["count"] == 2
        for entry in list_res.data["cookies"]:
            assert "value" not in entry
        assert "secret-sid-val" not in json.dumps(list_res.data)

        get_res = await ctx.capabilities.call(
            "web.cookies.v1",
            {"action": "get_cookies", "url": "https://api.example.com/v1/items", "domain": "example.com"},
        )
        assert get_res.ok is True
        assert get_res.data["count"] == 2
        assert "sid=secret-sid-val" in get_res.data["cookie_header"]
        assert "csrf=csrf-tok-val" in get_res.data["cookie_header"]
        assert "other-val" not in get_res.data["cookie_header"]
        assert "old-val" not in get_res.data["cookie_header"]

        http_res = await ctx.capabilities.call(
            "web.cookies.v1",
            {"action": "get", "url": "http://api.example.com/v1/items"},
        )
        assert http_res.ok is True
        assert [c["name"] for c in http_res.data["cookies"]] == ["csrf"]

        # Without explicit domain, api.example.com gets .example.com + api.example.com; with include_subdomains=False, only api.example.com
        strict_res = await ctx.capabilities.call(
            "web.cookies.v1",
            {"action": "get", "url": "https://api.example.com/v1/items", "include_subdomains": False},
        )
        assert strict_res.ok is True
        assert [c["name"] for c in strict_res.data["cookies"]] == ["csrf"]

        out_file = tmp_path / "exported" / "example-cookies.json"
        imp_res = await ctx.capabilities.call(
            "web.cookies.v1",
            {
                "action": "import",
                "domain": "example.com",
                "target": "both",
                "output_path": str(out_file),
                "secret_key": "cookies:example.com",
            },
        )
        assert imp_res.ok is True
        assert imp_res.data["vault_stored"] is True
        assert "secret-sid-val" not in json.dumps(imp_res.data)
        assert "secret-sid-val" not in json.dumps(ctx.get_config())

        vault_val = ctx.get_secret("cookies:example.com")
        assert vault_val is not None and "secret-sid-val" in vault_val

        assert out_file.is_file()
        assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(out_file.parent.stat().st_mode) == 0o700

        trav_imp = await ctx.capabilities.call(
            "web.cookies.v1",
            {"action": "import", "domain": "example.com", "target": "file", "output_path": "../escape.json"},
        )
        assert trav_imp.ok is False
        assert trav_imp.data["error_code"] == "path_safety"
    finally:
        lock_conn.rollback()
        lock_conn.close()
        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_cookies_missing_and_encrypted_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "plugin-data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    missing = await ctx.capabilities.call("web.cookies.v1", {"action": "get", "domain": "example.com"})
    assert missing.ok is False
    assert missing.data["error_code"] == "not_found"

    profile_dir = tmp_path / "xdg-data" / "omnidesk" / "chrome-profile"
    db_path = _create_cookies_db(
        profile_dir,
        [
            (".locked.com", "auth", "", b"v11\x01\x02\x03\x04ciphertext", "/", _future_chromium_utc(), 1, 1, 1, 1),
            (".v10only.com", "sid", "", b"v10\x01\x02\x03\x04badcipher", "/", _future_chromium_utc(), 1, 1, 1, 1),
        ],
    )
    no_match = await ctx.capabilities.call("web.cookies.v1", {"action": "get", "domain": "absent.com"})
    assert no_match.ok is False
    assert no_match.data["error_code"] == "not_found"

    v10_fail = await ctx.capabilities.call("web.cookies.v1", {"action": "get", "domain": "v10only.com"})
    assert v10_fail.ok is False
    assert v10_fail.data["error_code"] == "cookie_decryption_unavailable"

    enc_get = await ctx.capabilities.call("web.cookies.v1", {"action": "get", "domain": "locked.com"})
    assert enc_get.ok is False
    assert enc_get.data["error_code"] == "cookie_decryption_unavailable"
    assert "v11" in enc_get.data["unsupported_schemes"]
    assert "ciphertext" not in json.dumps(enc_get.data)

    enc_imp = await ctx.capabilities.call("web.cookies.v1", {"action": "import", "domain": "locked.com"})
    assert enc_imp.ok is False
    assert enc_imp.data["error_code"] == "cookie_decryption_unavailable"
    assert ctx.get_secret("cookies:locked.com") is None

    plugin.set_cookie_decryptor(lambda host, name, blob: "decrypted-token" if blob.startswith(b"v11") else None)
    dec_get = await ctx.capabilities.call("web.cookies.v1", {"action": "get", "domain": "locked.com"})
    assert dec_get.ok is True
    assert dec_get.data["cookie_header"] == "auth=decrypted-token"

    # Symlinked Cookies DB must be rejected with path_safety
    real_copy = tmp_path / "real_cookies.sqlite"
    db_path.replace(real_copy)
    db_path.symlink_to(real_copy)
    sym_res = await ctx.capabilities.call("web.cookies.v1", {"action": "list", "domain": "locked.com"})
    assert sym_res.ok is False
    assert sym_res.data["error_code"] == "path_safety"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_cookie_read_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path,
        config_schema=manifest["config_schema"],
    )
    ctx.set_config("profile_dir", str(tmp_path))
    plugin = module.create_plugin()
    plugin.ctx = ctx

    def _slow_handler(*_args: Any, **_kwargs: Any) -> Any:
        time.sleep(0.25)
        return CapabilityResult.ok({"count": 0, "cookies": []})

    monkeypatch.setattr(module, "handle_cookies_action", _slow_handler)
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(25):
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    result = await plugin.handle_cookies({"action": "list", "domain": "example.com"})
    await ticker
    assert result.ok is True
    assert ticks >= 10


def test_cookie_db_busy_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path,
        config_schema=manifest["config_schema"],
    )
    _create_cookies_db(
        tmp_path,
        [("example.com", "sid", "plain-token", b"", "/", _future_chromium_utc(), 1, 1, 1, 1)],
    )

    import cookies_store

    def _busy(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cookies_store, "read_cookie_rows", _busy)
    result = module.handle_cookies_action(
        ctx,
        {"action": "get", "domain": "example.com"},
        profile_dir=tmp_path,
        default_import_target="vault",
        default_cookie_file_path="",
    )
    assert result.ok is False
    assert result.data["error_code"] == "cookie_db_busy"
