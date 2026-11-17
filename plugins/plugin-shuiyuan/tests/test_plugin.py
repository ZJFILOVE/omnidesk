"""Offline tests for the Shuiyuan plugin wrapper and lifecycle."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from omnidesk_sdk import PluginContext  # noqa: E402
from shuiyuan_http import HttpRequest, HttpResponse  # noqa: E402
from plugin import ShuiyuanPlugin, create_plugin  # noqa: E402


class _FakeHttp:
    def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
        self.payloads = payloads
        self.requests: list[HttpRequest] = []

    async def request(self, req: HttpRequest, *, extra_secrets=()) -> HttpResponse:  # type: ignore[no-untyped-def]
        del extra_secrets
        self.requests.append(req)
        path = req.url.split("shuiyuan.test", 1)[-1]
        payload = self.payloads.get(path)
        if payload is None:
            return HttpResponse(status_code=404, effective_url=req.url, body="{}")
        return HttpResponse(
            status_code=200,
            effective_url=req.url,
            headers={"content-type": "application/json"},
            body=json.dumps(payload),
        )


@pytest.mark.asyncio
async def test_lifecycle_registers_source_comments_and_tab() -> None:
    plugin = create_plugin()
    ctx = PluginContext("plugin-shuiyuan")

    await plugin.on_load(ctx)
    try:
        assert ctx.capabilities.has("text.source.v1")
        assert ctx.capabilities.has("text.comments.v1")
        assert plugin.on_health_check is not None
        health = await plugin.on_health_check()
        assert health["plugin_id"] == "plugin-shuiyuan"
        assert health["ok"] is True
        tab_status = await plugin.handle_tab_action("status")
        assert tab_status["ok"] is True
        assert tab_status["result"]["auth"]["authenticated"] is False
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-shuiyuan")


@pytest.mark.asyncio
async def test_fetch_feed_parses_real_discourse_shape() -> None:
    payload = {
        "users": [{"id": 7, "username": "alice", "name": "Alice"}],
        "topic_list": {
            "more_topics_url": "/latest?page=1",
            "topics": [
                {
                    "id": 42,
                    "slug": "hello-sjtu",
                    "title": "Hello &amp; welcome",
                    "fancy_title": "Hello &amp; welcome",
                    "posts_count": 4,
                    "like_count": 9,
                    "category_id": 0,
                    "last_posted_at": "2026-11-17T01:02:03Z",
                    "posters": [{"user_id": 7, "description": "Original Poster"}],
                }
            ],
        },
    }
    http = _FakeHttp({"/latest.json": payload})
    plugin = ShuiyuanPlugin(
        base_url="https://shuiyuan.test",
        http_client=http,
    )
    ctx = PluginContext("plugin-shuiyuan")
    await plugin.on_load(ctx)
    try:
        result = await plugin.handle_text_source_rpc({"action": "fetch_feed", "channel_id": "latest"})
        assert result.ok is True
        item = result.data["items"][0]
        assert item["id"] == "topic:42"
        assert item["title"] == "Hello & welcome"
        assert item["author"] == "Alice (@alice)"
        assert item["comment_count"] == 3
        assert item["url"] == "https://shuiyuan.test/t/hello-sjtu/42"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-shuiyuan")


@pytest.mark.asyncio
async def test_auth_required_is_fail_closed_and_redacted() -> None:
    http = _FakeHttp({"/latest.json": {"error_type": "not_logged_in"}})
    plugin = ShuiyuanPlugin(base_url="https://shuiyuan.test", http_client=http)
    ctx = PluginContext("plugin-shuiyuan")
    await plugin.on_load(ctx)
    try:
        plugin.auth.set_manual_cookie_header("_t=super-secret-token")
        result = await plugin.handle_text_source_rpc({"action": "fetch_feed"})
        assert result.ok is False
        assert result.data["error_code"] == "permission_denied"
        assert "super-secret-token" not in json.dumps(result.model_dump(), ensure_ascii=False)
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-shuiyuan")


@pytest.mark.asyncio
async def test_comments_requires_article_id() -> None:
    plugin = create_plugin()
    ctx = PluginContext("plugin-shuiyuan")
    await plugin.on_load(ctx)
    try:
        result = await plugin.handle_comments_rpc({"action": "fetch_comments"})
        assert result.ok is False
        assert result.data["error_code"] == "invalid_argument"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-shuiyuan")
