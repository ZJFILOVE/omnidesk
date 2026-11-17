"""Comprehensive offline tests for plugin-v2ex manifest, lifecycle, auth, HTTP security, parsing, pagination, and translation."""

from __future__ import annotations

import asyncio
import http.server
import importlib.util
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
for _entry in (str(REPO_ROOT), str(PLUGIN_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from omnidesk_sdk import BasePlugin, PluginContext, TextArticle, TextFeedItem, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_v2ex_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plugin_v2ex_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_ctx(tmp_path: Path, manifest: dict[str, Any]) -> PluginContext:
    return PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )


class _MockV2exServer:
    """Thread-safe local loopback HTTP server serving real V2EX HTML and JSON payloads."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.mode = "ok"
        self.delay_sec = 0.0
        self.custom_routes: dict[str, tuple[int, dict[str, str], str | bytes]] = {}
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                full_path = self.path
                outer.requests.append(
                    {
                        "path": parsed.path,
                        "full_path": full_path,
                        "query": qs,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                if outer.delay_sec > 0:
                    time.sleep(outer.delay_sec)

                if full_path in outer.custom_routes:
                    st, hdrs, body = outer.custom_routes[full_path]
                    self._send(st, body, hdrs)
                    return

                if outer.mode == "500":
                    self._send(500, "Internal Server Error", {"Content-Type": "text/plain"})
                    return
                if outer.mode == "429":
                    self._send(429, "Too Many Requests", {"Content-Type": "text/plain"})
                    return
                if outer.mode == "cloudflare":
                    self._send(
                        403,
                        "<html><title>Just a moment...</title><body><div class='cf-turnstile'>Verify</div></body></html>",
                        {"Content-Type": "text/html; charset=utf-8"},
                    )
                    return
                if outer.mode == "malformed_html":
                    self._send(200, "<<<not valid v2ex html>>>", {"Content-Type": "text/html; charset=utf-8"})
                    return
                if outer.mode == "login_wall":
                    self._send(
                        302,
                        "",
                        {"Location": f"{outer.base_url}/signin?next={parsed.path}"},
                    )
                    return

                if parsed.path == "/signin":
                    self._send(
                        200,
                        '<html><head><title>V2EX › 登录</title></head><body><form action="/signin">你要查看的页面需要先登录</form></body></html>',
                        {"Content-Type": "text/html; charset=utf-8"},
                    )
                    return

                if parsed.path == "/" and qs.get("tab") == ["apple"]:
                    self._send(
                        200,
                        """
                        <div id="Main"><div class="box">
                            <div class="cell item">
                                <span class="item_title"><a href="/t/30001#reply5">M4 Max MacBook Pro Experience</a></span>
                                <span class="topic_info">
                                    <a class="node" href="/go/apple">Apple</a>
                                    <strong><a href="/member/macuser">macuser</a></strong>
                                    <span title="2026-11-14 12:00:00 +08:00">30 分钟前</span>
                                </span>
                                <a class="count_livid" href="/t/30001#reply5">5</a>
                            </div>
                        </div></div>
                        """,
                        {"Content-Type": "text/html; charset=utf-8"},
                    )
                    return

                if parsed.path == "/" and qs.get("tab", ["hot"])[0] in {"hot", "tech"}:
                    self._send(
                        200,
                        """
                        <div id="Main"><div class="box">
                            <div class="cell item">
                                <span class="item_title"><a href="/t/1045678#reply12" class="topic-link">Linux 6.12 内核调度器分析</a></span>
                                <span class="topic_info">
                                    <a class="node" href="/go/linux">Linux</a>
                                    <strong><a href="/member/alice">alice</a></strong>
                                    <span title="2026-11-14 10:00:00 +08:00">1 小时前</span>
                                    <div class="votes">♥ 7</div>
                                </span>
                                <a class="count_livid" href="/t/1045678#reply12">12</a>
                            </div>
                        </div></div>
                        """,
                        {"Content-Type": "text/html; charset=utf-8"},
                    )
                    return

                if parsed.path == "/recent":
                    p = int(qs.get("p", ["1"])[0])
                    if p == 2:
                        self._send(
                            200,
                            """
                            <div id="Main"><div class="box">
                                <div class="cell item">
                                    <span class="item_title"><a href="/t/1045700">Recent Page 2 Topic</a></span>
                                    <span class="topic_info"><strong><a href="/member/bob">bob</a></strong></span>
                                </div>
                            </div></div>
                            """,
                            {"Content-Type": "text/html; charset=utf-8"},
                        )
                        return
                    self._send(
                        200,
                        '<div id="Main"><div class="box"></div></div>',
                        {"Content-Type": "text/html; charset=utf-8"},
                    )
                    return

                if parsed.path == "/go/python":
                    p = int(qs.get("p", ["1"])[0])
                    if p >= 99:
                        self._send(
                            200,
                            '<div id="Main"><div class="box"></div></div>',
                            {"Content-Type": "text/html; charset=utf-8"},
                        )
                        return
                    self._send(
                        200,
                        """
                        <div id="Main"><div id="TopicsNode" class="box">
                            <div class="cell">
                                <span class="item_title"><a href="/t/20001#reply2">Python 3.14 Free-Threaded</a></span>
                                <span class="topic_info"><strong><a href="/member/pydev">pydev</a></strong></span>
                            </div>
                        </div></div>
                        """,
                        {"Content-Type": "text/html; charset=utf-8"},
                    )
                    return

                if parsed.path == "/my/following":
                    cookie = self.headers.get("Cookie", "")
                    if "A2=" not in cookie:
                        self._send(302, "", {"Location": f"{outer.base_url}/signin"})
                        return
                    self._send(
                        200,
                        """
                        <div id="Main"><div class="box">
                            <div class="cell item">
                                <span class="item_title"><a href="/t/88001#reply3">Following Stream Topic</a></span>
                                <span class="topic_info"><strong><a href="/member/friend">friend</a></strong></span>
                            </div>
                        </div></div>
                        """,
                        {"Content-Type": "text/html; charset=utf-8"},
                    )
                    return

                if parsed.path == "/my/nodes":
                    if "A2=" not in self.headers.get("Cookie", ""):
                        self._send(302, "", {"Location": f"{outer.base_url}/signin"})
                        return
                    self._send(
                        200,
                        """
                        <div id="Main"><div class="box">
                            <a class="grid_item" href="/go/rust">Rust <span class="fade">42</span></a>
                            <a class="grid_item" href="/go/zig">Zig <span class="fade">9</span></a>
                        </div></div>
                        """,
                        {"Content-Type": "text/html; charset=utf-8"},
                    )
                    return

                if parsed.path == "/t/1045678":
                    p = int(qs.get("p", ["1"])[0])
                    if p == 1:
                        self._send(
                            200,
                            """
                            <div id="Main">
                                <div class="box">
                                    <div class="header">
                                        <a href="/go/programmer">程序员</a>
                                        <h1>Understanding Zero-ABI Plugins</h1>
                                        <small><a href="/member/op_user">op_user</a> · <span title="2026-11-14 09:00:00 +08:00">2 小时前</span></small>
                                    </div>
                                    <div class="cell">
                                        <div class="topic_content">
                                            <h2>Architecture Overview</h2>
                                            <p>Pure Python plugins keep interfaces clean and maintainable.</p>
                                            <blockquote>Never import sibling plugins directly.</blockquote>
                                            <pre><code class="language-python">def hello():
    return "world"</code></pre>
                                            <p>$$E = mc^2$$</p>
                                        </div>
                                    </div>
                                    <div class="subtle">
                                        <span class="fade">第 1 条附言 · 1 小时前</span>
                                        <div class="topic_content"><p>Updated benchmarks attached.</p></div>
                                    </div>
                                </div>
                                <div class="box">
                                    <input type="number" class="page_input" value="1" max="2" />
                                    <div id="r_101" class="cell">
                                        <strong><a href="/member/bob" class="dark">bob</a></strong>
                                        <span class="ago">50 分钟前</span>
                                        <span class="small fade">♥ 3</span>
                                        <span class="no">1</span>
                                        <div class="reply_content">Great architecture write-up!</div>
                                    </div>
                                    <div id="r_102" class="cell">
                                        <strong><a href="/member/op_user" class="dark">op_user</a></strong>
                                        <span class="ago">40 分钟前</span>
                                        <span class="no">2</span>
                                        <div class="reply_content">@<a href="/member/bob">bob</a> Thanks for reading!</div>
                                    </div>
                                </div>
                            </div>
                            """,
                            {"Content-Type": "text/html; charset=utf-8"},
                        )
                        return
                    if p == 2:
                        self._send(
                            200,
                            """
                            <div id="Main">
                                <div class="box">
                                    <div class="header"><h1>Understanding Zero-ABI Plugins</h1></div>
                                </div>
                                <div class="box">
                                    <input type="number" class="page_input" value="2" max="2" />
                                    <div id="r_103" class="cell">
                                        <strong><a href="/member/dave" class="dark">dave</a></strong>
                                        <span class="ago">10 分钟前</span>
                                        <span class="no">3</span>
                                        <div class="reply_content">@bob #1 Fully agreed with #1.</div>
                                    </div>
                                </div>
                            </div>
                            """,
                            {"Content-Type": "text/html; charset=utf-8"},
                        )
                        return
                    # Simulate V2EX server clamping ?p=99 back to last page (value="2", max="2")
                    self._send(
                        200,
                        """
                        <div id="Main">
                            <div class="box"><div class="header"><h1>Understanding Zero-ABI Plugins</h1></div></div>
                            <div class="box">
                                <input type="number" class="page_input" value="2" max="2" />
                                <div id="r_103" class="cell">
                                    <strong><a href="/member/dave" class="dark">dave</a></strong>
                                    <span class="no">3</span>
                                    <div class="reply_content">@bob #1 Fully agreed with #1.</div>
                                </div>
                            </div>
                        </div>
                        """,
                        {"Content-Type": "text/html; charset=utf-8"},
                    )
                    return

                # API v2 endpoints (PAT required)
                if parsed.path.startswith("/api/v2/"):
                    auth = self.headers.get("Authorization", "")
                    if auth != "Bearer pat_v2_valid_token":
                        self._send(401, json.dumps({"success": False, "message": "Invalid token"}), {"Content-Type": "application/json"})
                        return
                    if parsed.path == "/api/v2/nodes/python/topics":
                        p = int(qs.get("p", ["1"])[0])
                        if p > 1:
                            self._send(200, json.dumps({"success": True, "result": []}), {"Content-Type": "application/json"})
                            return
                        self._send(
                            200,
                            json.dumps(
                                {
                                    "success": True,
                                    "result": [
                                        {
                                            "id": 20001,
                                            "title": "Python 3.14 Free-Threaded Benchmarks",
                                            "content": "Testing free-threaded performance.",
                                            "url": f"{outer.base_url}/t/20001",
                                            "replies": 21,
                                            "created": 1763100000,
                                            "member": {"username": "py_dev"},
                                            "node": {"name": "python", "title": "Python"},
                                        }
                                    ],
                                }
                            ),
                            {"Content-Type": "application/json"},
                        )
                        return
                    if parsed.path == "/api/v2/topics/20001":
                        self._send(
                            200,
                            json.dumps(
                                {
                                    "success": True,
                                    "result": {
                                        "id": 20001,
                                        "title": "Python 3.14 Free-Threaded Benchmarks",
                                        "content": "# Environment\n\n```bash\npython3 -X gil=0 bench.py\n```\n\n$$\\alpha + \\beta = \\gamma$$\n\nGreat speedup observed.",
                                        "url": f"{outer.base_url}/t/20001",
                                        "replies": 21,
                                        "created": 1763100000,
                                        "member": {"username": "py_dev"},
                                        "node": {"name": "python", "title": "Python"},
                                        "supplements": [
                                            {"created": 1763100500, "content": "Added multi-core graph."}
                                        ],
                                    },
                                }
                            ),
                            {"Content-Type": "application/json"},
                        )
                        return
                    if parsed.path == "/api/v2/topics/20001/replies":
                        p = int(qs.get("p", ["1"])[0])
                        if p == 1:
                            arr = [
                                {
                                    "id": 5000 + i,
                                    "content": "First floor benchmark note" if i == 1 else f"Reply {i}",
                                    "created": 1763100000 + i * 60,
                                    "member": {"username": f"user_{i}"},
                                }
                                for i in range(1, 21)
                            ]
                            self._send(200, json.dumps({"success": True, "result": arr}), {"Content-Type": "application/json"})
                            return
                        if p == 2:
                            self._send(
                                200,
                                json.dumps(
                                    {
                                        "success": True,
                                        "result": [
                                            {
                                                "id": 5021,
                                                "content": "@user_1 Follow-up data attached",
                                                "created": 1763102000,
                                                "member": {"username": "py_dev"},
                                            }
                                        ],
                                    }
                                ),
                                {"Content-Type": "application/json"},
                            )
                            return
                        self._send(200, json.dumps({"success": True, "result": []}), {"Content-Type": "application/json"})
                        return

                self._send(404, "Not Found", {"Content-Type": "text/plain"})

            def _send(self, status: int, body: str | bytes, headers: dict[str, str] | None = None) -> None:
                raw = body if isinstance(body, bytes) else body.encode("utf-8")
                try:
                    self.send_response(status)
                    for k, v in (headers or {}).items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except BrokenPipeError:
                    pass

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _MockV2exServer:
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def test_manifest_v2_schema_and_v1_config_parity() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-v2ex"
    assert manifest["kind"] == "workspace"
    assert manifest["domain"] == "text"
    assert manifest["runtime"] == "python"
    assert manifest["entry"] == "plugin:create_plugin"
    assert manifest["provides"] == ["text.source.v1", "text.comments.v1"]
    assert {"web.embed.v1", "web.cookies.v1", "web.extract.v1", "web.fetch.v1", "llm.translate.v1", "llm.chat.v1"}.issubset(
        set(manifest["consumes"])
    )
    assert {"config:read", "config:write", "secret:vault", "network:wan", "ipc:capability_call"}.issubset(
        set(manifest["permissions"])
    )

    allowed_top = set(schema["properties"].keys())
    assert set(manifest.keys()).issubset(allowed_top)
    for req_key in schema["required"]:
        assert req_key in manifest

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(c) for c in manifest["provides"] + manifest["consumes"])

    fields = {item["key"]: item for item in manifest["config_schema"]}
    # Verify full v1 config_schema parity + translation toggle
    assert fields["chrome_cookie_source"]["type"] == "select"
    assert fields["chrome_cookie_source"]["default"] == "auto"
    assert fields["chrome_cookie_source"]["options"] == ["auto", "plugin-chrome-web", "vault", "file", "disabled"]
    assert fields["cookie_header"]["type"] == "secret"
    assert fields["cookie_file_path"]["type"] == "string"
    assert fields["prefer_chrome_fetch"]["type"] == "bool"
    assert fields["prefer_chrome_fetch"]["default"] is False
    assert fields["request_timeout_sec"]["type"] == "int"
    assert fields["request_timeout_sec"]["default"] == 15
    assert fields["api_token"]["type"] == "secret"
    assert fields["subscribed_nodes"]["type"] == "string"
    assert fields["subscribed_nodes"]["default"] == "programmer,python,linux,macos"
    assert fields["auto_translate"]["type"] == "bool"
    assert fields["auto_translate"]["default"] is False
    assert fields["target_lang"]["type"] == "string"
    assert fields["target_lang"]["default"] == "zh-CN"


@pytest.mark.asyncio
async def test_lifecycle_health_check_and_resource_cleanup(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    plugin = mod.create_plugin()
    assert isinstance(plugin, BasePlugin)

    ctx = _make_ctx(tmp_path, manifest)
    ctx.set_secret("api_token", "pat_secret_never_leak")
    ctx.set_secret("cookie_header", "A2=secret_a2_never_leak; PB3_SESSION=pb3_secret")
    assert "api_token" not in ctx.get_config()
    assert "cookie_header" not in ctx.get_config()

    await invoke_lifecycle_hook(plugin.on_load, ctx)
    assert ctx.capabilities.has("text.source.v1")
    assert ctx.capabilities.has("text.comments.v1")
    assert len(ctx.domains.text_sources()) == 1
    assert len(ctx.domains.workspace_tabs("text")) == 1

    health = await invoke_lifecycle_hook(plugin.on_health_check)
    assert health["ok"] is True
    assert health["plugin_id"] == "plugin-v2ex"
    assert health["has_api_token"] is True
    assert health["has_a2"] is True
    health_dump = json.dumps(health, ensure_ascii=False)
    assert "pat_secret_never_leak" not in health_dump
    assert "secret_a2_never_leak" not in health_dump
    assert "pb3_secret" not in health_dump

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
    assert not ctx.capabilities.has("text.source.v1")
    assert not ctx.capabilities.has("text.comments.v1")
    assert len(ctx.domains.text_sources()) == 0
    assert len(ctx.domains.workspace_tabs("text")) == 0


@pytest.mark.asyncio
async def test_auth_priority_redaction_and_fail_closed_chrome_sync(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".v2ex.com\tTRUE\t/\tTRUE\t1999999999\tA2\tfile_a2_token\n"
        ".v2ex.com\tTRUE\t/\tFALSE\t1999999999\tV2EX_TAB\ttech\n"
        ".v2ex.com\tTRUE\t/\tTRUE\t1000000000\tEXPIRED\told\n"
        ".other.com\tTRUE\t/\tTRUE\t1999999999\tA2\twrong_domain\n",
        encoding="utf-8",
    )

    plugin = mod.create_plugin()
    ctx = _make_ctx(tmp_path, manifest)
    ctx.set_config({"chrome_cookie_source": "auto", "cookie_file_path": str(cookie_file)})
    ctx.set_secret("cookie_header", "A2=vault_a2_secret")
    ctx.set_secret("api_token", "Bearer pat_super_secret")

    # 1. Chrome web.cookies.v1 takes top priority over vault & file
    ctx.register_capability(
        "web.cookies.v1",
        lambda req: {
            "cookies": [
                {"name": "A2", "value": "chrome_a2_secret", "domain": ".v2ex.com", "path": "/", "secure": True},
                {"name": "PB3_SESSION", "value": "pb3_chrome", "domain": ".v2ex.com", "path": "/"},
            ]
        },
    )
    ctx.register_capability(
        "web.embed.v1",
        lambda req: {"launched": True, "url": req.get("url")},
    )
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    st_res = await ctx.capabilities.call("text.source.v1", {"action": "auth_status"})
    assert st_res.ok is True
    assert st_res.data["cookie_source"] == "chrome:web.cookies.v1"
    assert st_res.data["has_a2"] is True
    assert st_res.data["has_pb3_session"] is True
    assert st_res.data["has_api_token"] is True
    assert "chrome_a2_secret" not in json.dumps(st_res.data)
    assert "pat_super_secret" not in json.dumps(st_res.data)

    # Sync Chrome cookies writes snapshot to SecretVault
    sync_ok = await ctx.capabilities.call("text.source.v1", {"action": "auth_sync_chrome"})
    assert sync_ok.ok is True and sync_ok.data["synced"] is True
    assert "chrome_a2_secret" in (ctx.get_secret("cookie_header") or "")

    # Open login window calls web.embed.v1
    login_res = await ctx.capabilities.call("text.source.v1", {"action": "auth_open_login"})
    assert login_res.ok is True and login_res.data["opened"] is True
    assert login_res.data["login_url"] == "https://www.v2ex.com/signin"

    # 2. If Chrome has no A2 cookie, auth_sync_chrome MUST fail closed
    ctx.register_capability("web.cookies.v1", lambda req: {"cookies": [{"name": "V2EX_TAB", "value": "tech"}]})
    sync_fail = await ctx.capabilities.call("text.source.v1", {"action": "auth_sync_chrome"})
    assert sync_fail.ok is False
    assert sync_fail.data["error_code"] == "auth_required"

    # 3. Vault priority when Chrome unavailable (and test auth_set_cookie / auth_set_pat / auth_logout)
    ctx.capabilities.unregister("web.cookies.v1")
    ctx.set_config("chrome_cookie_source", "vault")
    await ctx.capabilities.call("text.source.v1", {"action": "auth_set_cookie", "cookie_header": "A2=manual_vault_a2"})
    st_vault = await ctx.capabilities.call("text.source.v1", {"action": "auth_status"})
    assert st_vault.data["cookie_source"] == "vault" and st_vault.data["has_a2"] is True

    # Empty set_pat / set_cookie fail closed
    bad_pat = await ctx.capabilities.call("text.source.v1", {"action": "auth_set_pat", "api_token": "   "})
    assert bad_pat.ok is False and bad_pat.data["error_code"] == "invalid_argument"

    # 4. File fallback filtering (ignores EXPIRED and wrong_domain)
    await ctx.capabilities.call("text.source.v1", {"action": "auth_logout"})
    ctx.set_config("chrome_cookie_source", "file")
    resolved_file = await plugin.auth.resolve_for_url("https://www.v2ex.com/")
    assert resolved_file.cookie_source_used == "file"
    assert "A2=file_a2_token" in resolved_file.cookie_header
    assert "EXPIRED" not in resolved_file.cookie_header
    assert "wrong_domain" not in resolved_file.cookie_header

    # 5. Reject path traversal & symlink cookie_file_path
    ctx.set_config("cookie_file_path", "/tmp/../etc/passwd")
    trav_res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "following"})
    assert trav_res.ok is False and trav_res.data["error_code"] == "path_safety"

    sym_path = tmp_path / "sym_cookies.txt"
    sym_path.symlink_to(cookie_file)
    ctx.set_config("cookie_file_path", str(sym_path))
    sym_res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "following"})
    assert sym_res.ok is False and sym_res.data["error_code"] == "path_safety"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_http_security_https_redirects_size_limit_and_cloudflare_fallback(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    from v2ex_http import HttpRequest
    from v2ex_security import redact_sensitive

    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    plugin = mod.create_plugin()
    ctx = _make_ctx(tmp_path, manifest)
    ctx.set_secret("cookie_header", "A2=super_secret_cookie")
    ctx.set_secret("api_token", "pat_secret_xyz")
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # 1. Non-HTTPS URL rejected
    bad_scheme = await plugin.http.perform_direct(HttpRequest(url="http://www.v2ex.com/t/100"))
    assert bad_scheme.ok is False and bad_scheme.error_code == "invalid_argument"

    # 2. Cross-origin redirect strips Cookie and Authorization
    seen_reqs: list[dict[str, Any]] = []

    def _redirect_transport(req: Any) -> Any:
        from v2ex_http import HttpResponse

        seen_reqs.append({"url": req.url, "headers": dict(req.headers)})
        if len(seen_reqs) == 1:
            return HttpResponse(
                ok=True,
                status_code=302,
                headers={"location": "https://cdn.otherhost.example/payload"},
            )
        return HttpResponse(ok=True, status_code=200, body="ok")

    plugin.http.set_transport_override(_redirect_transport)
    redir_resp = await plugin.http.perform_direct(
        HttpRequest(
            url="https://www.v2ex.com/t/100",
            headers={"Cookie": "A2=super_secret_cookie", "Authorization": "Bearer pat_secret_xyz"},
        )
    )
    assert redir_resp.ok is True and len(seen_reqs) == 2
    assert "Cookie" in seen_reqs[0]["headers"] and "Authorization" in seen_reqs[0]["headers"]
    assert "Cookie" not in seen_reqs[1]["headers"] and "Authorization" not in seen_reqs[1]["headers"]

    # 3. Max redirects cap enforced (MAX_REDIRECTS=5)
    from v2ex_http import HttpResponse

    plugin.http.set_transport_override(
        lambda req: HttpResponse(ok=True, status_code=302, headers={"location": "https://www.v2ex.com/loop"})
    )
    loop_resp = await plugin.http.perform_direct(HttpRequest(url="https://www.v2ex.com/loop"))
    assert loop_resp.ok is False and loop_resp.error_code == "blocked_redirect"

    # 4. Cloudflare 403 triggers web.fetch.v1 fallback (without forwarding Cookie header)
    plugin.http.set_transport_override(
        lambda req: HttpResponse(ok=False, status_code=403, body="Just a moment... cf-turnstile")
    )
    ctx.register_capability(
        "web.fetch.v1",
        lambda req: (
            pytest.fail("Cookie must not be passed in web.fetch.v1 headers")
            if any(k.lower() == "cookie" for k in (req.get("headers") or {}))
            else {
                "status": 200,
                "url": req["url"],
                "body": '<div id="Main"><div class="box"><div class="cell item"><span class="item_title"><a href="/t/999">Bypassed via Chrome</a></span></div></div></div>',
            }
        ),
    )
    fb_res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "tech"})
    assert fb_res.ok is True
    assert fb_res.data["items"][0]["id"] == "999"
    assert fb_res.data["items"][0]["title"] == "Bypassed via Chrome"

    # 5. If web.fetch.v1 fails and web.extract.v1 succeeds, falls back to web.extract.v1
    ctx.register_capability("web.fetch.v1", lambda req: mod.CapabilityResult.fail("cdp busy", error_code="service_offline"))
    ctx.register_capability(
        "web.extract.v1",
        lambda req: {
            "status": 200,
            "url": req["url"],
            "html": '<div id="Main"><div class="box"><div class="cell item"><span class="item_title"><a href="/t/1001">Extracted via Chrome</a></span></div></div></div>',
        },
    )
    ext_res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "tech"})
    assert ext_res.ok is True and ext_res.data["items"][0]["id"] == "1001"

    # 6. If Chrome fetch & extract are unavailable and Cloudflare blocks, fail closed with cloudflare_blocked
    ctx.capabilities.unregister("web.fetch.v1")
    ctx.capabilities.unregister("web.extract.v1")
    cf_fail = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "tech"})
    assert cf_fail.ok is False and cf_fail.data["error_code"] == "cloudflare_blocked"

    # 7. Secret redaction never leaks cookie or PAT
    redacted = redact_sensitive("Bearer pat_secret_xyz Cookie: A2=super_secret_cookie")
    assert "pat_secret_xyz" not in redacted and "super_secret_cookie" not in redacted

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_loopback_server_real_http_html_api_pagination_and_errors(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    with _MockV2exServer() as srv:
        # Also test oversized > 8 MiB response on loopback SafeHttpClient
        srv.custom_routes["/t/777777?p=1"] = (
            200,
            {"Content-Type": "text/html; charset=utf-8"},
            "x" * (8 * 1024 * 1024 + 512),
        )

        plugin = mod.V2exPlugin(base_url=srv.base_url, allow_loopback_http=True)
        ctx = _make_ctx(tmp_path, manifest)
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # 1. Channels check & TextSource registration
        src = ctx.domains.text_sources()[0]
        chans = src.channels()
        chan_ids = {c.id for c in chans}
        assert {"hot", "all", "tech", "creative", "play", "apple", "jobs", "qna", "following", "node:python"}.issubset(
            chan_ids
        )

        # 2. Guest HTML feed + query filtering + pagination boundaries
        apple_items = await src.fetch_feed("apple")
        assert len(apple_items) == 1 and isinstance(apple_items[0], TextFeedItem)
        assert apple_items[0].id == "30001" and apple_items[0].comment_count == 5

        recent_p2 = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "hot", "page": 2})
        assert recent_p2.ok is True and recent_p2.data["items"][0]["id"] == "1045700"

        empty_p99 = await ctx.capabilities.call(
            "text.source.v1", {"action": "fetch_feed", "channel_id": "node:python", "page": 99}
        )
        assert empty_p99.ok is True and empty_p99.data["items"] == [] and empty_p99.data["count"] == 0

        bad_page = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "hot", "page": 0})
        assert bad_page.ok is False and bad_page.data["error_code"] == "invalid_argument"
        assert await src.fetch_feed("hot", page=0) == []

        # 3. Unauthenticated following feed MUST fail closed with auth_required (no fake items)
        unauth_follow = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "following"})
        assert unauth_follow.ok is False and unauth_follow.data["error_code"] == "auth_required"

        # 4. Authenticated following feed & /my/nodes sync with A2 cookie
        ctx.set_secret("cookie_header", "A2=valid_a2_session")
        auth_follow = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "following"})
        assert auth_follow.ok is True and auth_follow.data["items"][0]["id"] == "88001"

        sync_nodes = await ctx.capabilities.call("text.source.v1", {"action": "sync_nodes"})
        assert sync_nodes.ok is True and sync_nodes.data["added"] == 2
        assert any(n["slug"] == "rust" for n in sync_nodes.data["nodes"])
        assert (tmp_path / "data" / "subscriptions.json").is_file()

        # 5. HTML topic parsing: code, formula, quote, heading, subtle, paginated 2-page reply tree
        art_res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "1045678"})
        assert art_res.ok is True
        assert art_res.data["source_mode"] == "html_cookie"
        assert art_res.data["pages_fetched"] == 2
        assert art_res.data["total_pages"] == 2
        assert art_res.data["total_replies"] == 3
        art = TextArticle.model_validate(art_res.data["article"])
        kinds = {p.kind for p in art.paragraphs}
        assert {"heading", "paragraph", "quote", "code", "formula"}.issubset(kinds)
        code_para = next(p for p in art.paragraphs if p.kind == "code")
        assert code_para.language == "python" and 'return "world"' in code_para.original
        formula_para = next(p for p in art.paragraphs if p.kind == "formula")
        assert formula_para.original == "E = mc^2"
        assert any(p.kind == "paragraph" and "Updated benchmarks attached." in p.original for p in art.paragraphs)
        # Comment tree: #1 (bob) has #2 (op_user OP) and #3 (dave) nested under it
        assert len(art.comments) == 1
        assert art.comments[0].author == "bob" and art.comments[0].score == 3
        assert len(art.comments[0].replies) == 2
        assert "OP" in art.comments[0].replies[0].role_badge
        assert art.comments[0].replies[1].author == "dave"

        # Single reply page boundary (page=99 past total_pages=2 returns empty comments cleanly)
        cmt_p99 = await ctx.capabilities.call("text.comments.v1", {"action": "fetch_comments", "id": "1045678", "page": 99})
        assert cmt_p99.ok is True and cmt_p99.data["comments"] == [] and cmt_p99.data["pages_fetched"] == 0

        # 6. API v2 PAT path: node feed + 21-reply multi-page topic & reply tree
        ctx.set_secret("api_token", "pat_v2_valid_token")
        py_feed = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "node:python"})
        assert py_feed.ok is True and py_feed.data["source_mode"] == "api_v2_pat"
        assert py_feed.data["items"][0]["id"] == "20001"

        v2_cmt = await ctx.capabilities.call(
            "text.comments.v1", {"action": "fetch_comments", "id": f"{srv.base_url}/t/20001"}
        )
        assert v2_cmt.ok is True
        assert v2_cmt.data["source_mode"] == "api_v2_pat"
        assert v2_cmt.data["pages_fetched"] == 2
        assert v2_cmt.data["total_replies"] == 21
        assert len(v2_cmt.data["comments"]) == 20
        assert v2_cmt.data["comments"][0]["replies"][0]["author"] == "py_dev"
        assert "OP" in v2_cmt.data["comments"][0]["replies"][0]["role_badge"]

        # Invalid PAT fails closed with auth_required (without cookie)
        ctx.set_secret("cookie_header", None)
        ctx.set_secret("api_token", "pat_wrong_secret")
        bad_pat_feed = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "node:python"})
        assert bad_pat_feed.ok is False and bad_pat_feed.data["error_code"] == "auth_required"
        assert "pat_wrong_secret" not in bad_pat_feed.error

        # 7. Error cases: 404, 429, 500, malformed HTML, login wall, response > 8 MiB, timeout
        ctx.set_secret("api_token", None)
        err_404 = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "9999999"})
        assert err_404.ok is False and err_404.data["error_code"] == "not_found"

        err_big = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "777777"})
        assert err_big.ok is False and err_big.data["error_code"] == "too_large"

        srv.mode = "429"
        err_429 = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "tech"})
        assert err_429.ok is False and err_429.data["error_code"] == "rate_limited"

        srv.mode = "500"
        err_500 = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "tech"})
        assert err_500.ok is False and err_500.data["error_code"] == "service_offline"

        srv.mode = "malformed_html"
        err_parse_feed = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "tech"})
        assert err_parse_feed.ok is False and err_parse_feed.data["error_code"] == "parse_error"
        err_parse_art = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "1045678"})
        assert err_parse_art.ok is False and err_parse_art.data["error_code"] == "parse_error"

        srv.mode = "login_wall"
        err_login = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "1045678"})
        assert err_login.ok is False and err_login.data["error_code"] == "auth_required"

        srv.mode = "ok"
        srv.delay_sec = 0.6
        ctx.set_config("request_timeout_sec", 0.2)
        err_timeout = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "tech"})
        assert err_timeout.ok is False and err_timeout.data["error_code"] == "timeout"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_translation_skips_code_formula_and_node_subscription_persistence(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    with _MockV2exServer() as srv:
        plugin = mod.V2exPlugin(base_url=srv.base_url, allow_loopback_http=True)
        ctx = _make_ctx(tmp_path, manifest)
        ctx.set_config({"auto_translate": True, "target_lang": "zh-CN"})

        seen_texts: list[str] = []

        def _fake_translate(req: dict[str, Any]) -> dict[str, Any]:
            segs = req.get("texts") or ([req["text"]] if "text" in req else [])
            seen_texts.extend(segs)
            return {"translations": [f"[ZH] {s}" for s in segs]}

        ctx.register_capability("llm.translate.v1", _fake_translate)
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "1045678"})
        assert res.ok is True
        assert res.data["translated"] is True
        assert res.data["translation_degraded"] is False

        art = TextArticle.model_validate(res.data["article"])
        assert art.title_zh == "[ZH] Understanding Zero-ABI Plugins"
        code_para = next(p for p in art.paragraphs if p.kind == "code")
        formula_para = next(p for p in art.paragraphs if p.kind == "formula")
        text_para = next(p for p in art.paragraphs if p.kind == "paragraph" and "Pure Python" in p.original)
        assert code_para.translated == ""
        assert formula_para.translated == ""
        assert text_para.translated.startswith("[ZH] ")
        assert all("def hello()" not in t and "E = mc^2" not in t for t in seen_texts)
        assert art.comments[0].translated_body == "[ZH] Great architecture write-up!"

        # Graceful degradation when llm.translate.v1 fails
        ctx.register_capability(
            "llm.translate.v1",
            lambda req: {"error": "quota exhausted", "error_code": "quota_exceeded"}
            if False
            else mod.CapabilityResult.fail("quota exhausted", error_code="quota_exceeded"),
        )
        deg_res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "1045678"})
        assert deg_res.ok is True
        assert deg_res.data["translated"] is False
        assert deg_res.data["translation_degraded"] is True
        assert "quota_exceeded" in deg_res.data["degraded_reason"]

        # Node subscribe / unsubscribe persistence across config reload
        sub_res = await ctx.capabilities.call(
            "text.source.v1", {"action": "subscribe_node", "slug": "rust", "title": "Rust"}
        )
        assert sub_res.ok is True and any(n["slug"] == "rust" for n in sub_res.data["nodes"])

        unsub_res = await ctx.capabilities.call("text.source.v1", {"action": "unsubscribe_node", "slug": "macos"})
        assert unsub_res.ok is True and all(n["slug"] != "macos" for n in unsub_res.data["nodes"])
        # Subsequent reload_config must NOT re-add unsubscribed default node
        plugin.reload_config()
        assert all(n.slug != "macos" for n in plugin.source.subscriptions.list_nodes())

        # Path traversal node slug rejected fail-closed
        bad_node = await ctx.capabilities.call("text.source.v1", {"action": "subscribe_node", "slug": "../../etc"})
        assert bad_node.ok is False and bad_node.data["error_code"] == "invalid_argument"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_api_v1_fallback_companion_summary_gzip_and_edge_cases(tmp_path: Path) -> None:
    import gzip

    mod = _load_plugin_module()
    from v2ex_parser import build_reply_tree, parse_html_topic_page
    from v2ex_security import V2exError

    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    # 1. Parser fidelity: script math/tex, data-formula, katex-display, page_normal pagination, and depth clamp
    rich_html = """
    <div id="Main">
      <div class="box">
        <div class="header"><h1>Math &amp; Code</h1><a href="/member/alice">alice</a></div>
        <div class="cell"><div class="topic_content">
          <img data-formula="\\sum_{i=1}^n i" />
          <script type="math/tex">\\int_0^1 x^2 dx</script>
          <div class="katex-display">a^2 + b^2 = c^2</div>
          <pre><code class="language-rust">fn main() {}</code></pre>
        </div></div>
      </div>
      <div class="box">
        <a class="page_current">1</a><a class="page_normal">2</a><a class="page_normal">3</a>
      </div>
    </div>
    """
    parsed_math = parse_html_topic_page(rich_html, "42")
    formulas = [p.original for p in parsed_math.article.paragraphs if p.kind == "formula"]
    assert formulas == ["\\sum_{i=1}^n i", "\\int_0^1 x^2 dx", "a^2 + b^2 = c^2"]
    assert parsed_math.total_pages == 3

    # Depth clamping (> 24 replies chaining on previous floor)
    from v2ex_parser import RawReply

    deep_replies = [
        RawReply(id=f"r_{i}", floor=i, author=f"u{i}", body=f"#{i - 1} reply" if i > 1 else "root", explicit_floor=(i - 1 if i > 1 else 0))
        for i in range(1, 31)
    ]
    deep_tree = build_reply_tree(deep_replies, "u1")
    def _max_depth(nodes: list[Any], d: int = 1) -> int:
        if not nodes:
            return d - 1
        return max(_max_depth(n.replies, d + 1) for n in nodes)
    assert _max_depth(deep_tree) <= 24

    # 2. Loopback: gzip response, API v1 fallback when HTML gets 403 Cloudflare, summarize via llm.chat.v1, tab action
    with _MockV2exServer() as srv:
        srv.custom_routes["/t/55555?p=1"] = (
            403,
            {"Content-Type": "text/html"},
            "<html><title>Attention Required! | Cloudflare</title><body>cf-turnstile</body></html>",
        )
        srv.custom_routes["/api/topics/show.json?id=55555"] = (
            200,
            {"Content-Type": "application/json"},
            json.dumps(
                [
                    {
                        "id": 55555,
                        "title": "API v1 Fallback Topic",
                        "content": "Plain body content",
                        "replies": 1,
                        "created": 1763100000,
                        "member": {"username": "v1_author"},
                        "node": {"name": "linux", "title": "Linux"},
                    }
                ]
            ),
        )
        srv.custom_routes["/api/replies/show.json?topic_id=55555"] = (
            200,
            {"Content-Type": "application/json"},
            json.dumps([{"id": 9001, "content": "v1 reply", "created": 1763100100, "member": {"username": "r_user"}}]),
        )
        gz_html = gzip.compress(
            b'<div id="Main"><div class="box"><div class="cell item"><span class="item_title"><a href="/t/66666">Gzip Topic</a></span></div></div></div>'
        )
        srv.custom_routes["/?tab=play"] = (
            200,
            {"Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip"},
            gz_html,
        )

        plugin = mod.V2exPlugin(base_url=srv.base_url, allow_loopback_http=True)
        ctx = _make_ctx(tmp_path, manifest)
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # Gzip-compressed HTML feed end-to-end
        gz_res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "play"})
        assert gz_res.ok is True and gz_res.data["items"][0]["id"] == "66666"
        assert gz_res.data["items"][0]["title"] == "Gzip Topic"

        # API v1 fallback on Cloudflare-blocked topic page
        v1_res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "55555"})
        assert v1_res.ok is True and v1_res.data["source_mode"] == "api_v1_fallback"
        assert v1_res.data["article"]["title"] == "API v1 Fallback Topic"
        assert v1_res.data["total_replies"] == 1

        # Direct TextSource.fetch_article raises V2exError on invalid ID
        src = ctx.domains.text_sources()[0]
        with pytest.raises(V2exError) as exc_info:
            await src.fetch_article("http://www.v2ex.com/t/123")
        assert exc_info.value.error_code == "invalid_argument"

        # Summarize fails closed without llm.chat.v1, then succeeds when registered
        sum_fail = await ctx.capabilities.call("text.source.v1", {"action": "summarize", "id": "55555"})
        assert sum_fail.ok is False and sum_fail.data["error_code"] == "service_offline"

        ctx.register_capability("llm.chat.v1", lambda req: {"reply": "Summary: API v1 fallback works cleanly."})
        sum_ok = await ctx.capabilities.call("text.source.v1", {"action": "summarize", "id": "55555"})
        assert sum_ok.ok is True and "Summary:" in sum_ok.data["reply"]

        # WorkspaceTab on_action callback & config.changed event
        tab_out = await plugin.handle_tab_action("fetch_feed", {"channel_id": "apple"})
        assert tab_out["ok"] is True and tab_out["result"]["items"][0]["id"] == "30001"
        ctx.set_config("request_timeout_sec", 9)
        await ctx.emit("config.changed", {"plugin_id": "plugin-v2ex"})
        await asyncio.sleep(0.02)
        assert plugin.auth.config.request_timeout_sec == 9

        # Unknown action fails closed
        unk = await ctx.capabilities.call("text.source.v1", {"action": "not_a_real_action"})
        assert unk.ok is False and unk.data["error_code"] == "invalid_argument"
        unk_cmt = await ctx.capabilities.call("text.comments.v1", {"action": "bad_action"})
        assert unk_cmt.ok is False and unk_cmt.data["error_code"] == "invalid_argument"

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()

