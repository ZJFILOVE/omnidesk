"""Offline tests for the X plugin wrapper, timeline parsing, and subscriptions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from omnidesk_sdk import CapabilityResult, PluginContext, TextFeedItem  # noqa: E402
from plugin import XPlugin, create_plugin  # noqa: E402
from x_client import (  # noqa: E402
    HttpResponse,
    extract_chunk_urls_for_operations,
    extract_query_ids_from_js,
)
from x_subscriptions import XSubscriptionStore  # noqa: E402


def _timeline_payload() -> dict[str, object]:
    return {
        "data": {
            "home": {
                "home_timeline_urt": {
                    "instructions": [
                        {
                            "type": "TimelineAddEntries",
                            "entries": [
                                {
                                    "entryId": "tweet-123",
                                    "content": {
                                        "entryType": "TimelineTimelineItem",
                                        "itemContent": {
                                            "tweet_results": {
                                                "result": {
                                                    "rest_id": "123",
                                                    "core": {
                                                        "user_results": {
                                                            "result": {
                                                                "rest_id": "7",
                                                                "core": {
                                                                    "name": "Alice",
                                                                    "screen_name": "alice",
                                                                },
                                                            }
                                                        }
                                                    },
                                                    "legacy": {
                                                        "full_text": "Hello X",
                                                        "created_at": "Mon Nov 17 01:02:03 +0000 2026",
                                                        "favorite_count": 12,
                                                        "reply_count": 2,
                                                        "retweet_count": 1,
                                                    },
                                                }
                                            }
                                        },
                                    },
                                },
                                {
                                    "entryId": "cursor-bottom-1",
                                    "content": {
                                        "entryType": "TimelineTimelineCursor",
                                        "cursorType": "Bottom",
                                        "value": "next-cursor",
                                    },
                                },
                            ],
                        }
                    ]
                }
            }
        }
    }


@pytest.mark.asyncio
async def test_lifecycle_and_timeline_fetch() -> None:
    payload = _timeline_payload()

    def transport(req):  # type: ignore[no-untyped-def]
        return HttpResponse(
            ok=True,
            status_code=200,
            final_url=req.url,
            body=json.dumps(payload),
            transport="test",
        )

    plugin = XPlugin(transport_override=transport)
    ctx = PluginContext("plugin-x")
    await plugin.on_load(ctx)
    try:
        assert ctx.capabilities.has("text.source.v1")
        assert ctx.capabilities.has("text.comments.v1")
        result = await plugin.handle_text_source_rpc(
            {"action": "fetch_feed", "channel_id": "for_you", "translate": False}
        )
        assert result.ok is True
        assert result.data["count"] == 1
        item = result.data["items"][0]
        assert item["id"] == "123"
        assert item["title"] == "@alice: Hello X"
        assert item["author"] == "Alice (@alice)"
        assert result.data["next_cursor"] == "next-cursor"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-x")


def test_subscription_store_parses_lists_and_urls(tmp_path: Path) -> None:
    store = XSubscriptionStore(tmp_path)
    store.apply_default_spec("123:AI News, https://x.com/i/lists/456:Mixed")
    try:
        assert [(item.id, item.name) for item in store.list_pinned()] == [
            ("123", "AI News"),
            ("456", "Mixed"),
        ]
        assert [channel.id for channel in store.build_channels()] == [
            "following",
            "for_you",
            "bookmarks",
            "lists",
            "list:123",
            "list:456",
        ]
    finally:
        # Leave cleanup to pytest tmp_path.
        pass


@pytest.mark.asyncio
async def test_article_requires_a_tweet_target() -> None:
    plugin = create_plugin()
    ctx = PluginContext("plugin-x")
    await plugin.on_load(ctx)
    try:
        result = await plugin.handle_text_source_rpc(
            {"action": "fetch_article", "article_id": "file:///etc/passwd"}
        )
        assert result.ok is False
        assert result.data["error_code"] == "invalid_argument"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-x")


@pytest.mark.asyncio
async def test_login_without_chrome_capability_is_fail_closed() -> None:
    plugin = create_plugin()
    ctx = PluginContext("plugin-x")
    await plugin.on_load(ctx)
    try:
        result = await plugin.handle_text_source_rpc({"action": "login"})
        assert result.ok is False
        assert result.data["error_code"] == "service_offline"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-x")


@pytest.mark.asyncio
async def test_auto_translation_caps_batch_size() -> None:
    plugin = create_plugin()
    ctx = PluginContext("plugin-x")
    calls: list[list[str]] = []

    def translate(req):  # type: ignore[no-untyped-def]
        texts = list(req.get("texts") or [])
        calls.append(texts)
        return CapabilityResult.ok({"translations": [f"译文 {text}" for text in texts]})

    ctx.register_capability("llm.translate.v1", translate)
    await plugin.on_load(ctx)
    try:
        items = [
            TextFeedItem(id=str(idx), title=f"Hello tweet {idx}", category="social")
            for idx in range(45)
        ]
        translated = await plugin.translate_feed_items(items)
        assert len(calls) == 1
        assert len(calls[0]) == 5
        assert translated[4].title_zh == "译文 Hello tweet 4"
        assert translated[5].title_zh == ""
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-x")


@pytest.mark.asyncio
async def test_query_id_refresh_falls_back_when_chrome_fetch_fails() -> None:
    html = (
        "<html><script>"
        'const op = {queryId: "NEWQUERYID123456", operationName: "HomeLatestTimeline"};'
        "</script></html>"
    )

    def transport(req):  # type: ignore[no-untyped-def]
        return HttpResponse(ok=True, status_code=200, final_url=req.url, body=html, transport="test")

    plugin = XPlugin(transport_override=transport)
    ctx = PluginContext("plugin-x")
    await plugin.on_load(ctx)
    try:
        ctx.register_capability(
            "web.fetch.v1",
            lambda _req: CapabilityResult.fail("CDP 服务不可达", error_code="service_offline"),
        )
        refreshed = await plugin.client.refresh_query_ids_from_bundle(plugin.auth)
        assert refreshed is True
        assert plugin.client.query_id("HomeLatestTimeline") == "NEWQUERYID123456"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-x")


def test_extracts_operation_chunk_urls_from_webpack_mapping() -> None:
    html = (
        '<html><body><script>'
        '53852:"shared~bundle.Compose~bundle.HomeTimeline~bundle.LoggedInMain";'
        '53852:"d6e92168bece3358";'
        '61001:"bundle.Bookmarks";'
        '61001:"0011223344556677";'
        "</script></body></html>"
    )

    urls = extract_chunk_urls_for_operations(html, ["HomeLatestTimeline"])

    assert urls == [
        "https://abs.twimg.com/responsive-web/client-web/"
        "shared~bundle.Compose~bundle.HomeTimeline~bundle.LoggedInMain.d6e92168bece3358a.js"
    ]


@pytest.mark.asyncio
async def test_query_id_refresh_scans_operation_chunk() -> None:
    home = (
        '<html><script src="https://abs.twimg.com/responsive-web/client-web/'
        'main.abc123.js"></script><script>'
        '53852:"shared~bundle.Compose~bundle.HomeTimeline~bundle.LoggedInMain";'
        '53852:"d6e92168bece3358";'
        "</script></html>"
    )
    chunk = (
        '<script>const timeline = {queryId:"KmJCeZ__ef1fNos7tKN_fg",'
        'operationName:"HomeLatestTimeline"};</script>'
    )

    def transport(req):  # type: ignore[no-untyped-def]
        body = home if req.url == "https://x.com/home" else chunk if "HomeTimeline" in req.url else ""
        return HttpResponse(ok=True, status_code=200, final_url=req.url, body=body, transport="test")

    plugin = XPlugin(transport_override=transport)
    ctx = PluginContext("plugin-x")
    await plugin.on_load(ctx)
    try:
        assert extract_query_ids_from_js(chunk)["HomeLatestTimeline"] == "KmJCeZ__ef1fNos7tKN_fg"
        refreshed = await plugin.client.refresh_query_ids_from_bundle(
            plugin.auth,
            operations=("HomeLatestTimeline",),
        )
        assert refreshed is True
        assert plugin.client.query_id("HomeLatestTimeline") == "KmJCeZ__ef1fNos7tKN_fg"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-x")
