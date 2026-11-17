"""Comprehensive offline contract, HTTP/HTML/JSON parsing, auth, pagination, and security tests for plugin-reddit."""

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
for _entry in (str(REPO_ROOT), str(REPO_ROOT / "core"), str(PLUGIN_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from omnidesk_core.manifest import load_manifest  # noqa: E402
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, invoke_lifecycle_hook  # noqa: E402

import reddit_auth  # noqa: E402
import reddit_http  # noqa: E402
import reddit_parser  # noqa: E402
import reddit_security  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_reddit_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sample_listing_page1(sub: str = "cpp", after: str = "t3_page2") -> dict[str, Any]:
    return {
        "kind": "Listing",
        "data": {
            "after": after,
            "before": None,
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": "cpp26refl",
                        "name": "t3_cpp26refl",
                        "subreddit": sub,
                        "subreddit_name_prefixed": f"r/{sub}",
                        "title": "C++26 Static Reflection &amp; P2996",
                        "author": "wg21_member",
                        "link_flair_text": "Proposal",
                        "score": 310,
                        "num_comments": 84,
                        "created_utc": 1763105000,
                        "permalink": f"/r/{sub}/comments/cpp26refl/cpp26_static_reflection/",
                        "is_self": True,
                        "selftext": "Compile-time reflection P2996 status update.",
                    },
                }
            ],
        },
    }


def _sample_listing_page2(sub: str = "cpp") -> dict[str, Any]:
    return {
        "kind": "Listing",
        "data": {
            "after": None,
            "before": "t3_cpp26refl",
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": "cpp26p2",
                        "name": "t3_cpp26p2",
                        "subreddit": sub,
                        "subreddit_name_prefixed": f"r/{sub}",
                        "title": "Zero-overhead Contracts in C++26",
                        "author": "std_author",
                        "score": 145,
                        "num_comments": 21,
                        "created_utc": 1763106000,
                        "permalink": f"/r/{sub}/comments/cpp26p2/contracts/",
                        "is_self": True,
                        "selftext": "Contract assertions overview.",
                    },
                }
            ],
        },
    }


def _sample_thread_payload() -> list[dict[str, Any]]:
    selftext = (
        "# 1. Benchmark Setup\n\n"
        "We evaluated prefill &amp; decode throughput across batch sizes.\n\n"
        "$$\\text{Speedup} = \\frac{T_{\\text{FP16}}}{T_{\\text{FP8}}}$$\n\n"
        "> Memory bandwidth remains the primary bottleneck at long context.\n\n"
        "```cpp\n"
        "for (int b = 0; b < batch_size; ++b) {\n"
        "    launch_fp8_attention(stream, q[b], k[b], v[b]);\n"
        "}\n"
        "```"
    )
    return [
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": "1kvcache",
                            "name": "t3_1kvcache",
                            "subreddit": "LocalLLaMA",
                            "subreddit_name_prefixed": "r/LocalLLaMA",
                            "title": "FlashAttention-3 &amp; FP8 KV Cache Benchmarks",
                            "author": "llama_researcher",
                            "link_flair_text": "Benchmarks",
                            "score": 642,
                            "num_comments": 58,
                            "created_utc": 1763100000,
                            "permalink": "/r/LocalLLaMA/comments/1kvcache/flashattention3_fp8_kv_cache_benchmarks/",
                            "is_self": True,
                            "selftext": selftext,
                        },
                    }
                ]
            },
        },
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c_root1",
                            "name": "t1_c_root1",
                            "parent_id": "t3_1kvcache",
                            "author": "cuda_wizard",
                            "author_flair_text": "Kernel Dev",
                            "body": "Great numbers! How does RoPE scaling behave at 128k context?",
                            "score": 95,
                            "created_utc": 1763100600,
                            "is_submitter": False,
                            "replies": {
                                "kind": "Listing",
                                "data": {
                                    "children": [
                                        {
                                            "kind": "t1",
                                            "data": {
                                                "id": "c_lvl2",
                                                "name": "t1_c_lvl2",
                                                "parent_id": "t1_c_root1",
                                                "author": "llama_researcher",
                                                "body": "We use YaRN with dynamic scaling &gt; 64k tokens.",
                                                "score": 61,
                                                "created_utc": 1763101200,
                                                "is_submitter": True,
                                                "replies": {
                                                    "kind": "Listing",
                                                    "data": {
                                                        "children": [
                                                            {
                                                                "kind": "t1",
                                                                "data": {
                                                                    "id": "c_lvl3",
                                                                    "name": "t1_c_lvl3",
                                                                    "parent_id": "t1_c_lvl2",
                                                                    "author": "mod_guardian",
                                                                    "distinguished": "moderator",
                                                                    "body": "Pinned to the community benchmark wiki.",
                                                                    "score": 28,
                                                                    "created_utc": 1763101800,
                                                                    "replies": {
                                                                        "kind": "Listing",
                                                                        "data": {
                                                                            "children": [
                                                                                {
                                                                                    "kind": "t1",
                                                                                    "data": {
                                                                                        "id": "c_lvl4",
                                                                                        "name": "t1_c_lvl4",
                                                                                        "parent_id": "t1_c_lvl3",
                                                                                        "author": "quant_enthusiast",
                                                                                        "body": "Tested on dual 3090s!",
                                                                                        "score": 19,
                                                                                        "created_utc": 1763102400,
                                                                                        "replies": "",
                                                                                    },
                                                                                },
                                                                                {
                                                                                    "kind": "more",
                                                                                    "data": {
                                                                                        "id": "more_deep",
                                                                                        "parent_id": "t1_c_lvl3",
                                                                                        "depth": 3,
                                                                                        "count": 2,
                                                                                        "children": [
                                                                                            "c_lvl4_more1",
                                                                                            "c_lvl5_more2",
                                                                                        ],
                                                                                    },
                                                                                },
                                                                            ]
                                                                        },
                                                                    },
                                                                },
                                                            }
                                                        ]
                                                    },
                                                },
                                            },
                                        }
                                    ]
                                },
                            },
                        },
                    },
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c_deleted",
                            "name": "t1_c_deleted",
                            "parent_id": "t3_1kvcache",
                            "author": "[deleted]",
                            "body": "[deleted]",
                            "score": -8,
                            "created_utc": 1763100700,
                            "replies": "",
                        },
                    },
                ]
            },
        },
    ]


