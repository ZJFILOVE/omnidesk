"""Offline search, HTTPS/SSRF, 16MB/zip-bomb/traversal/symlink, encoding, and atomic write tests."""

from __future__ import annotations

import io
import json
import stat
import sys
import time
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


def _build_zip(entries: list[tuple[str, bytes, int | None]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content, mode in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            zf.writestr(info, content)
    return buf.getvalue()


async def _setup_ctx(tmp_path: Path, plugin: Any, config: dict[str, Any] | None = None) -> PluginContext:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    if config:
        ctx.set_config(config)
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    return ctx


@pytest.mark.asyncio
async def test_search_assrt_empty_401_timeout_and_offline(tmp_path: Path, make_https_server) -> None:
    module = load_sub_hunter_module("plugin")

    def _responder(handler: Any) -> None:
        if handler.path.startswith("/v1/sub/search"):
            auth = handler.headers.get("Authorization", "")
            if auth == "Bearer bad-token":
                handler.send_response(401)
                handler.end_headers()
                return
            if "empty=1" in handler.path or "NoMatch" in handler.path:
                body = json.dumps({"status": 0, "sub": {"subs": []}}).encode()
            elif "SlowMovie" in handler.path:
                time.sleep(0.4)
                body = json.dumps({"status": 0, "sub": {"subs": []}}).encode()
            else:
                body = json.dumps(
                    {
                        "status": 0,
                        "sub": {
                            "subs": [
                                {"id": 42, "native_name": "Severance", "videoname": "S02E03", "subtype": "srt", "lang": {"desc": "双语"}}
                            ]
                        },
                    }
                ).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        handler.send_response(404)
        handler.end_headers()

    srv = make_https_server(_responder)
    plugin = module.SubHunterPlugin(ssl_context=srv.ssl_context)
    ctx = await _setup_ctx(tmp_path, plugin, {"provider": "assrt", "assrt_api_base": srv.base_url})

    no_tok = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "search", "title": "Severance"})
    assert no_tok.ok is False
    assert no_tok.data["error_code"] == "not_configured"

    ctx.set_secret("assrt_token", "valid-assrt-secret")
    ok_res = await ctx.capabilities.call(
        "subtitle.fetch.v1",
        {"action": "search", "title": "Severance", "imdb_id": "tt11280740", "season": 2, "episode": 3, "language": "zh"},
    )
    assert ok_res.ok is True
    assert ok_res.data["count"] == 1
    assert ok_res.data["results"][0]["id"] == "42"
    assert srv.requests[-1]["headers"]["authorization"] == "Bearer valid-assrt-secret"

    empty_res = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "search", "query": "NoMatch"})
    assert empty_res.ok is True
    assert empty_res.data["results"] == []
    assert empty_res.data["count"] == 0

    to_res = await ctx.capabilities.call(
        "subtitle.fetch.v1", {"action": "search", "query": "SlowMovie", "timeout_sec": 0.1}
    )
    assert to_res.ok is False
    assert to_res.data["error_code"] == "timeout"

    ctx.set_secret("assrt_token", "bad-token")
    unauth = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "search", "query": "Severance"})
    assert unauth.ok is False
    assert unauth.data["error_code"] == "permission_denied"
    assert unauth.data["http_status"] == 401
    assert "bad-token" not in unauth.error
    assert "bad-token" not in json.dumps(unauth.data)

    ctx.set_config("assrt_api_base", "https://127.0.0.1:1")
    off_res = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "search", "query": "Severance"})
    assert off_res.ok is False
    assert off_res.data["error_code"] == "service_offline"
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_https_only_ssrf_and_16mb_limit(tmp_path: Path, make_https_server) -> None:
    module = load_sub_hunter_module("plugin")

    def _responder(handler: Any) -> None:
        if handler.path == "/huge.srt":
            body = b"1\n00:00:01,000 --> 00:00:02,000\n" + (b"A" * (16 * 1024 * 1024 + 128))
        else:
            body = b"1\n00:00:01,000 --> 00:00:02,000\nOK\n"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    srv = make_https_server(_responder)
    plugin = module.SubHunterPlugin(ssl_context=srv.ssl_context)
    dl_root = tmp_path / "dl"
    ctx = await _setup_ctx(tmp_path, plugin, {"download_root": str(dl_root), "max_download_mb": 64})

    for bad_url in ("http://example.com/sub.srt", "http://127.0.0.1:8000/sub.srt", "file:///etc/passwd"):
        r = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "download", "url": bad_url})
        assert r.ok is False
        assert r.data["error_code"] == "invalid_argument"

    r_ssrf = await ctx.capabilities.call(
        "subtitle.fetch.v1", {"action": "download", "url": "https://169.254.169.254/latest/meta-data"}
    )
    assert r_ssrf.ok is False
    assert r_ssrf.data["error_code"] == "path_safety"

    r_escape = await ctx.capabilities.call(
        "subtitle.fetch.v1",
        {"action": "download", "url": f"{srv.base_url}/ok.srt", "output_dir": str(tmp_path / "outside")},
    )
    assert r_escape.ok is False
    assert r_escape.data["error_code"] == "path_safety"

    r_huge = await ctx.capabilities.call(
        "subtitle.fetch.v1", {"action": "download", "url": f"{srv.base_url}/huge.srt"}
    )
    assert r_huge.ok is False
    assert r_huge.data["error_code"] == "validation_error"
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_zip_bomb_traversal_symlink_and_encoding_conversion(tmp_path: Path, make_https_server) -> None:
    module = load_sub_hunter_module("plugin")
    gb_srt = "1\n00:00:01,000 --> 00:00:03,000\n简体中文字幕测试，镕喆。\n".encode("gb18030")
    big5_srt = "1\n00:00:01,000 --> 00:00:03,000\n繁體中文字幕測試！時間軸與編碼。\n".encode("big5")

    good_zip = _build_zip([("sub/movie.chs.srt", gb_srt, 0o100644), ("movie.cht.srt", big5_srt, 0o100644)])
    traversal_zip = _build_zip([("good.srt", b"1\n00:00:01,000 --> 00:00:02,000\nhi\n", None), ("../../evil.srt", b"x", None)])
    symlink_zip = _build_zip([("link.srt", b"/etc/passwd", stat.S_IFLNK | 0o777)])
    bomb_zip = _build_zip([("bomb.srt", b"A" * (2 * 1024 * 1024), None)])

    payloads = {
        "/good.zip": good_zip,
        "/traversal.zip": traversal_zip,
        "/symlink.zip": symlink_zip,
        "/bomb.zip": bomb_zip,
    }

    def _responder(handler: Any) -> None:
        data = payloads[handler.path]
        handler.send_response(200)
        handler.send_header("Content-Type", "application/zip")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    srv = make_https_server(_responder)
    plugin = module.SubHunterPlugin(ssl_context=srv.ssl_context)
    dl_root = tmp_path / "subs"
    ctx = await _setup_ctx(tmp_path, plugin, {"download_root": str(dl_root), "max_download_mb": 1})

    r_trav = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "download", "url": f"{srv.base_url}/traversal.zip"})
    assert r_trav.ok is False
    assert r_trav.data["error_code"] == "path_safety"
    assert not (dl_root / "good.srt").exists()

    r_sym = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "download", "url": f"{srv.base_url}/symlink.zip"})
    assert r_sym.ok is False
    assert r_sym.data["error_code"] == "path_safety"

    r_bomb = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "download", "url": f"{srv.base_url}/bomb.zip"})
    assert r_bomb.ok is False
    assert r_bomb.data["error_code"] == "validation_error"

    r_ok = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "download", "url": f"{srv.base_url}/good.zip"})
    assert r_ok.ok is True
    assert r_ok.data["count"] == 2
    assert "简体中文字幕测试，镕喆。" in (dl_root / "movie.chs.srt").read_text(encoding="utf-8")
    assert "繁體中文字幕測試！時間軸與編碼。" in (dl_root / "movie.cht.srt").read_text(encoding="utf-8")
    assert not list(dl_root.glob("*.tmp"))

    (dl_root / "movie.chs.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n人工精校版\n", encoding="utf-8")
    r_prot = await ctx.capabilities.call("subtitle.fetch.v1", {"action": "download", "url": f"{srv.base_url}/good.zip"})
    assert r_prot.ok is False
    assert r_prot.data["error_code"] == "conflict"
    assert "人工精校版" in (dl_root / "movie.chs.srt").read_text(encoding="utf-8")
    await ctx.acleanup()
