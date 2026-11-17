"""Comprehensive offline contract, HTML/JSON parsing, auth, HTTP security, translation, and companion tests."""

from __future__ import annotations

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


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_zhihu_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_ctx(tmp_path: Path, *, manifest_data: dict[str, Any] | None = None) -> PluginContext:
    manifest = manifest_data or json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    return PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )


class _ExternalLandingServer:
    """Secondary loopback server on a distinct port to verify cross-origin header stripping."""

    def __init__(self) -> None:
        self.received_headers: list[dict[str, str]] = []
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.received_headers.append({k.lower(): v for k, v in self.headers.items()})
                body = b"landing-ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _ExternalLandingServer:
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


class _MockZhihuServer:
    """Thread-safe local loopback HTTP server simulating Zhihu web/API responses and failures."""

    def __init__(self, *, external_landing_url: str = "") -> None:
        self.mode = "ok"
        self.delay_sec = 0.0
        self.external_landing_url = external_landing_url
        self.requests: list[dict[str, Any]] = []
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                outer.requests.append({"path": parsed.path, "query": qs, "headers": hdrs})

                if outer.delay_sec > 0:
                    time.sleep(outer.delay_sec)

                if parsed.path == "/signin":
                    self._send_html(200, "<html><head><title>登录知乎</title></head><body><form>登录知乎</form></body></html>")
                    return

                mode = outer.mode
                if mode == "timeout":
                    time.sleep(1.2)
                    self._send_text(200, "late")
                    return
                if mode == "401":
                    self._send_json(401, {"error": {"code": 401, "message": "ERR_LOGIN_REQUIRED"}})
                    return
                if mode == "signin_redirect":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{outer.port}/signin")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if mode == "cloudflare":
                    self._send_html(
                        403,
                        "<!doctype html><html><head><title>Just a moment...</title></head>"
                        "<body>Checking your browser before accessing /cdn-cgi/challenge-platform/</body></html>",
                        extra_headers={"cf-mitigated": "challenge", "cf-ray": "abc123"},
                    )
                    return
                if mode == "40362":
                    self._send_json(403, {"error": {"code": 40362, "message": "x-zse-96 signature required"}})
                    return
                if mode == "429":
                    self._send_json(429, {"error": {"code": 429, "message": "Too many requests"}})
                    return
                if mode == "404":
                    self._send_json(404, {"error": {"code": 404, "message": "Not found"}})
                    return
                if mode == "500":
                    self._send_json(500, {"error": "Internal server error"})
                    return
                if mode == "502":
                    self._send_text(502, "Bad Gateway")
                    return
                if mode == "malformed_html":
                    self._send_html(
                        200,
                        '<html><body><script id="js-initialData">{broken_json</script><div>incomplete<p>no rich marker',
                    )
                    return
                if mode == "malformed_json":
                    self._send_text(200, "<<<not json at all>>>", content_type="application/json")
                    return
                if mode == "oversized":
                    big = b"x" * (8 * 1024 * 1024 + 4096)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(big)))
                    self.end_headers()
                    try:
                        self.wfile.write(big)
                    except BrokenPipeError:
                        pass
                    return

                if parsed.path == "/redirect-external" and outer.external_landing_url:
                    self.send_response(302)
                    self.send_header("Location", f"{outer.external_landing_url}/landing")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if parsed.path == "/redirect-loop":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{outer.port}/redirect-loop")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if parsed.path == "/api/v3/feed/topstory/hot-lists/total":
                    if mode == "empty":
                        self._send_json(200, {"data": []})
                        return
                    if mode == "hot_html_fallback":
                        self._send_json(500, {"error": "use html"})
                        return
                    self._send_json(
                        200,
                        {
                            "data": [
                                {
                                    "detail_text": "2560 万热度",
                                    "target": {
                                        "id": 556677,
                                        "type": "question",
                                        "title": "为什么微内核插件架构适合桌面伴侣？",
                                        "excerpt": "零 ABI 隔离优势讨论",
                                        "answer_count": 88,
                                        "comment_count": 12,
                                    },
                                }
                            ]
                        },
                    )
                    return

                if parsed.path == "/hot":
                    self._send_html(
                        200,
                        """<html><body><script id="js-initialData">
                        {"initialState":{"topstory":{"hotList":[{"target":{"titleArea":{"text":"HTML 回退热榜问题"},"excerptArea":{"text":"热榜摘要"},"metricsArea":{"text":"120 万热度"},"link":{"url":"https://www.zhihu.com/question/998877"}}}]}}}
                        </script></body></html>""",
                    )
                    return

                if parsed.path == "/api/v3/feed/topstory/recommend":
                    if mode == "recommend_403":
                        self._send_json(403, {"error": {"code": 40362, "message": "x-zse-96 signature required"}})
                        return
                    self._send_json(
                        200,
                        {
                            "data": [
                                {
                                    "type": "feed",
                                    "target": {
                                        "id": 778899,
                                        "type": "article",
                                        "title": "从零推导注意力机制中的缩放因子",
                                        "excerpt": "为什么除以根号 d_k",
                                        "author": {"name": "机器学习研究员"},
                                        "voteup_count": 512,
                                        "comment_count": 33,
                                        "updated": 1700020000,
                                    },
                                }
                            ]
                        },
                    )
                    return

                if parsed.path == "/api/v3/moments":
                    self._send_json(
                        200,
                        {
                            "data": [
                                {
                                    "action_text": "赞同了回答",
                                    "target": {
                                        "id": 889900,
                                        "type": "answer",
                                        "question": {"id": 556677, "title": "微内核问答"},
                                        "author": {"name": "系统架构师"},
                                        "excerpt": "子进程故障隔离",
                                        "voteup_count": 100,
                                        "comment_count": 5,
                                    },
                                }
                            ]
                        },
                    )
                    return

                if parsed.path == "/api/v4/people/self/collections":
                    self._send_json(200, {"data": [{"id": 301, "title": "深度学习精读", "item_count": 5}]})
                    return

                if parsed.path == "/api/v4/collections/301/items":
                    self._send_json(
                        200,
                        {
                            "data": [
                                {
                                    "content": {
                                        "id": 778899,
                                        "type": "article",
                                        "title": "从零推导注意力机制中的缩放因子",
                                        "excerpt": "收藏夹文章摘要",
                                        "author": {"name": "机器学习研究员"},
                                        "voteup_count": 512,
                                        "comment_count": 33,
                                    }
                                }
                            ]
                        },
                    )
                    return

                if parsed.path == "/api/v4/search_v3":
                    if mode == "empty":
                        self._send_json(200, {"data": []})
                        return
                    q_val = (qs.get("q") or [""])[0]
                    offset = int((qs.get("offset") or ["0"])[0])
                    if offset >= 20:
                        self._send_json(200, {"data": []})
                        return
                    self._send_json(
                        200,
                        {
                            "data": [
                                {
                                    "object": {
                                        "id": 778899,
                                        "type": "article",
                                        "title": f"搜索命中: {q_val}",
                                        "excerpt": "缩放点积注意力",
                                        "author": {"name": "研究员"},
                                        "voteup_count": 88,
                                    }
                                }
                            ]
                        },
                    )
                    return

                if parsed.path == "/p/778899":
                    self._send_html(
                        200,
                        """<html><body><script id="js-initialData">
                        {"initialState":{"entities":{"articles":{"778899":{"id":778899,"title":"从零推导注意力机制中的缩放因子","author":{"name":"机器学习研究员"},"voteupCount":512,"commentCount":2,"updated":1700020000,"content":"<p>缩放点积注意力定义如下：</p><p><img data-formula=\\"\\\\text{Attention}(Q,K,V) = \\\\text{softmax}\\\\left(\\\\frac{QK^T}{\\\\sqrt{d_k}}\\\\right)V\\"/></p><pre><code class=\\"language-python\\">def scale(q, k):\\n    return q @ k.T</code></pre>"}}}}}
                        </script></body></html>""",
                    )
                    return

                if parsed.path == "/p/990011":
                    self._send_html(
                        200,
                        """<html><body><script id="js-initialData">
                        {"initialState":{"entities":{"articles":{"990011":{"id":990011,"title":"Attention Is All You Need Notes","author":{"name":"Researcher"},"voteupCount":210,"updated":1700020000,"content":"<h2>Overview</h2><p>Scaled dot-product attention stabilizes variance.</p><p><img data-formula=\\"d_k = 64\\"/></p><pre><code class=\\"language-python\\">def attn(x):\\n    return x</code></pre><blockquote>Keep math formulas untouched.</blockquote>"}}}}}
                        </script></body></html>""",
                    )
                    return

                if parsed.path == "/api/v4/answers/112233":
                    self._send_json(
                        200,
                        {
                            "id": 112233,
                            "question": {"id": 4455, "title": "API 兜底回答标题"},
                            "author": {"name": "答主甲"},
                            "voteup_count": 77,
                            "created_time": 1700000000,
                            "content": "<p>通过 API v4 兜底解析正文。</p>",
                        },
                    )
                    return

                if parsed.path == "/question/4455/answer/112233":
                    self._send_json(404, {"error": "html missing"})
                    return

                if parsed.path == "/api/v4/comment_v5/articles/778899/root_comment":
                    offset = int((qs.get("offset") or ["0"])[0])
                    if offset >= 20:
                        self._send_json(200, {"paging": {"is_end": True, "totals": 1}, "data": []})
                        return
                    self._send_json(
                        200,
                        {
                            "paging": {"is_end": False, "next": "https://www.zhihu.com/api/v4/comment_v5/articles/778899/root_comment?offset=20", "totals": 2},
                            "data": [
                                {
                                    "id": "c500",
                                    "author": {"name": "机器学习研究员"},
                                    "author_tag": [{"text": "作者"}],
                                    "content": "欢迎讨论推导细节",
                                    "vote_count": 42,
                                    "created_time": 1700021000,
                                    "child_comment_count": 1,
                                }
                            ],
                        },
                    )
                    return

                if parsed.path == "/api/v4/comment_v5/comment/c500/child_comment":
                    self._send_json(
                        200,
                        {
                            "paging": {"is_end": True, "totals": 1},
                            "data": [
                                {
                                    "id": "c501",
                                    "author": {"name": "博士生A"},
                                    "reply_to_author": {"name": "机器学习研究员"},
                                    "content": "方差分析非常到位",
                                    "vote_count": 9,
                                    "created_time": 1700021500,
                                }
                            ],
                        },
                    )
                    return

                if parsed.path == "/api/v4/comment_v5/articles/990011/root_comment":
                    self._send_json(
                        200,
                        {
                            "paging": {"is_end": True, "totals": 1},
                            "data": [
                                {
                                    "id": "c900",
                                    "author": {"name": "Reader"},
                                    "content": "Great explanation on variance scaling!",
                                    "vote_count": 15,
                                    "created_time": 1700022000,
                                }
                            ],
                        },
                    )
                    return

                if parsed.path.startswith("/api/v4/comment_v5/"):
                    self._send_json(200, {"paging": {"is_end": True, "totals": 0}, "data": []})
                    return

                self._send_json(404, {"error": "not found"})

            def _send_json(self, status: int, payload: Any) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send_bytes(status, raw, "application/json; charset=utf-8")

            def _send_html(self, status: int, html_str: str, *, extra_headers: dict[str, str] | None = None) -> None:
                self._send_bytes(status, html_str.encode("utf-8"), "text/html; charset=utf-8", extra_headers=extra_headers)

            def _send_text(self, status: int, text: str, *, content_type: str = "text/plain; charset=utf-8") -> None:
                self._send_bytes(status, text.encode("utf-8"), content_type)

            def _send_bytes(
                self,
                status: int,
                raw: bytes,
                content_type: str,
                *,
                extra_headers: dict[str, str] | None = None,
            ) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(raw)))
                    for k, v in (extra_headers or {}).items():
                        self.send_header(k, v)
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

    def __enter__(self) -> _MockZhihuServer:
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def test_manifest_v2_matches_schema_and_v1_config_parity() -> None:
    manifest_obj = load_manifest(PLUGIN_DIR)
    assert manifest_obj.schema_version == 2
    assert manifest_obj.id == "plugin-zhihu"
    assert manifest_obj.entry == "plugin:create_plugin"
    assert manifest_obj.domain == "text"
    assert manifest_obj.kind == "workspace"
    assert manifest_obj.provides == ["text.source.v1", "text.comments.v1", "zhihu.auth.v1", "zhihu.companion.v1"]

    raw_manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))
    assert set(raw_manifest.keys()).issubset(set(schema["properties"].keys()))
    cap_re = re.compile(schema["properties"]["provides"]["items"]["pattern"])
    assert all(cap_re.match(c) for c in raw_manifest["provides"] + raw_manifest["consumes"])

    fields = {item["key"]: item for item in raw_manifest["config_schema"]}
    assert fields["chrome_cookie_source"]["type"] == "select"
    assert fields["chrome_cookie_source"]["default"] == "auto"
    assert fields["chrome_cookie_source"]["options"] == ["auto", "plugin-chrome-web", "vault", "file", "disabled"]
    assert fields["cookie_header"]["type"] == "secret"
    assert fields["cookie_header"]["default"] == ""
    assert fields["cookie_file_path"]["type"] == "string"
    assert fields["prefer_chrome_fetch"]["type"] == "bool"
    assert fields["prefer_chrome_fetch"]["default"] is False
    assert fields["request_timeout_sec"]["type"] == "int"
    assert fields["request_timeout_sec"]["default"] == 15
    assert fields["pinned_channels"]["type"] == "string"
    assert fields["auto_translate"]["type"] == "bool"
    assert fields["auto_translate"]["default"] is False