def _sample_morechildren_payload() -> dict[str, Any]:
    return {
        "json": {
            "errors": [],
            "data": {
                "things": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c_lvl4_more1",
                            "name": "t1_c_lvl4_more1",
                            "parent_id": "t1_c_lvl3",
                            "author": "vllm_contributor",
                            "body": "Merged into upstream PR #9412.",
                            "score": 14,
                            "created_utc": 1763103000,
                        },
                    },
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c_lvl5_more2",
                            "name": "t1_c_lvl5_more2",
                            "parent_id": "t1_c_lvl4_more1",
                            "author": "llama_researcher",
                            "is_submitter": True,
                            "body": "Awesome, thanks for upstreaming!",
                            "score": 11,
                            "created_utc": 1763103600,
                        },
                    },
                ]
            },
        }
    }


class _MockRedditServer:
    """Thread-safe local loopback HTTP server exercising real SafeHttpClient and parsers."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.mode = "ok"
        self.delay_sec = 0.0
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                outer.requests.append(
                    {
                        "path": parsed.path,
                        "query": qs,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                if outer.delay_sec > 0:
                    time.sleep(outer.delay_sec)

                mode = outer.mode
                if mode == "401":
                    self._send_json(401, {"error": 401, "message": "Unauthorized"})
                    return
                if mode == "403":
                    self._send_json(403, {"error": 403, "message": "Forbidden"})
                    return
                if mode == "cloudflare":
                    self._send_html(
                        403,
                        "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
                        "<body>Checking your browser before accessing... Ray ID: 8abc</body></html>",
                        headers={"cf-mitigated": "challenge", "cf-ray": "8abc"},
                    )
                    return
                if mode == "404":
                    self._send_json(404, {"error": 404, "message": "Not Found"})
                    return
                if mode == "429":
                    self._send_json(429, {"error": 429, "message": "Too Many Requests"})
                    return
                if mode == "500":
                    self._send_json(500, {"error": 500, "message": "Internal Server Error"})
                    return
                if mode == "503":
                    self._send_json(503, {"error": 503, "message": "Service Unavailable"})
                    return
                if mode == "malformed_json":
                    self._send_raw(200, "application/json", b"{not valid json")
                    return
                if mode == "malformed_html_200":
                    self._send_html(200, "<html><body><div>broken html")
                    return
                if mode == "too_large":
                    payload = b'{"kind":"Listing","data":{"children":[' + (b" " * (8 * 1024 * 1024 + 1024)) + b"]}}"
                    self._send_raw(200, "application/json", payload)
                    return
                if mode == "empty_listing":
                    self._send_json(200, {"kind": "Listing", "data": {"after": None, "before": None, "children": []}})
                    return
                if mode == "empty_post":
                    self._send_json(
                        200,
                        [
                            {"kind": "Listing", "data": {"children": []}},
                            {"kind": "Listing", "data": {"children": []}},
                        ],
                    )
                    return

                if parsed.path.endswith("/api/morechildren.json"):
                    self._send_json(200, _sample_morechildren_payload())
                    return

                if parsed.path.endswith("/subreddits/mine/subscriber.json"):
                    cookie = self.headers.get("Cookie", "")
                    auth = self.headers.get("Authorization", "")
                    if "reddit_session=" not in cookie and not auth.startswith("Bearer "):
                        self._send_json(401, {"error": 401, "message": "Login required"})
                        return
                    self._send_json(
                        200,
                        {
                            "kind": "Listing",
                            "data": {
                                "children": [
                                    {
                                        "kind": "t5",
                                        "data": {
                                            "display_name": "MachineLearning",
                                            "display_name_prefixed": "r/MachineLearning",
                                            "title": "Machine Learning",
                                            "subscribers": 2900000,
                                        },
                                    }
                                ]
                            },
                        },
                    )
                    return

                if "/comments/" in parsed.path:
                    self._send_json(200, _sample_thread_payload())
                    return

                if parsed.path.endswith("/search.json"):
                    q = (qs.get("q") or [""])[0]
                    sub = "cpp" if "/r/cpp/" in parsed.path else "LocalLLaMA"
                    if q == "empty":
                        self._send_json(200, {"kind": "Listing", "data": {"after": None, "children": []}})
                    else:
                        self._send_json(200, _sample_listing_page1(sub=sub, after=""))
                    return

                if parsed.path in {"/hot.json", "/new.json", "/top.json"}:
                    cookie = self.headers.get("Cookie", "")
                    auth = self.headers.get("Authorization", "")
                    if "reddit_session=" not in cookie and not auth.startswith("Bearer "):
                        self._send_json(401, {"error": 401, "message": "Login required"})
                        return
                    self._send_json(200, _sample_listing_page1(sub="rust", after=""))
                    return

                after = (qs.get("after") or [""])[0]
                sub = "cpp" if "/r/cpp/" in parsed.path else "popular"
                if after == "t3_page2":
                    self._send_json(200, _sample_listing_page2(sub=sub))
                else:
                    self._send_json(200, _sample_listing_page1(sub=sub, after="t3_page2"))

            def _send_raw(
                self,
                status: int,
                content_type: str,
                raw: bytes,
                headers: dict[str, str] | None = None,
            ) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(raw)))
                    for k, v in (headers or {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(raw)
                except BrokenPipeError:
                    pass

            def _send_json(self, status: int, payload: Any) -> None:
                self._send_raw(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

            def _send_html(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
                self._send_raw(status, "text/html; charset=utf-8", body.encode("utf-8"), headers=headers)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _MockRedditServer:
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def test_manifest_matches_v1_and_v2_schema() -> None:
    manifest_raw = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))
    manifest = load_manifest(PLUGIN_DIR)

    assert manifest.schema_version == 2
    assert manifest.id == "plugin-reddit"
    assert manifest.kind == "workspace"
    assert manifest.domain == "text"
    assert manifest.entry == "plugin:create_plugin"
    assert "text.source.v1" in manifest.provides
    assert "text.comments.v1" in manifest.provides
    assert "reddit.auth.v1" in manifest.provides
    assert {"web.embed.v1", "web.cookies.v1", "web.fetch.v1", "web.extract.v1", "llm.chat.v1", "llm.translate.v1"}.issubset(
        set(manifest.consumes)
    )
    assert set(manifest_raw.keys()).issubset(set(schema["properties"].keys()))

    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(c) for c in manifest.provides + manifest.consumes)

    fields = {f["key"]: f for f in manifest_raw["config_schema"]}
    for required_key in (
        "chrome_cookie_source",
        "cookie_header",
        "oauth_token",
        "cookie_file_path",
        "prefer_chrome_fetch",
        "auto_translate",
        "target_lang",
        "request_timeout_sec",
        "subreddits",
        "user_agent",
    ):
        assert required_key in fields
    assert fields["chrome_cookie_source"]["type"] == "select"
    assert fields["chrome_cookie_source"]["options"] == ["auto", "plugin-chrome-web", "vault", "file", "disabled"]
    assert fields["cookie_header"]["type"] == "secret"
    assert fields["oauth_token"]["type"] == "secret"
    assert fields["request_timeout_sec"]["default"] == 15
    assert fields["subreddits"]["default"] == "LocalLLaMA,cpp,rust,programming"
    assert fields["auto_translate"]["type"] == "bool"


@pytest.mark.asyncio
async def test_lifecycle_and_health_check(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    plugin = module.create_plugin()
    assert isinstance(plugin, BasePlugin)

    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_secret("cookie_header", "reddit_session=super_secret_sess; token_v2=super_secret_jwt")
    ctx.set_secret("oauth_token", "oauth_bearer_secret_999")
    assert "cookie_header" not in ctx.get_config()
    assert "oauth_token" not in ctx.get_config()

    await invoke_lifecycle_hook(plugin.on_load, ctx)
    assert len(ctx.domains.text_sources()) == 1
    assert ctx.capabilities.has("text.source.v1")
    assert ctx.capabilities.has("text.comments.v1")
    assert ctx.capabilities.has("reddit.auth.v1")
    assert len(ctx.domains.workspace_tabs("text")) == 1

    health = await invoke_lifecycle_hook(plugin.on_health_check)
    assert health["ok"] is True
    assert health["plugin_id"] == "plugin-reddit"
    assert health["auth"]["authenticated"] is True
    health_dump = json.dumps(health)
    assert "super_secret_sess" not in health_dump
    assert "super_secret_jwt" not in health_dump
    assert "oauth_bearer_secret_999" not in health_dump
    tab_status = await plugin.handle_tab_action("status")
    assert tab_status["ok"] is True
    assert tab_status["result"]["authenticated"] is True

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
    assert ctx.domains.text_sources() == []
    assert not ctx.capabilities.has("text.source.v1")


@pytest.mark.asyncio
async def test_real_http_feed_article_comments_subscriptions_and_pagination(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    with _MockRedditServer() as srv:
        plugin = module.RedditPlugin(api_base_url=srv.base_url, allow_loopback_http=True)
        ctx = PluginContext(
            manifest["id"],
            plugin_dir=PLUGIN_DIR,
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "cfg",
            config_schema=manifest["config_schema"],
        )
        await plugin.on_load(ctx)

        # 1. Channels & Subscriptions CRUD + persistence
        chans_res = await ctx.capabilities.call("text.source.v1", {"action": "channels"})
        assert chans_res.ok is True
        chan_ids = [c["id"] for c in chans_res.data["channels"]]
        assert chan_ids[:3] == ["home", "popular", "r/LocalLLaMA"]
        assert chans_res.data["channels"][0]["requires_auth"] is True

        sub_res = await ctx.capabilities.call(
            "text.source.v1",
            {"action": "subscribe", "subreddit": "r/systems", "display_name": "r/systems", "default_sort": "top"},
        )
        assert sub_res.ok is True
        assert sub_res.data["subscribed"] == "systems"
        assert (tmp_path / "data" / "subscriptions.json").is_file()

        unsub_res = await ctx.capabilities.call("text.source.v1", {"action": "unsubscribe", "subreddit": "rust"})
        assert unsub_res.ok is True
        assert unsub_res.data["unsubscribed"] == "rust"

        # 2. Fetch feed + pagination boundaries
        src = ctx.domains.text_sources()[0]
        p1 = await src.fetch_feed("r/cpp", "", page=1)
        assert len(p1) == 1
        assert p1[0].id == "reddit:cpp:cpp26refl"
        assert p1[0].title == "[Proposal] C++26 Static Reflection & P2996"
        assert p1[0].score == 310
        assert p1[0].comment_count == 84

        p2 = await src.fetch_feed("r/cpp", "", page=2)
        assert len(p2) == 1
        assert p2[0].id == "reddit:cpp:cpp26p2"

        # End of pagination -> empty list without re-fetching page 1
        req_count_before_p3 = len(srv.requests)
        p3 = await src.fetch_feed("r/cpp", "", page=3)
        assert p3 == []
        assert len(srv.requests) == req_count_before_p3

        # Out-of-bounds page & empty listing return []
        assert await src.fetch_feed("r/cpp", "", page=0) == []
        bad_page_rpc = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "r/cpp", "page": 0})
        assert bad_page_rpc.ok is False
        assert bad_page_rpc.data["error_code"] == "invalid_argument"

        srv.mode = "empty_listing"
        empty_rpc = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "r/cpp"})
        assert empty_rpc.ok is True
        assert empty_rpc.data["items"] == []
        assert empty_rpc.data["has_more"] is False
        srv.mode = "ok"

        # 3. Search
        search_res = await ctx.capabilities.call(
            "text.source.v1", {"action": "search", "subreddit": "r/cpp", "query": "reflection"}
        )
        assert search_res.ok is True
        assert len(search_res.data["items"]) == 1
        bad_search = await ctx.capabilities.call("text.source.v1", {"action": "search", "query": ""})
        assert bad_search.ok is False
        assert bad_search.data["error_code"] == "invalid_argument"

        # 4. Fetch article -> markdown headings, formulas, quotes, code blocks, 5-level comment tree
        art_res = await ctx.capabilities.call(
            "text.source.v1",
            {"action": "fetch_article", "id": "https://www.reddit.com/r/LocalLLaMA/comments/1kvcache/"},
        )
        assert art_res.ok is True
        article = art_res.data["article"]
        kinds = [p["kind"] for p in article["paragraphs"]]
        assert kinds == ["heading", "paragraph", "formula", "quote", "code"]
        assert article["paragraphs"][2]["original"].startswith("\\text{Speedup}")
        assert article["paragraphs"][4]["language"] == "cpp"
        assert "launch_fp8_attention" in article["paragraphs"][4]["original"]

        comments = article["comments"]
        assert len(comments) == 2
        assert comments[0]["role_badge"] == "Kernel Dev"
        assert comments[1]["collapsed"] is True  # [deleted] & score <= -5
        c2 = comments[0]["replies"][0]
        assert c2["role_badge"] == "OP"
        assert c2["body"] == "We use YaRN with dynamic scaling > 64k tokens."
        c3 = c2["replies"][0]
        assert c3["role_badge"] == "MOD"
        # Auto-expanded morechildren stitched into level 4 & 5!
        assert len(c3["replies"]) == 2
        assert c3["replies"][1]["id"] == "c_lvl4_more1"
        assert c3["replies"][1]["replies"][0]["id"] == "c_lvl5_more2"
        assert c3["replies"][1]["replies"][0]["role_badge"] == "OP"

        # 5. Explicit text.comments.v1 fetch_comments + expand_more
        cm_res = await ctx.capabilities.call(
            "text.comments.v1", {"action": "fetch_comments", "article_id": "reddit:LocalLLaMA:1kvcache", "expand_more": False}
        )
        assert cm_res.ok is True
        assert len(cm_res.data["more_stubs"]) == 1
        exp_res = await ctx.capabilities.call(
            "text.comments.v1",
            {
                "action": "fetch_more_comments",
                "link_id": cm_res.data["link_fullname"],
                "post_author": "llama_researcher",
                "children": ["c_lvl4_more1", "c_lvl5_more2"],
                "comments": cm_res.data["comments"],
            },
        )
        assert exp_res.ok is True
        assert exp_res.data["attached"] == 2

        await plugin.on_unload()
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_auth_priority_fail_closed_and_secrets_protection(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    # Create Netscape cookie file (with expired + other domain + valid entry)
    future_ts = int(time.time()) + 86400
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        f"#HttpOnly_.reddit.com\tTRUE\t/\tTRUE\t{future_ts}\treddit_session\tfile_sess_token\n"
        f".reddit.com\tTRUE\t/\tTRUE\t{future_ts}\tloid\tfile_loid_001\n"
        f".reddit.com\tTRUE\t/\tTRUE\t1000\ttoken_v2\texpired_tok\n"
        f".other.com\tTRUE\t/\tTRUE\t{future_ts}\treddit_session\twrong_domain\n",
        encoding="utf-8",
    )

    with _MockRedditServer() as srv:
        plugin = module.RedditPlugin(api_base_url=srv.base_url, allow_loopback_http=True)
        ctx = PluginContext(
            manifest["id"],
            plugin_dir=PLUGIN_DIR,
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "cfg",
            config_schema=manifest["config_schema"],
        )
        await plugin.on_load(ctx)

        # 1. Guest accessing `home` MUST fail closed with `auth_required` (NO fake articles/items)
        home_guest = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "home"})
        assert home_guest.ok is False
        assert home_guest.data["error_code"] == "auth_required"
        assert home_guest.data["items"] == []
        assert await plugin.text_source.fetch_feed("home") == []
        assert plugin.text_source.last_error is not None
        assert plugin.text_source.last_error["error_code"] == "auth_required"

        # 2. sync_subscriptions without auth fails closed
        sync_sub_guest = await ctx.capabilities.call("text.source.v1", {"action": "sync_subscriptions"})
        assert sync_sub_guest.ok is False
        assert sync_sub_guest.data["error_code"] == "auth_required"

        # 3. sync_cookies without Chrome bridge fails closed (never fakes login success)
        sync_no_chrome = await ctx.capabilities.call("reddit.auth.v1", {"action": "sync_cookies"})
        assert sync_no_chrome.ok is False
        assert sync_no_chrome.data["error_code"] == "auth_required"

        # 4. Login without web.embed.v1 fails closed
        login_no_chrome = await ctx.capabilities.call("reddit.auth.v1", {"action": "login"})
        assert login_no_chrome.ok is False
        assert login_no_chrome.data["error_code"] == "not_configured"

        # 5. File cookie source works & respects domain/expiry
        ctx.set_config("chrome_cookie_source", "file")
        ctx.set_config("cookie_file_path", str(cookie_file))
        st_file = await ctx.capabilities.call("reddit.auth.v1", {"action": "status"})
        assert st_file.ok is True
        assert st_file.data["authenticated"] is True
        assert st_file.data["source"] == "file"
        assert st_file.data["has_reddit_session"] is True
        assert st_file.data["has_token_v2"] is False  # expired filtered out
        assert "file_sess_token" not in json.dumps(st_file.data)

        # 6. Register web.cookies.v1 returning ONLY guest `loid` -> sync_cookies fails closed!
        #    AND in auto mode it MUST NOT overwrite SecretVault's real session!
        chrome_mode = "guest_only"

        async def _fake_cookies(req: dict[str, Any]) -> CapabilityResult:
            if chrome_mode == "guest_only":
                return CapabilityResult.ok({"cookies": [{"name": "loid", "value": "guest_loid", "domain": ".reddit.com"}]})
            return CapabilityResult.ok(
                {
                    "cookies": [
                        {
                            "name": "reddit_session",
                            "value": "chrome_live_secret_888",
                            "domain": ".reddit.com",
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                            "expirationDate": float(future_ts),
                        },
                        {
                            "name": "token_v2",
                            "value": "chrome_jwt_secret_999",
                            "domain": ".reddit.com",
                            "path": "/",
                            "secure": True,
                        },
                    ]
                }
            )

        async def _fake_embed(req: dict[str, Any]) -> CapabilityResult:
            assert req["url"] == "https://www.reddit.com/login"
            return CapabilityResult.ok({"session_id": "reddit-login-1"})

        ctx.capabilities.register("web.cookies.v1", _fake_cookies, plugin_id="plugin-chrome-web")
        ctx.capabilities.register("web.embed.v1", _fake_embed, plugin_id="plugin-chrome-web")

        sync_guest = await ctx.capabilities.call("reddit.auth.v1", {"action": "sync_cookies"})
        assert sync_guest.ok is False
        assert sync_guest.data["error_code"] == "auth_required"

        # Save manual cookie_header to Vault -> auto mode keeps Vault even when Chrome only has `loid`
        ctx.set_config("chrome_cookie_source", "auto")
        save_res = await ctx.capabilities.call(
            "reddit.auth.v1", {"action": "set_cookie_header", "cookie_header": "reddit_session=vault_sess_777"}
        )
        assert save_res.ok is True
        st_auto_vault = await ctx.capabilities.call("reddit.auth.v1", {"action": "status"})
        assert st_auto_vault.data["source"] == "vault"
        assert ctx.get_secret("cookie_header") == "reddit_session=vault_sess_777"

        # Now Chrome logs in -> sync_cookies & home & sync_subscriptions succeed!
        chrome_mode = "authed"
        sync_ok = await ctx.capabilities.call("reddit.auth.v1", {"action": "sync_cookies"})
        assert sync_ok.ok is True
        assert sync_ok.data["synced"] is True
        assert "chrome_live_secret_888" not in json.dumps(sync_ok.data)

        home_ok = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "home"})
        assert home_ok.ok is True
        assert len(home_ok.data["items"]) == 1

        sync_subs = await ctx.capabilities.call("text.source.v1", {"action": "sync_subscriptions"})
        assert sync_subs.ok is True
        assert sync_subs.data["merged"] == 1

        login_ok = await ctx.capabilities.call("reddit.auth.v1", {"action": "login"})
        assert login_ok.ok is True
        assert login_ok.data["launched"] is True

        # Logout clears vault secrets
        logout_res = await ctx.capabilities.call("reddit.auth.v1", {"action": "logout"})
        assert logout_res.ok is True
        assert ctx.get_secret("cookie_header") is None

        await plugin.on_unload()
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_upstream_errors_cloudflare_timeout_too_large_and_security(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    with _MockRedditServer() as srv:
        plugin = module.RedditPlugin(api_base_url=srv.base_url, allow_loopback_http=True)
        ctx = PluginContext(
            manifest["id"],
            plugin_dir=PLUGIN_DIR,
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "cfg",
            config_schema=manifest["config_schema"],
        )
        await plugin.on_load(ctx)

        # 1. Upstream HTTP error codes
        for mode, expected_code in (
            ("401", "auth_required"),
            ("403", "auth_required"),
            ("cloudflare", "cloudflare_blocked"),
            ("404", "not_found"),
            ("429", "rate_limited"),
            ("500", "service_offline"),
            ("503", "service_offline"),
            ("malformed_json", "parse_error"),
            ("malformed_html_200", "parse_error"),
            ("empty_post", "not_found"),
        ):
            srv.mode = mode
            if mode == "empty_post":
                res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "1kvcache"})
            else:
                res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "r/cpp"})
            assert res.ok is False, f"expected failure for mode={mode}"
            assert res.data["error_code"] == expected_code, f"mode={mode} got {res.data}"

        # TextSource.fetch_article raises RedditPluginError fail-closed (no fake article)
        srv.mode = "404"
        with pytest.raises(reddit_security.RedditPluginError) as exc_info:
            await plugin.text_source.fetch_article("1kvcache")
        assert exc_info.value.error_code == "not_found"

        # 2. Oversized body (> 8 MiB)
        srv.mode = "too_large"
        res_large = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "r/cpp"})
        assert res_large.ok is False
        assert res_large.data["error_code"] == "too_large"

        # 3. Timeout
        srv.mode = "ok"
        srv.delay_sec = 0.5
        ctx.set_config("request_timeout_sec", 0.2)
        res_to = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "r/cpp"})
        assert res_to.ok is False
        assert res_to.data["error_code"] == "timeout"
        srv.delay_sec = 0.0
        ctx.set_config("request_timeout_sec", 15)

        # 4. Path traversal & symlink protection on cookie_file_path
        ctx.set_config("chrome_cookie_source", "file")
        ctx.set_config("cookie_file_path", "../../etc/passwd")
        trav_res = await ctx.capabilities.call("reddit.auth.v1", {"action": "status"})
        assert trav_res.ok is False
        assert trav_res.data["error_code"] == "path_safety"

        real_file = tmp_path / "real.txt"
        real_file.write_text("", encoding="utf-8")
        sym_file = tmp_path / "sym_cookies.txt"
        sym_file.symlink_to(real_file)
        ctx.set_config("cookie_file_path", str(sym_file))
        sym_res = await ctx.capabilities.call("reddit.auth.v1", {"action": "status"})
        assert sym_res.ok is False
        assert sym_res.data["error_code"] == "path_safety"

        await plugin.on_unload()
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_chrome_fetch_and_html_extract_fallback_and_malformed_html(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    with _MockRedditServer() as srv:
        srv.mode = "cloudflare"
        plugin = module.RedditPlugin(api_base_url=srv.base_url, allow_loopback_http=True)
        ctx = PluginContext(
            manifest["id"],
            plugin_dir=PLUGIN_DIR,
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "cfg",
            config_schema=manifest["config_schema"],
        )

        fetch_calls = 0
        extract_mode = "valid_listing"

        async def _fake_web_fetch(req: dict[str, Any]) -> CapabilityResult:
            nonlocal fetch_calls
            fetch_calls += 1
            if fetch_calls == 1:
                assert req["open_tab_if_missing"] is True
                assert req["tab_url"].startswith("http://127.0.0.1:")
                return CapabilityResult.ok(
                    {
                        "status": 200,
                        "url": req["url"],
                        "headers": {"content-type": "application/json"},
                        "body": json.dumps(_sample_listing_page1("rust", after="")),
                    }
                )
            return CapabilityResult.fail("Cloudflare fetch blocked", error_code="service_offline")

        async def _fake_web_extract(req: dict[str, Any]) -> CapabilityResult:
            if extract_mode == "valid_listing":
                return CapabilityResult.ok(
                    {
                        "url": req["url"],
                        "html": (
                            '<html><body><div class="thing link" data-fullname="t3_html1" '
                            'data-subreddit="rust" data-author="ferris" data-permalink="/r/rust/comments/html1/title/">'
                            '<div class="score unvoted">188</div>'
                            '<a class="title" href="/r/rust/comments/html1/title/">Rust Async Guide</a>'
                            '<img src="https://example.com/thumb.png"><br>'
                            '<div class="md"><p>Extracted via Chrome DOM.</p></div>'
                            "</div></body></html>"
                        ),
                    }
                )
            if extract_mode == "valid_post":
                return CapabilityResult.ok(
                    {
                        "url": req["url"],
                        "html": (
                            '<html><body>'
                            '<div class="thing link" data-fullname="t3_html1" data-subreddit="rust" data-author="ferris">'
                            '<div class="score unvoted">188</div>'
                            '<a class="title">Rust Async Guide</a>'
                            '<div class="md"><p>Line one</p><p>Line two</p></div>'
                            "</div>"
                            '<div class="thing comment" data-fullname="t1_hc1" data-author="alice">'
                            '<div class="score unvoted">12</div>'
                            '<div class="md"><p>Parent reply</p></div>'
                            '<div class="child"><div class="sitetable">'
                            '<div class="thing comment" data-fullname="t1_hc2" data-author="bob">'
                            '<div class="score unvoted">7</div>'
                            '<div class="md"><p>Nested reply</p></div>'
                            "</div></div></div></div>"
                            "</body></html>"
                        ),
                    }
                )
            if extract_mode == "unclosed_thing":
                return CapabilityResult.ok(
                    {
                        "url": req["url"],
                        "html": '<html><body><div class="thing link" data-fullname="t3_bad"><a class="title">Truncated',
                    }
                )
            if extract_mode == "unclosed_script":
                return CapabilityResult.ok(
                    {
                        "url": req["url"],
                        "html": '<html><body><div class="thing link" data-fullname="t3_bad"><a class="title">Ok</a></div><script>evil(',
                    }
                )
            return CapabilityResult.ok({"url": req["url"], "html": "<html><body><h1>502 Bad Gateway</h1></body></html>"})

        ctx.capabilities.register("web.fetch.v1", _fake_web_fetch, plugin_id="plugin-chrome-web")
        ctx.capabilities.register("web.extract.v1", _fake_web_extract, plugin_id="plugin-chrome-web")
        await plugin.on_load(ctx)

        # 1. web.fetch.v1 succeeds on Cloudflare 403
        res_fetch = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "r/rust"})
        assert res_fetch.ok is True
        assert res_fetch.data["transport"] == "web.fetch.v1"
        assert res_fetch.data["items"][0]["id"] == "reddit:rust:cpp26refl"

        # 2. web.fetch.v1 fails -> falls back to web.extract.v1 HTML (with void <img>/<br> tags!)
        res_html_feed = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "r/rust"})
        assert res_html_feed.ok is True
        assert res_html_feed.data["transport"] == "web.extract.v1:html"
        assert res_html_feed.data["items"][0]["title"] == "Rust Async Guide"
        assert res_html_feed.data["items"][0]["score"] == 188
        assert res_html_feed.data["after"] == "t3_html1"
        assert res_html_feed.data["has_more"] is True

        # 3. web.extract.v1 post HTML with nested comment tree!
        extract_mode = "valid_post"
        res_html_post = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "r/rust/html1"})
        assert res_html_post.ok is True
        post_comments = res_html_post.data["article"]["comments"]
        assert len(post_comments) == 1
        assert post_comments[0]["id"] == "hc1"
        assert post_comments[0]["replies"][0]["id"] == "hc2"

        # 4. Malformed HTML modes MUST fail closed with parse_error
        for bad_mode in ("unclosed_thing", "unclosed_script", "no_reddit_things"):
            extract_mode = bad_mode
            bad_res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_feed", "channel_id": "r/rust"})
            assert bad_res.ok is False, f"expected parse_error for {bad_mode}"
            assert bad_res.data["error_code"] == "parse_error"

        await plugin.on_unload()
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_redirect_ssrf_and_ua_normalization() -> None:
    assert "omnidesk.plugin.reddit" in reddit_security.normalize_user_agent("")
    assert "omnidesk.plugin.reddit" in reddit_security.normalize_user_agent("curl/8.5.0")
    assert "omnidesk.plugin.reddit" in reddit_security.normalize_user_agent("python-requests/2.31")
    assert reddit_security.normalize_user_agent("linux:custom.bot:v1.0") == "linux:custom.bot:v1.0"

    # Cross-host redirect strips Cookie & Authorization while Reddit subdomain keeps Cookie
    http_client = reddit_http.RedditHttpClient(allow_loopback_http=False)
    seen_specs: list[reddit_http.HttpRequestSpec] = []

    def _mock_transport(spec: reddit_http.HttpRequestSpec) -> reddit_http.HttpResponse:
        seen_specs.append(spec)
        if len(seen_specs) == 1:
            return reddit_http.HttpResponse(
                ok=True,
                status_code=302,
                headers={"location": "https://www.reddit.com/r/cpp/hot.json?raw_json=1"},
            )
        if len(seen_specs) == 2:
            return reddit_http.HttpResponse(
                ok=True,
                status_code=302,
                headers={"location": "https://cdn.external-tracker.example.org/payload.json"},
            )
        return reddit_http.HttpResponse(
            ok=True,
            status_code=200,
            body='{"kind":"Listing","data":{"children":[]}}',
        )

    http_client.set_transport_override(_mock_transport)
    resp = await http_client.perform_direct(
        reddit_http.HttpRequestSpec(
            url="https://old.reddit.com/r/cpp/hot.json?raw_json=1",
            headers={
                "Cookie": "reddit_session=secret_sess_123; token_v2=jwt_456",
                "Authorization": "Bearer secret_oauth_789",
            },
        )
    )
    assert resp.ok is True
    assert len(seen_specs) == 3
    assert "Cookie" in seen_specs[1].headers
    assert "Authorization" not in seen_specs[1].headers  # Stripped across non-same-origin redirect
    assert "Cookie" not in seen_specs[2].headers
    assert "Authorization" not in seen_specs[2].headers

    # Redirect loop cap (> 5 hops)
    http_client.set_transport_override(
        lambda spec: reddit_http.HttpResponse(
            ok=True, status_code=302, headers={"location": "https://www.reddit.com/loop"}
        )
    )
    loop_resp = await http_client.perform_direct(
        reddit_http.HttpRequestSpec(url="https://old.reddit.com/r/cpp/hot.json")
    )
    assert loop_resp.ok is False
    assert loop_resp.error_code == "blocked_redirect"

    # Non-HTTPS & SSRF URLs rejected fail-closed
    for bad_url in (
        "http://old.reddit.com/r/cpp/hot.json",
        "file:///etc/passwd",
        "https://127.0.0.1/admin",
        "https://169.254.169.254/latest/meta-data",
        "https://user:pass@www.reddit.com/r/cpp",
    ):
        bad_resp = await http_client.perform_direct(reddit_http.HttpRequestSpec(url=bad_url))
        assert bad_resp.ok is False
        assert bad_resp.error_code in {"ssrf", "invalid_argument"}


@pytest.mark.asyncio
async def test_bilingual_translation_preserves_code_formula_and_companion_llm(tmp_path: Path) -> None:
    module = _load_plugin_module()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    with _MockRedditServer() as srv:
        plugin = module.RedditPlugin(api_base_url=srv.base_url, allow_loopback_http=True)
        ctx = PluginContext(
            manifest["id"],
            plugin_dir=PLUGIN_DIR,
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "cfg",
            config_schema=manifest["config_schema"],
        )

        translated_inputs: list[str] = []

        async def _fake_translate(req: dict[str, Any]) -> CapabilityResult:
            texts = req.get("texts") or ([req["text"]] if "text" in req else [])
            translated_inputs.extend(texts)
            if len(texts) == 1:
                return CapabilityResult.ok({"translation": f"[ZH] {texts[0]}", "reply": f"[ZH] {texts[0]}"})
            return CapabilityResult.ok({"translations": [f"[ZH] {t}" for t in texts]})

        async def _fake_chat(req: dict[str, Any]) -> CapabilityResult:
            assert "launch_fp8_attention" in req["prompt"]
            assert "\\text{Speedup}" in req["prompt"]
            return CapabilityResult.ok({"reply": "核心结论：FP8 KV Cache 显著提升长上下文吞吐。"})

        ctx.capabilities.register("llm.translate.v1", _fake_translate, plugin_id="plugin-magpie-agent")
        ctx.capabilities.register("llm.chat.v1", _fake_chat, plugin_id="plugin-magpie-agent")
        ctx.set_config("auto_translate", True)
        await plugin.on_load(ctx)

        art_res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "1kvcache"})
        assert art_res.ok is True
        assert art_res.data["translated"] is True
        article = art_res.data["article"]
        assert article["title_zh"].startswith("[ZH] ")

        paras = article["paragraphs"]
        assert paras[0]["translated"].startswith("[ZH] ")  # heading
        assert paras[1]["translated"].startswith("[ZH] ")  # paragraph
        assert paras[2]["kind"] == "formula" and paras[2]["translated"] == ""  # formula skipped!
        assert paras[3]["translated"].startswith("[ZH] ")  # quote
        assert paras[4]["kind"] == "code" and paras[4]["translated"] == ""  # code skipped!
        assert all("launch_fp8_attention" not in t and "\\text{Speedup}" not in t for t in translated_inputs)

        # Comments recursively translated too
        assert article["comments"][0]["translated_body"].startswith("[ZH] ")
        assert article["comments"][0]["replies"][0]["translated_body"].startswith("[ZH] ")

        # Companion LLM summarize & ask
        sum_res = await ctx.capabilities.call("text.source.v1", {"action": "summarize", "article": article})
        assert sum_res.ok is True
        assert "FP8 KV Cache" in sum_res.data["reply"]
        assert sum_res.data["code_blocks"] == 1
        assert sum_res.data["formula_blocks"] == 1

        # Markdown indented code + \[...\] formula + single-line $...$ + empty morechildren
        extra_paras = reddit_parser.parse_markdown_to_paragraphs(
            "\\[\nE = mc^2\n\\]\n\n$a^2 + b^2 = c^2$\n\n    int main() { return 0; }\n"
        )
        assert [p.kind for p in extra_paras] == ["formula", "formula", "code"]
        assert "int main()" in extra_paras[2].original

        empty_more_comments: list[Any] = []
        assert reddit_parser.attach_more_children(empty_more_comments, "t3_x", "op", '{"json":{"errors":[],"data":{"things":[]}}}') == 0
        assert empty_more_comments == []

        # shreddit HTML fallback + Chromium expires_utc cookie parsing
        shreddit_html = (
            '<html><body>'
            '<shreddit-post id="t3_sh1" subreddit-name="LocalLLaMA" author="dev1" post-title="Shreddit Test" score="55" permalink="/r/LocalLLaMA/comments/sh1/test/">'
            '<div class="md"><p>Post body</p></div>'
            "</shreddit-post>"
            '<shreddit-comment thingid="t1_sc1" author="commenter1" score="9">'
            '<div class="md"><p>Top comment</p></div>'
            "</shreddit-comment>"
            "</body></html>"
        )
        sh_res = reddit_parser.parse_post_html(shreddit_html)
        assert sh_res.article.title == "Shreddit Test"
        assert len(sh_res.article.comments) == 1
        assert sh_res.article.comments[0].body == "Top comment"

        past_chrome_us = int((time.time() - 3600 + 11644473600) * 1_000_000)
        future_chrome_us = int((time.time() + 3600 + 11644473600) * 1_000_000)
        parsed_c = reddit_auth.parse_json_cookies(
            [
                {"name": "reddit_session", "value": "live_v", "domain": ".reddit.com", "expires_utc": future_chrome_us, "secure": True},
                {"name": "token_v2", "value": "expired_v", "domain": ".reddit.com", "expires_utc": past_chrome_us, "secure": True},
            ]
        )
        built_hdr = reddit_auth.build_cookie_header(parsed_c, "https://www.reddit.com/")
        assert built_hdr == "reddit_session=live_v"

        ask_res = await ctx.capabilities.call(
            "text.source.v1", {"action": "ask", "id": "1kvcache", "question": "RoPE 如何扩展？"}
        )
        assert ask_res.ok is True
        assert ask_res.data["mode"] == "ask"

        # Graceful degradation when llm.translate.v1 is absent
        ctx.capabilities.unregister("llm.translate.v1", plugin_id="plugin-magpie-agent")
        deg_res = await ctx.capabilities.call("text.source.v1", {"action": "fetch_article", "id": "1kvcache"})
        assert deg_res.ok is True
        assert deg_res.data["translation_degraded"] is True
        assert deg_res.data["degraded_reason"] == "llm.translate.v1_unavailable"

        # Companion fails closed when llm.chat.v1 is absent
        ctx.capabilities.unregister("llm.chat.v1", plugin_id="plugin-magpie-agent")
        no_llm = await ctx.capabilities.call("text.source.v1", {"action": "summarize", "id": "1kvcache"})
        assert no_llm.ok is False
        assert no_llm.data["error_code"] == "service_offline"

        await plugin.on_unload()
        await ctx.acleanup()
