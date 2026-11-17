"""Offline tests for declarative feed recipe loading, feed mapping, and articles."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from http_client import HttpResponse  # noqa: E402
from omnidesk_sdk import PluginContext  # noqa: E402
from plugin import FeedRecipePlugin  # noqa: E402


class _Transport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def request(self, req, *, extra_secrets=(), allow_loopback_http=False, allowed_loopback_origins=()):  # type: ignore[no-untyped-def]
        del extra_secrets, allow_loopback_http, allowed_loopback_origins
        self.urls.append(req.url)
        if req.url.endswith("/feed.json"):
            body = json.dumps(
                {
                    "items": [
                        {
                            "id": "post-1",
                            "title": "First post",
                            "url": "https://example.com/post/1",
                            "author": "Alice",
                            "created_at": "2026-11-17",
                            "summary": "A short summary",
                            "points": 12,
                            "comments": 2,
                            "tags": ["test", "demo"],
                        }
                    ]
                }
            )
        else:
            body = json.dumps(
                {
                    "title": "First post",
                    "author": "Alice",
                    "published_at": "2026-11-17",
                    "content": "Hello recipe world.",
                    "comments": [
                        {"id": "c1", "author": "Bob", "body": "Nice post", "score": 3},
                    ],
                }
            )
        return HttpResponse(
            status_code=200,
            effective_url=req.url,
            headers={"content-type": "application/json"},
            body=body,
        )


def _write_recipe(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "id": "demo",
                "name": "Demo Feed",
                "format": "json",
                "feed": {
                    "url": "https://example.com/feed.json",
                    "items_path": "items",
                    "fields": {
                        "id": "id",
                        "title": "title",
                        "url": "url",
                        "author": "author",
                        "published_at": "created_at",
                        "excerpt": "summary",
                        "score": "points",
                        "comment_count": "comments",
                        "tags": "tags",
                    },
                },
                "article": {
                    "url": "{{url}}",
                    "mode": "json",
                    "comments": {
                        "path": "comments",
                        "id": "id",
                        "author": "author",
                        "body": "body",
                        "score": "score",
                    },
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_lifecycle_feed_and_article(tmp_path: Path) -> None:
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    _write_recipe(recipes_dir / "demo.json")
    transport = _Transport()
    plugin = FeedRecipePlugin(transport=transport, recipes_dir=recipes_dir)
    ctx = PluginContext("plugin-feed-recipe")
    await plugin.on_load(ctx)
    try:
        assert ctx.capabilities.has("text.source.v1")
        assert ctx.capabilities.has("text.comments.v1")
        assert ctx.capabilities.has("feed.recipe.v1")

        feed = await plugin.handle_text_source_rpc(
            {"action": "fetch_feed", "recipe_id": "demo", "force_refresh": True}
        )
        assert feed.ok is True
        item = feed.data["items"][0]
        assert item["id"] == "post-1"
        assert item["title"] == "First post"
        assert item["tags"] == ["test", "demo"]
        assert item["comment_count"] == 2
        assert item["extra"]["recipe_id"] == "demo"

        article = await plugin.handle_text_source_rpc(
            {
                "action": "fetch_article",
                "recipe_id": "demo",
                "article_id": "demo|https://example.com/post/1",
            }
        )
        assert article.ok is True
        assert article.data["article"]["title"] == "First post"
        assert article.data["article"]["comments"][0]["body"] == "Nice post"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-feed-recipe")


@pytest.mark.asyncio
async def test_missing_recipe_fails_closed(tmp_path: Path) -> None:
    plugin = FeedRecipePlugin(transport=_Transport(), recipes_dir=tmp_path)
    ctx = PluginContext("plugin-feed-recipe")
    await plugin.on_load(ctx)
    try:
        result = await plugin.handle_text_source_rpc(
            {"action": "fetch_feed", "recipe_id": "missing", "force_refresh": True}
        )
        assert result.ok is False
        assert result.data["error_code"] == "not_found"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-feed-recipe")


@pytest.mark.asyncio
async def test_comments_capability_returns_tree(tmp_path: Path) -> None:
    _write_recipe(tmp_path / "demo.json")
    plugin = FeedRecipePlugin(transport=_Transport(), recipes_dir=tmp_path)
    ctx = PluginContext("plugin-feed-recipe")
    await plugin.on_load(ctx)
    try:
        result = await plugin.handle_comments_rpc(
            {"article_id": "demo|https://example.com/post/1"}
        )
        assert result.ok is True
        assert result.data["count"] == 1
        assert result.data["comments"][0]["author"] == "Bob"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-feed-recipe")