@pytest.mark.asyncio
async def test_lifecycle_on_load_health_check_and_unload_cleanup(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    plugin = mod.create_plugin()
    assert isinstance(plugin, BasePlugin)
    ctx = _make_ctx(tmp_path)

    await invoke_lifecycle_hook(plugin.on_load, ctx)
    assert len(ctx.domains.text_sources()) == 1
    for cap in ("text.source.v1", "text.comments.v1", "zhihu.auth.v1", "zhihu.companion.v1"):
        assert ctx.capabilities.has(cap)

    health = await invoke_lifecycle_hook(plugin.on_health_check)
    assert health["ok"] is True
    assert health["plugin_id"] == "plugin-zhihu"
    assert health["auth"]["authenticated"] is False

    await invoke_lifecycle_hook(plugin.on_unload)
    summary = await ctx.acleanup()
    assert summary["text_sources"] == 1
    assert summary["workspace_tabs"] == 1
    assert not ctx.capabilities.has("text.source.v1")


def test_real_html_initial_data_latex_code_quote_and_comment_tree_parsing() -> None:
    from zhihu_parser import (
        extract_initial_data,
        parse_article_from_initial_data,
        parse_article_id_or_url,
        parse_comments_from_initial_data,
        parse_fallback_html_dom,
    )

    html = r"""
<!DOCTYPE html>
<html>
<head><title>为什么微内核插件架构适合多模态桌面伴侣？ - 知乎</title></head>
<body>
<script id="js-initialData" type="text/json">
{
  "initialState": {
    "entities": {
      "questions": {
        "556677": {
          "id": 556677,
          "title": "为什么微内核插件架构适合多模态桌面伴侣？",
          "detail": "<p>假设系统吞吐满足 <img data-formula=\"\\lambda &lt; \\mu\" alt=\"[公式]\"/> 的稳态条件。</p>",
          "answerCount": 12,
          "followerCount": 340,
          "created": 1700000000
        }
      },
      "answers": {
        "889900": {
          "id": 889900,
          "question": {"id": 556677, "title": "为什么微内核插件架构适合多模态桌面伴侣？"},
          "author": {"name": "系统架构师"},
          "voteupCount": 1024,
          "commentCount": 18,
          "updatedTime": 1700003600,
          "content": "<h2>一、故障隔离数学模型</h2><p>在子进程隔离下，单插件崩溃不影响宿主窗口树。内联公式如 <img src=\"https://www.zhihu.com/equation?tex=x\" data-formula=\"P(\\text{crash}) \\to 0\"/> 所示。</p><p><img src=\"https://www.zhihu.com/equation?tex=E\" class=\"ee_img tr_noresize\" data-formula=\"\\mathcal{L}(\\theta) = \\frac{1}{N}\\sum_{i=1}^{N} \\log p(y_i \\mid x_i; \\theta)\"/></p><blockquote>将重度 DOM 解析放入 subprocess worker。</blockquote><pre><code class=\"language-cpp\">int main() {\n    return 0;\n}</code></pre><ul><li>第一条</li><li>第二条</li></ul><figure><figcaption>架构图注</figcaption></figure>"
        }
      },
      "comments": {
        "9001": {
          "id": 9001,
          "author": {"member": {"name": "系统架构师"}},
          "content": "<p>补充一点推导：<img data-formula=\"\\nabla_{\\theta}\\mathcal{L} = 0\"/></p>",
          "vote_count": 66,
          "created_time": 1700004000
        },
        "9002": {
          "id": 9002,
          "root_comment_id": 9001,
          "author": {"member": {"name": "读者甲"}},
          "reply_to_author": {"member": {"name": "系统架构师"}},
          "content": "非常清晰的证明！",
          "vote_count": 12,
          "created_time": 1700004500
        }
      }
    }
  }
}
</script>
</body>
</html>
"""
    init = extract_initial_data(html)
    assert isinstance(init, dict)
    target = parse_article_id_or_url("https://www.zhihu.com/question/556677/answer/889900")
    assert target.kind == "answer"
    assert target.question_id == "556677"
    assert target.answer_id == "889900"
    assert target.canonical_id == "answer:556677:889900"

    article = parse_article_from_initial_data(init, target)
    assert article is not None
    assert article.id == "answer:556677:889900"
    assert article.title == "为什么微内核插件架构适合多模态桌面伴侣？"
    assert article.authors == "系统架构师"

    assert any(
        p.kind == "formula"
        and p.original == r"\mathcal{L}(\theta) = \frac{1}{N}\sum_{i=1}^{N} \log p(y_i \mid x_i; \theta)"
        for p in article.paragraphs
    )
    assert any(p.kind == "paragraph" and r"$P(\text{crash}) \to 0$" in p.original for p in article.paragraphs)
    assert any(p.kind == "quote" and r"$\lambda < \mu$" in p.original for p in article.paragraphs)
    assert any(
        p.kind == "code" and p.language == "cpp" and "\n    return 0;\n" in p.original
        for p in article.paragraphs
    )
    assert any(p.kind == "quote" and "subprocess worker" in p.original for p in article.paragraphs)
    assert any(p.kind == "paragraph" and p.original == "• 第一条" for p in article.paragraphs)
    assert any(p.kind == "paragraph" and p.original == "[图注] 架构图注" for p in article.paragraphs)

    assert len(article.comments) == 1
    assert article.comments[0].id == "9001"
    assert article.comments[0].role_badge == "答主"
    assert r"$\nabla_{\theta}\mathcal{L} = 0$" in article.comments[0].body
    assert len(article.comments[0].replies) == 1
    assert article.comments[0].replies[0].id == "9002"
    assert article.comments[0].replies[0].body.startswith("回复 @系统架构师：")

    # DOM fallback works for genuine RichText, fails closed on malformed garbage HTML
    dom_art = parse_fallback_html_dom(
        "<html><body><h1>标题</h1><div class='RichText'><p>真实段落</p></div></body></html>",
        parse_article_id_or_url("article:123"),
    )
    assert dom_art is not None and dom_art.paragraphs[0].original == "真实段落"
    assert parse_fallback_html_dom("<html><body><div>broken<p>unclosed", parse_article_id_or_url("article:123")) is None

    # Cyclic parent references in malformed comments must not loop infinitely
    cyclic = parse_comments_from_initial_data(
        {
            "initialState": {
                "entities": {
                    "comments": {
                        "1": {"id": 1, "reply_to_comment_id": 2, "content": "A"},
                        "2": {"id": 2, "reply_to_comment_id": 1, "content": "B"},
                    }
                }
            }
        }
    )
    assert len(cyclic) >= 1


def test_nested_comments_json_and_ztext_math_parsing() -> None:
    from zhihu_parser import parse_comments_json

    v5_json = {
        "paging": {
            "is_end": False,
            "next": "https://www.zhihu.com/api/v4/comment_v5/answers/889900/root_comment?offset=20",
            "totals": 45,
        },
        "data": [
            {
                "id": "1001",
                "author": {"name": "专栏作者"},
                "author_tag": [{"text": "作者"}],
                "content": '<p>主楼评论，含公式 <span class="ztext-math" data-tex="E=mc^2">E=mc^2</span></p>',
                "vote_count": 128,
                "created_time": 1700010000,
                "child_comments": [
                    {
                        "id": "1002",
                        "author": {"name": "路人乙"},
                        "reply_to_author": {"name": "专栏作者"},
                        "content": "感谢作者解答",
                        "like_count": 19,
                        "created_time": 1700010600,
                    }
                ],
            }
        ],
    }
    page = parse_comments_json(v5_json, "专栏作者")
    assert page.is_end is False
    assert page.total == 45
    assert len(page.comments) == 1
    assert page.comments[0].role_badge == "作者"
    assert page.comments[0].score == 128
    assert "$E=mc^2$" in page.comments[0].body
    assert len(page.comments[0].replies) == 1
    assert page.comments[0].replies[0].author == "路人乙"
    assert page.comments[0].replies[0].score == 19


@pytest.mark.asyncio
async def test_auth_priority_vault_netscape_path_safety_and_fail_closed_sync(tmp_path: Path) -> None:
    from zhihu_auth import ZhihuConfig
    from zhihu_errors import ZhihuError

    mod = _load_plugin_module()
    plugin = mod.ZhihuPlugin()
    ctx = _make_ctx(tmp_path)

    # 1. Missing Chrome bridge -> sync_cookies & login fail closed
    await plugin.on_load(ctx)
    sync_missing = await plugin.handle_auth_rpc({"action": "sync_cookies"})
    assert sync_missing.ok is False
    assert sync_missing.data["error_code"] == "not_configured"
    assert sync_missing.data["synced"] is False

    login_missing = await plugin.handle_auth_rpc({"action": "login"})
    assert login_missing.ok is False
    assert login_missing.data["error_code"] == "service_offline"

    # 2. Chrome returns only guest d_c0 without z_c0 -> MUST fail closed (never fake authenticated!)
    chrome_mode = "guest_only"
    embed_calls: list[dict[str, Any]] = []

    def _mock_embed(req: dict[str, Any]) -> CapabilityResult:
        embed_calls.append(req)
        if req.get("action") == "open":
            return CapabilityResult.ok({"session_id": "chrome-zhihu-1"})
        return CapabilityResult.fail("unsupported")

    def _mock_cookies(req: dict[str, Any]) -> CapabilityResult:
        assert req.get("domain") == "zhihu.com"
        if chrome_mode == "guest_only":
            return CapabilityResult.ok({"cookie_header": "d_c0=guest_dev; _xsrf=xsrf_guest"})
        return CapabilityResult.ok(
            {"cookie_header": "z_c0=2|1:0|10:secret_token_999; d_c0=device_abc; _xsrf=xsrf_123; __zse_ck=001_sig"}
        )

    ctx.capabilities.register("web.embed.v1", _mock_embed, plugin_id="plugin-chrome-web")
    ctx.capabilities.register("web.cookies.v1", _mock_cookies, plugin_id="plugin-chrome-web")

    guest_sync = await plugin.handle_text_source_rpc({"action": "sync_cookies"})
    assert guest_sync.ok is False
    assert guest_sync.data["error_code"] == "not_configured"
    assert guest_sync.data["auth"]["authenticated"] is False

    # 3. Chrome returns full session with z_c0 -> sync succeeds & secrets never leak
    chrome_mode = "full"
    ok_sync = await plugin.handle_text_source_rpc({"action": "sync_cookies"})
    assert ok_sync.ok is True
    assert ok_sync.data["synced"] is True
    assert ok_sync.data["auth"]["authenticated"] is True
    assert ok_sync.data["auth"]["has_z_c0"] is True
    assert ok_sync.data["auth"]["has_zse_ck"] is True
    assert "secret_token_999" not in json.dumps(ok_sync.data)
    assert "z_c0=2|1:0|10:secret_token_999" in (ctx.get_secret("cookie_header") or "")

    # 4. Login window triggers web.embed.v1 to https://www.zhihu.com/signin
    login_res = await plugin.handle_text_source_rpc({"action": "login"})
    assert login_res.ok is True
    assert login_res.data["launched"] is True
    assert embed_calls[-1]["url"] == "https://www.zhihu.com/signin"

    # 5. Netscape cookie file fallback with expiry & domain filtering + JSON cookies
    cookie_txt = tmp_path / "cookies.txt"
    future_ts = int(time.time()) + 86400
    expired_ts = int(time.time()) - 100
    cookie_txt.write_text(
        "# Netscape HTTP Cookie File\n"
        f"#HttpOnly_.zhihu.com\tTRUE\t/\tTRUE\t{future_ts}\tz_c0\tfile_zc0_secret\n"
        f".zhihu.com\tTRUE\t/\tFALSE\t{future_ts}\t_xsrf\tfile_xsrf\n"
        f".zhihu.com\tTRUE\t/\tFALSE\t{expired_ts}\td_c0\texpired_val\n"
        f".other.com\tTRUE\t/\tFALSE\t{future_ts}\tz_c0\twrong_domain\n",
        encoding="utf-8",
    )
    plugin.auth.configure(ZhihuConfig(chrome_cookie_source="file", cookie_file_path=str(cookie_txt)), "")
    ctx.set_config({"chrome_cookie_source": "file", "cookie_file_path": str(cookie_txt)})
    file_res = await plugin.auth.resolve_for_url("https://www.zhihu.com/question/1")
    assert file_res.source_used == "file"
    assert file_res.has_z_c0 is True
    assert file_res.has_d_c0 is False  # expired cookie excluded
    assert "wrong_domain" not in file_res.cookie_header

    # 6. Path traversal & symlink rejected fail closed
    with pytest.raises(ZhihuError) as exc_trav:
        plugin.auth.configure(ZhihuConfig(chrome_cookie_source="file", cookie_file_path="../secret/cookies.txt"))
    assert exc_trav.value.error_code == "path_safety"

    symlink_cookie = tmp_path / "sym_cookies.txt"
    symlink_cookie.symlink_to(cookie_txt)
    ctx.set_config({"chrome_cookie_source": "file", "cookie_file_path": str(symlink_cookie)})
    bad_status = await plugin.handle_auth_rpc({"action": "status"})
    assert bad_status.ok is False
    assert bad_status.data["error_code"] == "path_safety"

    # 7. set_cookie_header & logout & control char rejection
    ctx.set_config({"chrome_cookie_source": "vault", "cookie_file_path": ""})
    bad_hdr = await plugin.handle_auth_rpc({"action": "set_cookie_header", "cookie_header": "z_c0=abc\r\nX-Injected: 1"})
    assert bad_hdr.ok is False
    assert bad_hdr.data["error_code"] == "invalid_argument"

    set_ok = await plugin.handle_auth_rpc({"action": "set_cookie_header", "cookie_header": "Cookie: z_c0=manual_tok; _xsrf=x1"})
    assert set_ok.ok is True
    assert set_ok.data["auth"]["authenticated"] is True
    assert "manual_tok" not in json.dumps(set_ok.data)

    out_res = await plugin.handle_auth_rpc({"action": "logout"})
    assert out_res.ok is True
    assert out_res.data["auth"]["authenticated"] is False

    await plugin.on_unload()
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_loopback_server_feeds_articles_comments_errors_timeouts_and_redirects(tmp_path: Path) -> None:
    from zhihu_http import ZhihuHttpRequest, ZhihuHttpResponse

    mod = _load_plugin_module()
    with _ExternalLandingServer() as ext_srv, _MockZhihuServer(external_landing_url=ext_srv.base_url) as srv:
        plugin = mod.ZhihuPlugin(
            web_base_url=srv.base_url,
            zhuanlan_base_url=srv.base_url,
            allow_loopback_http=True,
        )
        ctx = _make_ctx(tmp_path)
        await plugin.on_load(ctx)

        # A. Guest mode: hot feed succeeds, auth-required channels fail closed (NO fake items!)
        hot_res = await plugin.handle_text_source_rpc({"action": "fetch_feed", "channel_id": "hot"})
        assert hot_res.ok is True
        assert len(hot_res.data["items"]) == 1
        assert hot_res.data["items"][0]["id"] == "question:556677"
        assert hot_res.data["items"][0]["score"] == 25600000

        # Guest hot page 2 -> empty array, no fake data
        hot_p2 = await plugin.handle_text_source_rpc({"action": "fetch_feed", "channel_id": "hot", "page": 2})
        assert hot_p2.ok is True
        assert hot_p2.data["items"] == []

        # Direct URL query works for page 1, empty for page 2, rejects plain http:// external
        direct_q = await plugin.handle_text_source_rpc(
            {"action": "fetch_feed", "channel_id": "hot", "query": "https://zhuanlan.zhihu.com/p/778899"}
        )
        assert direct_q.ok is True and direct_q.data["items"][0]["id"] == "article:778899"
        direct_q2 = await plugin.handle_text_source_rpc(
            {"action": "fetch_feed", "channel_id": "hot", "query": "https://zhuanlan.zhihu.com/p/778899", "page": 2}
        )
        assert direct_q2.ok is True and direct_q2.data["items"] == []

        for auth_ch in ("recommend", "follow", "collections"):
            fail_ch = await plugin.handle_text_source_rpc({"action": "fetch_feed", "channel_id": auth_ch})
            assert fail_ch.ok is False
            assert fail_ch.data["error_code"] == "not_configured"
            assert fail_ch.data["auth_required"] is True
            assert fail_ch.data["items"] == []

        # B. Authenticated session + pinned_channels ordering
        ctx.set_secret("cookie_header", "z_c0=valid_sess_tok_888; d_c0=dev1; _xsrf=xsrf_tok_1; __zse_ck=zse1")
        ctx.set_config("pinned_channels", "recommend,collection:301,follow")
        ch_res = await plugin.handle_text_source_rpc({"action": "channels"})
        assert ch_res.ok is True
        ch_ids = [c["id"] for c in ch_res.data["channels"]]
        assert ch_ids == ["recommend", "collection:301", "follow", "hot", "collections"]

        # C. Hot HTML fallback when hot-lists API fails
        srv.mode = "hot_html_fallback"
        hot_fb = await plugin.handle_text_source_rpc({"action": "fetch_feed", "channel_id": "hot"})
        assert hot_fb.ok is True
        assert hot_fb.data["items"][0]["id"] == "question:998877"
        srv.mode = "ok"

        # D. Recommend, Follow, Collections, Collection:301, Search + empty + pagination boundary
        for ch_id, expected_id in (
            ("recommend", "article:778899"),
            ("follow", "answer:556677:889900"),
            ("collections", "article:778899"),
            ("collection:301", "article:778899"),
        ):
            res = await plugin.handle_text_source_rpc({"action": "fetch_feed", "channel_id": ch_id})
            assert res.ok is True, f"{ch_id} failed: {res}"
            assert res.data["items"][0]["id"] == expected_id

        search_p1 = await plugin.handle_text_source_rpc({"action": "search", "query": "attention", "page": 1})
        assert search_p1.ok is True and len(search_p1.data["items"]) == 1
        search_p2 = await plugin.handle_text_source_rpc({"action": "search", "query": "attention", "page": 2})
        assert search_p2.ok is True and search_p2.data["items"] == []
        bad_page = await plugin.handle_text_source_rpc({"action": "fetch_feed", "channel_id": "hot", "page": 0})
        assert bad_page.ok is False and bad_page.data["error_code"] == "invalid_argument"

        srv.mode = "empty"
        empty_hot = await plugin.handle_text_source_rpc({"action": "fetch_feed", "channel_id": "hot"})
        assert empty_hot.ok is True and empty_hot.data["items"] == []
        srv.mode = "ok"

        # E. Fetch article (HTML initialData + automatic nested comment enrichment) & API v4 fallback
        art_res = await plugin.handle_text_source_rpc({"action": "fetch_article", "id": "article:778899"})
        assert art_res.ok is True
        art = art_res.data["article"]
        assert art["id"] == "article:778899"
        assert art["paragraphs"][1]["kind"] == "formula"
        assert art["paragraphs"][2]["kind"] == "code"
        assert art["paragraphs"][2]["language"] == "python"
        assert len(art["comments"]) == 1 and art["comments"][0]["replies"][0]["id"] == "c501"

        api_art = await plugin.handle_text_source_rpc({"action": "fetch_article", "id": "answer:4455:112233"})
        assert api_art.ok is True
        assert api_art.data["article"]["id"] == "answer:4455:112233"

        # F. Comments RPC pagination & boundary validation
        c_p1 = await plugin.handle_comments_rpc({"action": "fetch_comments", "article_id": "article:778899", "offset": 0, "limit": 20})
        assert c_p1.ok is True and c_p1.data["paging"]["is_end"] is False and len(c_p1.data["comments"]) == 1
        c_p2 = await plugin.handle_comments_rpc({"action": "fetch_comments", "article_id": "article:778899", "offset": 20, "limit": 20})
        assert c_p2.ok is True and c_p2.data["paging"]["is_end"] is True and c_p2.data["comments"] == []
        c_child = await plugin.handle_comments_rpc({"action": "fetch_child_comments", "root_comment_id": "c500"})
        assert c_child.ok is True and c_child.data["comments"][0]["id"] == "c501"
        c_bad = await plugin.handle_comments_rpc({"action": "fetch_comments", "article_id": "article:778899", "offset": -1})
        assert c_bad.ok is False and c_bad.data["error_code"] == "invalid_argument"

        # G. Error modes: 401, signin_redirect, cloudflare, 40362, 429, 404, 500, 502, malformed HTML/JSON, oversized
        for mode, expected_code, extra_flag in (
            ("401", "permission_denied", "auth_required"),
            ("signin_redirect", "permission_denied", "auth_required"),
            ("cloudflare", "permission_denied", "cloudflare_blocked"),
            ("40362", "permission_denied", "challenge_detected"),
            ("429", "rate_limited", ""),
            ("404", "not_found", ""),
            ("500", "service_offline", ""),
            ("502", "service_offline", ""),
            ("malformed_html", "validation_error", ""),
            ("malformed_json", "validation_error", ""),
            ("oversized", "too_large", ""),
        ):
            srv.mode = mode
            res = await plugin.handle_text_source_rpc({"action": "fetch_article", "id": "article:778899"})
            assert res.ok is False, f"mode {mode} unexpectedly succeeded"
            assert res.data["error_code"] == expected_code, f"mode {mode} got {res.data}"
            if extra_flag:
                assert res.data.get(extra_flag) is True
            dumped = json.dumps(res.model_dump())
            assert "valid_sess_tok_888" not in dumped
            assert "xsrf_tok_1" not in dumped

        # H. Timeout test (fast 0.2s client timeout against 1.2s server sleep)
        srv.mode = "timeout"
        ctx.set_config("request_timeout_sec", 0.2)
        t_res = await plugin.handle_text_source_rpc({"action": "fetch_article", "id": "article:778899"})
        assert t_res.ok is False
        assert t_res.data["error_code"] == "timeout"
        ctx.set_config("request_timeout_sec", 15)
        srv.mode = "ok"

        # I. Real SafeHttpClient cross-origin redirect strips Cookie & Authorization + redirect cap + HTTPS-only
        redir_resp = await plugin.http.perform_direct(
            ZhihuHttpRequest(
                url=f"{srv.base_url}/redirect-external",
                headers={"Cookie": "z_c0=secret_redir_tok", "Authorization": "Bearer secret_auth"},
            ),
            secrets=("secret_redir_tok", "secret_auth"),
        )
        assert redir_resp.ok is True
        assert ext_srv.received_headers
        assert "cookie" not in ext_srv.received_headers[-1]
        assert "authorization" not in ext_srv.received_headers[-1]

        loop_resp = await plugin.http.perform_direct(
            ZhihuHttpRequest(url=f"{srv.base_url}/redirect-loop", max_redirects=3)
        )
        assert loop_resp.ok is False
        assert loop_resp.error_code == "blocked_redirect"

        # Plain http:// non-loopback rejected strictly
        non_https = await plugin.http.perform_direct(ZhihuHttpRequest(url="http://www.zhihu.com/hot"))
        assert non_https.ok is False
        assert non_https.error_code == "invalid_argument"

        # J. transport_override also strips x-xsrftoken on cross-origin redirect
        saw_external_xsrf = False

        def _override(req: ZhihuHttpRequest) -> ZhihuHttpResponse:
            nonlocal saw_external_xsrf
            if req.url == "https://www.zhihu.com/redir-out":
                assert "Cookie" in req.headers and "x-xsrftoken" in req.headers
                return ZhihuHttpResponse(ok=True, status_code=302, headers={"location": "https://external.example.org/done"})
            if req.url == "https://external.example.org/done":
                saw_external_xsrf = "x-xsrftoken" in req.headers or "Cookie" in req.headers
                return ZhihuHttpResponse(ok=True, status_code=200, body="ok")
            return ZhihuHttpResponse(ok=False, status_code=404)

        plugin.http.set_transport_override(_override)
        ov_resp = await plugin.http.fetch_with_fallback("https://www.zhihu.com/redir-out", plugin.auth, expect_json=False)
        assert ov_resp.ok is True and saw_external_xsrf is False
        plugin.http.set_transport_override(None)

        await plugin.on_unload()
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_chrome_web_fetch_fallback_never_leaks_cookie_header(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    plugin = mod.ZhihuPlugin()
    ctx = _make_ctx(tmp_path)
    ctx.set_secret("cookie_header", "z_c0=chrome_secret_tok; _xsrf=xsrf_hidden")

    captured_fetch_reqs: list[dict[str, Any]] = []

    def _mock_fetch(req: dict[str, Any]) -> CapabilityResult:
        captured_fetch_reqs.append(req)
        assert "Cookie" not in (req.get("headers") or {})
        assert "cookie" not in {k.lower() for k in (req.get("headers") or {})}
        assert "x-xsrftoken" not in {k.lower() for k in (req.get("headers") or {})}
        body = {
            "data": [
                {
                    "type": "feed",
                    "target": {
                        "id": 778899,
                        "type": "article",
                        "title": "Chrome 代理推荐结果",
                        "excerpt": "通过 web.fetch.v1 获取",
                        "author": {"name": "作者"},
                    },
                }
            ]
        }
        return CapabilityResult.ok({"status": 200, "url": req["url"], "body": json.dumps(body)})

    ctx.capabilities.register("web.fetch.v1", _mock_fetch, plugin_id="plugin-chrome-web")
    await plugin.on_load(ctx)

    rec = await plugin.handle_text_source_rpc({"action": "fetch_feed", "channel_id": "recommend"})
    assert rec.ok is True
    assert len(captured_fetch_reqs) == 1
    assert rec.data["items"][0]["id"] == "article:778899"

    await plugin.on_unload()
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_auto_translate_and_companion_llm(tmp_path: Path) -> None:
    mod = _load_plugin_module()
    with _MockZhihuServer() as srv:
        plugin = mod.ZhihuPlugin(
            web_base_url=srv.base_url,
            zhuanlan_base_url=srv.base_url,
            allow_loopback_http=True,
        )
        ctx = _make_ctx(tmp_path)
        ctx.set_config("auto_translate", True)

        translate_calls: list[dict[str, Any]] = []

        def _mock_translate(req: dict[str, Any]) -> CapabilityResult:
            translate_calls.append(req)
            texts = req.get("texts") or ([req["text"]] if "text" in req else [])
            return CapabilityResult.ok({"translations": [f"[译]{t}" for t in texts]})

        def _mock_chat(req: dict[str, Any]) -> CapabilityResult:
            return CapabilityResult.ok(
                {
                    "reply": "主旨：缩放点积保持方差稳定 $\\frac{1}{\\sqrt{d_k}}$。",
                    "model": "group/omnidesk-active",
                }
            )

        ctx.capabilities.register("llm.translate.v1", _mock_translate, plugin_id="plugin-magpie-agent")
        ctx.capabilities.register("llm.chat.v1", _mock_chat, plugin_id="plugin-magpie-agent")
        await plugin.on_load(ctx)

        # 1. Auto-translation skips code & formula blocks while translating title, headings, prose, quotes, comments
        art_res = await plugin.handle_text_source_rpc({"action": "fetch_article", "id": "article:990011"})
        assert art_res.ok is True
        art = art_res.data["article"]
        assert art["title_zh"] == "[译]Attention Is All You Need Notes"
        by_kind = {p["kind"]: p for p in art["paragraphs"]}
        assert by_kind["heading"]["translated"] == "[译]Overview"
        assert by_kind["paragraph"]["translated"] == "[译]Scaled dot-product attention stabilizes variance."
        assert by_kind["quote"]["translated"] == "[译]Keep math formulas untouched."
        assert by_kind["formula"]["translated"] == ""
        assert by_kind["code"]["translated"] == ""
        assert art["comments"][0]["translated_body"] == "[译]Great explanation on variance scaling!"

        # 2. Companion reading actions via zhihu.companion.v1 & text.source.v1
        for mode in ("summarize", "key_points", "explain_formulas"):
            comp = await plugin.handle_companion_rpc({"action": mode, "article_id": "article:990011"})
            assert comp.ok is True
            assert comp.data["mode"] == mode
            assert comp.data["formula_count"] == 1
            assert comp.data["model"] == "group/omnidesk-active"

        ask_res = await plugin.handle_text_source_rpc(
            {"action": "ask", "article": art, "question": "为什么保持方差？"}
        )
        assert ask_res.ok is True
        assert ask_res.data["mode"] == "ask"

        # 3. Fail closed on missing/empty article
        bad_comp = await plugin.handle_companion_rpc({"action": "summarize"})
        assert bad_comp.ok is False
        assert bad_comp.data["error_code"] == "invalid_argument"

        # 4. Graceful degradation when llm.translate.v1 fails & fail closed when llm.chat.v1 absent
        ctx.capabilities.unregister("llm.translate.v1", plugin_id="plugin-magpie-agent")
        degraded_art = await plugin.handle_text_source_rpc({"action": "fetch_article", "id": "article:990011"})
        assert degraded_art.ok is True
        assert degraded_art.data["article"]["paragraphs"][1]["translated"] == ""

        ctx.capabilities.unregister("llm.chat.v1", plugin_id="plugin-magpie-agent")
        no_llm = await plugin.handle_companion_rpc({"action": "summarize", "article_id": "article:990011"})
        assert no_llm.ok is False
        assert no_llm.data["error_code"] == "service_offline"

        await plugin.on_unload()
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_web_extract_fallback_json_cookies_v4_comments_and_domain_source(tmp_path: Path) -> None:
    from zhihu_http import ZhihuHttpRequest, ZhihuHttpResponse

    mod = _load_plugin_module()
    plugin = mod.ZhihuPlugin()
    ctx = _make_ctx(tmp_path)

    # 1. JSON cookie file parsing & filtering
    cookies_json = tmp_path / "cookies.json"
    future_ts = int(time.time()) + 7200
    cookies_json.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "z_c0",
                        "value": "json_zc0_val",
                        "domain": ".zhihu.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "expirationDate": future_ts,
                    },
                    {
                        "name": "_xsrf",
                        "value": "json_xsrf_val",
                        "domain": "www.zhihu.com",
                        "hostOnly": True,
                        "path": "/question",
                        "secure": False,
                        "expirationDate": future_ts,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    ctx.set_config({"chrome_cookie_source": "file", "cookie_file_path": str(cookies_json)})

    # 2. web.extract.v1 fallback when direct HTML fails & web.fetch.v1 unavailable
    def _mock_extract(req: dict[str, Any]) -> CapabilityResult:
        assert req["action"] == "extract"
        return CapabilityResult.ok(
            {
                "status": 200,
                "url": req["url"],
                "html": (
                    "<html><body><script id='js-initialData'>"
                    + json.dumps(
                        {
                            "initialState": {
                                "entities": {
                                    "questions": {
                                        "12345": {
                                            "id": 12345,
                                            "title": "问题测试标题",
                                            "detail": "<p>问题详情</p>",
                                            "answerCount": 1,
                                            "followerCount": 10,
                                        }
                                    },
                                    "answers": {
                                        "999": {
                                            "id": 999,
                                            "question": {"id": 12345},
                                            "author": {"name": "答主Z"},
                                            "voteupCount": 42,
                                            "content": "<p>通过 web.extract.v1 渲染获取</p>",
                                        }
                                    },
                                }
                            }
                        }
                    )
                    + "</script></body></html>"
                ),
            }
        )

    ctx.capabilities.register("web.extract.v1", _mock_extract, plugin_id="plugin-chrome-web")
    await plugin.on_load(ctx)

    # Override direct transport: fail HTML & comment_v5 so extract & v4 comment fallback trigger
    def _override(req: ZhihuHttpRequest) -> ZhihuHttpResponse:
        if req.url == "https://www.zhihu.com/question/12345":
            return ZhihuHttpResponse(ok=False, status_code=403, body="安全验证")
        if "/comment_v5/" in req.url:
            return ZhihuHttpResponse(ok=False, status_code=403, body='{"error":{"code":40362}}')
        if "/root_comments" in req.url:
            return ZhihuHttpResponse(
                ok=True,
                status_code=200,
                body=json.dumps(
                    {
                        "paging": {"is_end": True, "totals": 1},
                        "data": [{"id": "v4c1", "author": {"name": "V4评论者"}, "content": "v4 兜底评论", "like_count": 7}],
                    }
                ),
            )
        return ZhihuHttpResponse(ok=False, status_code=404)

    plugin.http.set_transport_override(_override)
    src = ctx.domains.text_sources()[0]
    assert [c.id for c in src.channels()][1] == "recommend"
    assert src.channels()[1].badge == ""  # authenticated via cookies.json

    question_art = await src.fetch_article("question:12345")
    assert question_art.id == "question:12345"
    assert question_art.title == "问题测试标题"
    assert any("web.extract.v1" in p.original for p in question_art.paragraphs)
    assert len(question_art.comments) == 1 and question_art.comments[0].id == "v4c1"

    # Reject non-zhihu URLs and unknown actions fail-closed
    bad_url = await plugin.handle_text_source_rpc({"action": "fetch_article", "url": "https://evil.example.com/p/1"})
    assert bad_url.ok is False and bad_url.data["error_code"] == "invalid_argument"
    bad_act = await plugin.handle_text_source_rpc({"action": "unknown_action"})
    assert bad_act.ok is False and bad_act.data["error_code"] == "invalid_argument"
    tab_res = await plugin.handle_tab_action("auth_status")
    assert tab_res["ok"] is True and tab_res["result"]["authenticated"] is True
    tab_status = await plugin.handle_tab_action("status")
    assert tab_status["ok"] is True and tab_status["result"]["authenticated"] is True

    await plugin.on_unload()
    await ctx.acleanup()
