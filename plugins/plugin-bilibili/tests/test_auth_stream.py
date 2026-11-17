"""Offline tests for QR login, SecretVault/web.cookies.v1, stream resolution, 4K availability, and security."""

from __future__ import annotations

import base64
import json
import stat
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for p in (str(PLUGIN_DIR), str(REPO_ROOT), str(REPO_ROOT / "core"), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from mock_bili import MockBiliServer, MockBiliState, write_fake_ytdlp  # noqa: E402
from omnidesk_core.config import ConfigStore  # noqa: E402
from omnidesk_core.vault import SecretVault  # noqa: E402
from omnidesk_sdk import CapabilityResult, PluginContext, invoke_lifecycle_hook  # noqa: E402


def _make_ctx(tmp_path: Path, *, api_base: str = "https://api.bilibili.com") -> PluginContext:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path,
        config_store=ConfigStore(tmp_path),
        secret_vault=SecretVault(config_dir=tmp_path),
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"api_base": api_base, "request_timeout_sec": 2, "max_retries": 0})
    return ctx


@pytest.mark.asyncio
async def test_qr_login_state_machine_0600_storage_and_redaction(tmp_path: Path) -> None:
    state = MockBiliState(qr_stage="waiting")
    with MockBiliServer(state) as srv:
        from plugin import create_plugin

        plugin = create_plugin()
        ctx = _make_ctx(tmp_path, api_base=srv.base_url)
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # Before login -> not_configured
        st0 = await ctx.capabilities.call("bilibili.media.v1", {"action": "status"})
        assert st0.ok is False and st0.data["error_code"] == "not_configured"
        assert st0.data["logged_in"] is False

        # Start QR -> valid SVG + PNG
        qr = await ctx.capabilities.call("bilibili.media.v1", {"action": "qr_login_start"})
        assert qr.ok is True
        assert qr.data["qrcode_key"] == "mockqrkey123456"
        assert qr.data["qr_url"].startswith("https://passport.bilibili.com/")
        ET.fromstring(qr.data["qr_svg"])
        png_bytes = base64.b64decode(qr.data["qr_png_base64"])
        assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")

        # Poll waiting -> scanned -> expired -> confirmed
        p1 = await ctx.capabilities.call("bilibili.media.v1", {"action": "qr_login_poll"})
        assert p1.ok is True and p1.data["status"] == "waiting" and p1.data["logged_in"] is False

        state.qr_stage = "scanned"
        p2 = await ctx.capabilities.call("bilibili.media.v1", {"action": "qr_login_poll"})
        assert p2.ok is True and p2.data["status"] == "scanned" and p2.data["logged_in"] is False

        state.qr_stage = "expired"
        p3 = await ctx.capabilities.call("bilibili.media.v1", {"action": "qr_login_poll"})
        assert p3.ok is True and p3.data["status"] == "expired" and p3.data["logged_in"] is False

        qr2 = await ctx.capabilities.call("bilibili.media.v1", {"action": "qr_login_start"})
        assert qr2.ok is True
        state.qr_stage = "confirmed"
        p4 = await ctx.capabilities.call("bilibili.media.v1", {"action": "qr_login_poll"})
        assert p4.ok is True and p4.data["status"] == "confirmed" and p4.data["logged_in"] is True
        assert "secret_sess_abcdef123456" not in json.dumps(p4.data)
        assert "secret_jct_7890" not in json.dumps(p4.data)
        assert "***" in p4.data["cookie_masked"]

        # Vault file is 0600 and plain config never stores cookie
        vault_path = tmp_path / "vault.json"
        assert stat.S_IMODE(vault_path.stat().st_mode) == 0o600
        cfg_text = (tmp_path / "plugins" / "plugin-bilibili.json").read_text(encoding="utf-8")
        assert "secret_sess_abcdef123456" not in cfg_text

        # Status after login -> ok with masked cookie
        st1 = await ctx.capabilities.call("bilibili.media.v1", {"action": "status"})
        assert st1.ok is True and st1.data["logged_in"] is True
        assert st1.data["uname"] == "OmniTester"
        assert "secret_sess_abcdef123456" not in json.dumps(st1.data)

        # Invalidated session -> unauthorized
        state.nav_logged_in = False
        st2 = await ctx.capabilities.call("bilibili.media.v1", {"action": "status"})
        assert st2.ok is False and st2.data["error_code"] == "unauthorized"

        # Logout clears SecretVault
        lo = await ctx.capabilities.call("bilibili.media.v1", {"action": "logout"})
        assert lo.ok is True and lo.data["logged_in"] is False
        assert ctx.get_secret("cookie_header") is None
        st3 = await ctx.capabilities.call("bilibili.media.v1", {"action": "status"})
        assert st3.ok is False and st3.data["error_code"] == "not_configured"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_web_cookies_v1_and_playurl_4k_unavailable(tmp_path: Path) -> None:
    state = MockBiliState(allow_4k=False)
    with MockBiliServer(state) as srv:
        from plugin import create_plugin

        plugin = create_plugin()
        ctx = _make_ctx(tmp_path, api_base=srv.base_url)
        ctx.register_capability(
            "web.cookies.v1",
            lambda req: CapabilityResult.ok({"cookies": [{"name": "SESSDATA", "value": "web_bridge_cookie_123"}]}),
        )
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        st = await ctx.capabilities.call("bilibili.media.v1", {"action": "status"})
        assert st.ok is True and st.data["cookie_source"] == "web.cookies.v1"

        # 4K requested when non-VIP -> quality_unavailable, never forged
        res_4k = await ctx.capabilities.call("bilibili.media.v1", {"action": "resolve_stream", "bvid": "BV1xK4y1Z7aa", "quality": "4k"})
        assert res_4k.ok is False
        assert res_4k.data["error_code"] == "quality_unavailable"

        # 1080p succeeds with real video/audio URLs and safe headers (no Cookie in headers)
        res_1080 = await ctx.capabilities.call("bilibili.media.v1", {"action": "resolve_stream", "bvid": "BV1xK4y1Z7aa", "quality": "1080p"})
        assert res_1080.ok is True
        assert res_1080.data["quality"] == "1080p"
        assert res_1080.data["video_url"].endswith("/cdn/1080.m4s")
        assert res_1080.data["audio_url"].endswith("/cdn/audio.m4s")
        assert "Referer" in res_1080.data["headers"]
        assert "Cookie" not in res_1080.data["headers"]
        assert "web_bridge_cookie_123" not in json.dumps(res_1080.data)

        # VIP enabled -> 4K succeeds
        state.allow_4k = True
        res_vip_4k = await ctx.capabilities.call("bilibili.media.v1", {"action": "resolve_stream", "bvid": "BV1xK4y1Z7aa", "quality": "4k"})
        assert res_vip_4k.ok is True and res_vip_4k.data["quality"] == "4k" and res_vip_4k.data["qn"] == 120

        # Live stream resolution
        res_live = await ctx.capabilities.call("bilibili.media.v1", {"action": "resolve_stream", "item_id": "live:9001", "quality": "auto"})
        assert res_live.ok is True and "/live-bvc/9001.flv" in res_live.data["play_url"]

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_fake_ytdlp_subprocess_cookies_drm_and_quality_unavailable(tmp_path: Path) -> None:
    argv_log = tmp_path / "argv.json"
    fake_ytdlp = write_fake_ytdlp(
        tmp_path,
        f"""
import json, os, stat, sys
args = sys.argv[1:]
cookie_mode = None
cookie_exists = False
if "--cookies" in args:
    cpath = args[args.index("--cookies") + 1]
    cookie_exists = os.path.isfile(cpath)
    cookie_mode = stat.S_IMODE(os.stat(cpath).st_mode)
with open({str(argv_log)!r}, "w", encoding="utf-8") as f:
    json.dump({{"args": args, "cookie_exists": cookie_exists, "cookie_mode": cookie_mode}}, f)

url = args[-1]
if "BV1DRM111111" in url:
    print(json.dumps({{"title": "DRM", "has_drm": True, "formats": [{{"url": "https://upos.bilivideo.com/x.m4s", "vcodec": "hev1", "height": 1080}}]}}))
else:
    print(json.dumps({{
        "title": "Fake YtDlp Video",
        "duration": 300,
        "formats": [
            {{"format_id": "80", "format_note": "1080P", "url": "https://upos.bilivideo.com/v1080.m4s", "vcodec": "avc1", "acodec": "none", "width": 1920, "height": 1080, "fps": 60}},
            {{"format_id": "30280", "url": "https://upos.bilivideo.com/a192.m4s", "vcodec": "none", "acodec": "mp4a", "abr": 192}},
        ],
    }}))
""",
    )

    from plugin import create_plugin

    plugin = create_plugin()
    ctx = _make_ctx(tmp_path)
    ctx.set_config({"ytdlp_bin": str(fake_ytdlp), "proxy": "http://alice:proxysecret99@127.0.0.1:8888"})
    ctx.set_secret("cookie_header", "SESSDATA=ytdlp_secret_val_777")
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    res = await ctx.capabilities.call(
        "bilibili.media.v1",
        {"action": "resolve_stream", "url": "https://www.bilibili.com/video/BV1xK4y1Z7aa", "quality": "1080p"},
    )
    assert res.ok is True
    assert res.data["source"] == "yt-dlp"
    assert res.data["video_url"] == "https://upos.bilivideo.com/v1080.m4s"
    assert res.data["audio_url"] == "https://upos.bilivideo.com/a192.m4s"
    assert "ytdlp_secret_val_777" not in json.dumps(res.data)
    assert "proxysecret99" not in json.dumps(res.data)

    recorded = json.loads(argv_log.read_text(encoding="utf-8"))
    assert recorded["cookie_exists"] is True
    assert recorded["cookie_mode"] == 0o600
    assert "--proxy" in recorded["args"]
    # Ephemeral cookie file must be deleted after subprocess finishes
    cpath = recorded["args"][recorded["args"].index("--cookies") + 1]
    assert not Path(cpath).exists()

    # 4K unavailable from yt-dlp formats
    res_4k = await ctx.capabilities.call(
        "bilibili.media.v1",
        {"action": "resolve_stream", "url": "https://www.bilibili.com/video/BV1xK4y1Z7aa", "quality": "4k"},
    )
    assert res_4k.ok is False and res_4k.data["error_code"] == "quality_unavailable"

    # DRM rejection
    res_drm = await ctx.capabilities.call(
        "bilibili.media.v1",
        {"action": "resolve_stream", "url": "https://www.bilibili.com/video/BV1DRM111111", "quality": "1080p"},
    )
    assert res_drm.ok is False and res_drm.data["error_code"] == "drm_protected"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_security_url_allowlist_traversal_and_redirect_blocking(tmp_path: Path) -> None:
    state = MockBiliState(redirect_to="https://evil.com/steal")
    with MockBiliServer(state) as srv:
        from plugin import create_plugin

        plugin = create_plugin()
        ctx = _make_ctx(tmp_path, api_base=srv.base_url)
        ctx.set_secret("cookie_header", "SESSDATA=secret_leak_check")
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # Non-bilibili URLs rejected
        for bad_url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://evil.com/video/BV1xK4y1Z7aa",
            "http://169.254.169.254/latest/meta-data",
            "file:///etc/passwd",
            "http://www.bilibili.com/video/BV1xK4y1Z7aa",
        ):
            r = await ctx.capabilities.call("bilibili.media.v1", {"action": "resolve_stream", "url": bad_url})
            assert r.ok is False, f"Expected rejection for {bad_url}"

        # Path traversal rejected
        for trav in ("../../etc/passwd", "https://www.bilibili.com/video/%2e%2e/admin"):
            r = await ctx.capabilities.call("bilibili.media.v1", {"action": "resolve_stream", "url": trav})
            assert r.ok is False and r.data["error_code"] == "path_safety"

        r_bin = await ctx.capabilities.call(
            "bilibili.media.v1",
            {"action": "resolve_stream", "bvid": "BV1xK4y1Z7aa", "ytdlp_bin": "../evil-bin"},
        )
        assert r_bin.ok is False and r_bin.data["error_code"] == "path_safety"

        # Redirect to evil.com is blocked
        r_redir = await ctx.capabilities.call("bilibili.media.v1", {"action": "status"})
        assert r_redir.ok is False
        assert "secret_leak_check" not in r_redir.error

        # Unsafe api_base is rejected
        ctx.set_config("api_base", "https://evil.com")
        health = await invoke_lifecycle_hook(plugin.on_health_check)
        assert health["ok"] is False

        # Unsafe proxy rejected without leaking proxy credentials
        ctx.set_config("api_base", srv.base_url)
        bad_proxy = await ctx.capabilities.call(
            "bilibili.media.v1",
            {"action": "resolve_stream", "bvid": "BV1xK4y1Z7aa", "proxy": "ftp://bob:pw999@127.0.0.1:2121"},
        )
        assert bad_proxy.ok is False and bad_proxy.data["error_code"] == "invalid_argument"
        assert "pw999" not in bad_proxy.error

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_ytdlp_timeout_failure_redaction_and_default_local_vault(tmp_path: Path) -> None:
    slow_ytdlp = write_fake_ytdlp(
        tmp_path,
        """
import sys, time
if "--timeout-mode" in sys.argv or "BV1TIMEOUT11" in sys.argv[-1]:
    time.sleep(2.0)
sys.stderr.write("ERROR: HTTP 401 login required for SESSDATA=leaked_tok_12345")
sys.exit(1)
""",
    )
    from plugin import create_plugin

    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        config_dir=tmp_path,
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"ytdlp_bin": str(slow_ytdlp), "request_timeout_sec": 0.2})
    ctx.set_secret("cookie_header", "SESSDATA=leaked_tok_12345")
    plugin = create_plugin()
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    r_fail = await ctx.capabilities.call(
        "bilibili.media.v1",
        {"action": "resolve_stream", "url": "https://www.bilibili.com/video/BV1xK4y1Z7aa"},
    )
    assert r_fail.ok is False and r_fail.data["error_code"] == "unauthorized"
    assert "leaked_tok_12345" not in r_fail.error

    r_to = await ctx.capabilities.call(
        "bilibili.media.v1",
        {"action": "resolve_stream", "url": "https://www.bilibili.com/video/BV1TIMEOUT11"},
    )
    assert r_to.ok is False and r_to.data["error_code"] == "timeout"

    lo = await ctx.capabilities.call("bilibili.media.v1", {"action": "logout"})
    assert lo.ok is True and ctx.get_secret("cookie_header") is None

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
