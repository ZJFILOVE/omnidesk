"""Additional security, redirect-isolation, gzip/zip-bomb, UTF-16, and provider-ID download tests."""

from __future__ import annotations

import gzip
import io
import json
import struct
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import load_sub_hunter_module  # noqa: E402
from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402


def _make_spoofed_zip_bomb() -> bytes:
    """Create a zip whose central directory & local header lie about file_size while stream > 1 MiB."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("spoof.srt", b"A" * (1200 * 1024))
    raw = bytearray(buf.getvalue())
    # Patch local file header (offset 22..26) & central directory uncompressed size to 50 bytes.
    raw[22:26] = struct.pack("<I", 50)
    cd_idx = raw.rfind(b"PK\x01\x02")
    assert cd_idx != -1
    raw[cd_idx + 24 : cd_idx + 28] = struct.pack("<I", 50)
    return bytes(raw)


def _make_many_entries_zip(count: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for idx in range(count):
            zf.writestr(f"e{idx}.txt", b"x")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_redirect_strips_credentials_and_blocks_http_downgrade(tmp_path: Path, make_https_server) -> None:
    module = load_sub_hunter_module("plugin")

    def _dest_responder(handler: Any) -> None:
        body = b"1\n00:00:01,000 --> 00:00:02,000\nRedirected OK\n"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    dest_srv = make_https_server(_dest_responder)

    def _origin_responder(handler: Any) -> None:
        if handler.path.startswith("/v1/sub/detail"):
            payload = {"status": 0, "sub": {"subs": [{"filelist": [{"url": f"{origin_srv.base_url}/redir/sub.srt"}]}]}}
            body = json.dumps(payload).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        if handler.path.startswith("/redir/"):
            handler.send_response(302)
            handler.send_header("Location", f"{dest_srv.base_url}/final.srt")
            handler.end_headers()
            return
        if handler.path.startswith("/downgrade"):
            handler.send_response(302)
            handler.send_header("Location", "http://example.com/sub.srt")
            handler.end_headers()
            return
        handler.send_response(404)
        handler.end_headers()

    origin_srv = make_https_server(_origin_responder)
    plugin = module.SubHunterPlugin(ssl_context=origin_srv.ssl_context)
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    dl_root = tmp_path / "dl"
    ctx.set_config({"assrt_api_base": origin_srv.base_url, "download_root": str(dl_root)})
    ctx.set_secret("assrt_token", "tok-only-for-assrt")
    ctx.set_secret("cookie_header", "sess=origin-secret-cookie")
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    res = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "download", "provider": "assrt", "id": "42"})
    assert res.ok is True
    assert res.data["count"] == 1
    assert "Redirected OK" in Path(res.data["files"][0]["path"]).read_text(encoding="utf-8")
    # Origin got cookie; cross-origin dest MUST NOT receive cookie or authorization!
    assert origin_srv.requests[-1]["headers"].get("cookie") == "sess=origin-secret-cookie"
    assert "cookie" not in dest_srv.requests[-1]["headers"]
    assert "authorization" not in dest_srv.requests[-1]["headers"]

    down_res = await ctx.capabilities.call(
        "subtitle.fetch.v1", {"action": "download", "url": f"{origin_srv.base_url}/downgrade"}
    )
    assert down_res.ok is False
    assert down_res.data["error_code"] == "invalid_argument"
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_spoofed_zip_bomb_entry_limit_gzip_bomb_utf16_and_custom_provider(tmp_path: Path, make_https_server) -> None:
    module = load_sub_hunter_module("plugin")
    utf16_srt = "1\n00:00:01,000 --> 00:00:02,000\nUTF-16 字幕测试\n".encode("utf-16")
    gz_bomb = gzip.compress(b"Z" * (1200 * 1024))
    spoof_zip = _make_spoofed_zip_bomb()
    many_zip = _make_many_entries_zip(205)

    def _responder(handler: Any) -> None:
        if handler.path.startswith("/search?"):
            body = json.dumps({"results": [{"id": "c1", "title": "Custom Hit", "language": "zh-CN"}]}).encode()
            ctype = "application/json"
        elif handler.path.startswith("/download?id=c1"):
            body, ctype = utf16_srt, "text/plain"
        elif handler.path == "/spoof.zip":
            body, ctype = spoof_zip, "application/zip"
        elif handler.path == "/many.zip":
            body, ctype = many_zip, "application/zip"
        elif handler.path == "/bomb.srt.gz":
            body, ctype = gz_bomb, "application/gzip"
        else:
            handler.send_response(404)
            handler.end_headers()
            return
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    srv = make_https_server(_responder)
    plugin = module.SubHunterPlugin(ssl_context=srv.ssl_context)
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    dl_root = tmp_path / "dl"
    ctx.set_config(
        {
            "provider": "custom",
            "custom_api_base": srv.base_url,
            "download_root": str(dl_root),
            "max_download_mb": 1,
        }
    )
    ctx.set_secret("custom_api_key", "custom-secret-key")
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    s_res = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "search", "title": "Test"})
    assert s_res.ok is True
    assert s_res.data["results"][0]["id"] == "c1"
    assert srv.requests[-1]["headers"]["authorization"] == "Bearer custom-secret-key"

    dl_res = await ctx.capabilities.call(
        "subtitle.fetch.v1", {"action": "download", "provider": "custom", "id": "c1", "filename": "u16.srt"}
    )
    assert dl_res.ok is True
    assert dl_res.data["files"][0]["source_encoding"] == "utf-16"
    assert "UTF-16 字幕测试" in (dl_root / "u16.srt").read_text(encoding="utf-8")

    for bad_path in ("/spoof.zip", "/many.zip", "/bomb.srt.gz"):
        r = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "download", "url": f"{srv.base_url}{bad_path}"})
        assert r.ok is False
        assert r.data["error_code"] == "validation_error"
    await ctx.acleanup()
