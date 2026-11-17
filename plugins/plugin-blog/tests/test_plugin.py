"""Offline tests for blog subscriptions, feed discovery, and article extraction."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from omnidesk_sdk import PluginContext  # noqa: E402
from omnidesk_sdk.http import HttpResponse  # noqa: E402
from blog_store import BlogSubscriptionStore  # noqa: E402
from plugin import BlogPlugin, create_plugin  # noqa: E402

BLOG_HOME = """<!doctype html>
<html><head>
<title>Example Blog</title>
<meta property="og:title" content="Example Blog"/>
<link rel="alternate" type="application/rss+xml" href="/feed.xml"/>
</head><body><h1>Example Blog</h1></body></html>
"""

BLOG_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Example Blog</title>
<link>https://blog.example/</link>
<item>
  <title>First post</title>
  <link>https://blog.example/posts/one</link>
  <guid>https://blog.example/posts/one</guid>
  <description>First summary</description>
  <pubDate>Mon, 17 Nov 2026 08:00:00 GMT</pubDate>
  <author>Alice</author>
  <category>AI</category>
</item>
<item>
  <title>Second post</title>
  <link>https://blog.example/posts/two</link>
  <guid>https://blog.example/posts/two</guid>
  <description>Second summary</description>
  <pubDate>Sun, 16 Nov 2026 08:00:00 GMT</pubDate>
</item>
</channel></rss>
"""

PLAIN_HOME = """<!doctype html>
<html><head><title>Plain Blog</title></head><body>
<article><a href="/posts/two">Plain post</a></article>
</body></html>
"""

POST_HTML = """<!doctype html>
<html><head>
<meta property="og:title" content="First post"/>
<meta property="og:site_name" content="Example Blog"/>
<meta property="article:published_time" content="2026-11-17T08:00:00Z"/>
<meta property="og:image" content="https://blog.example/cover.png"/>
</head><body><article>
<h1>First post</h1>
<p>First paragraph.</p>
<h2>Details</h2>
<p>Second paragraph with <code>inline code</code>.</p>
</article></body></html>
"""

PLAIN_POST_HTML = """<!doctype html>
<html><head><title>Plain post</title></head><body><article>
<h1>Plain post</h1><p>Plain article body.</p><p>Another paragraph.</p>
</article></body></html>
"""


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get(self, url: str, **kwargs: object) -> HttpResponse:
        self.calls.append(url)
        routes = {
            "https://blog.example/": (BLOG_HOME, "text/html; charset=utf-8"),
            "https://blog.example/feed.xml": (BLOG_FEED, "application/rss+xml"),
            "https://blog.example/posts/one": (POST_HTML, "text/html; charset=utf-8"),
            "https://plain.example/": (PLAIN_HOME, "text/html; charset=utf-8"),
            "https://plain.example/posts/two": (PLAIN_POST_HTML, "text/html; charset=utf-8"),
        }
        route = routes.get(url)
        if route is None:
            return HttpResponse(
                status_code=404,
                url=url,
                headers={"content-type": "text/plain"},
                content=b"not found",
            )
        body, content_type = route
        return HttpResponse(
            status_code=200,
            url=url,
            headers={"content-type": content_type},
            content=body.encode("utf-8"),
        )


@pytest.mark.asyncio
async def test_discover_subscribe_feed_and_read_article(tmp_path: Path) -> None:
    plugin = BlogPlugin(http_client=FakeHttp())
    ctx = PluginContext(
        "plugin-blog",
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )
    await plugin.on_load(ctx)
    try:
        assert ctx.capabilities.has("text.source.v1")
        assert ctx.capabilities.has("blog.subscription.v1")
        added = await plugin.handle_subscription_rpc(
            {"action": "add", "url": "https://blog.example/"}
        )
        assert added.ok
        sub = added.data["subscription"]
        assert sub["title"] == "Example Blog"
        assert sub["feed_url"] == "https://blog.example/feed.xml"

        channels = await plugin.handle_text_source_rpc({"action": "channels"})
        assert channels.ok
        assert channels.data["channels"][0]["id"] == sub["id"]

        feed = await plugin.handle_text_source_rpc({"action": "fetch_feed"})
        assert feed.ok
        assert feed.data["count"] == 2
        assert feed.data["items"][0]["title"] == "First post"
        item = feed.data["items"][0]
        assert item["channel_id"] == sub["id"]
        assert item["id"] == f"{sub['id']}::https://blog.example/posts/one"

        article = await plugin.handle_text_source_rpc(
            {"action": "fetch_article", "article_id": item["id"]}
        )
        assert article.ok
        payload = article.data["article"]
        assert payload["title"] == "First post"
        assert payload["cover_url"] == "https://blog.example/cover.png"
        assert [paragraph["kind"] for paragraph in payload["paragraphs"][:3]] == [
            "heading",
            "paragraph",
            "heading",
        ]
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-blog")


@pytest.mark.asyncio
async def test_homepage_fallback_without_feed(tmp_path: Path) -> None:
    plugin = BlogPlugin(http_client=FakeHttp())
    ctx = PluginContext(
        "plugin-blog",
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )
    await plugin.on_load(ctx)
    try:
        added = await plugin.handle_subscription_rpc(
            {"action": "add", "url": "https://plain.example/"}
        )
        assert added.ok
        sub = added.data["subscription"]
        assert sub["feed_url"] == ""
        assert sub["title"] == "Plain Blog"

        feed = await plugin.handle_text_source_rpc({"action": "fetch_feed"})
        assert feed.ok
        assert feed.data["count"] == 1
        assert feed.data["items"][0]["title"] == "Plain post"

        article = await plugin.fetch_article(feed.data["items"][0]["id"])
        assert article.title == "Plain post"
        assert article.paragraphs[0].original == "Plain post"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-blog")


@pytest.mark.asyncio
async def test_remove_and_persist_subscriptions(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = BlogSubscriptionStore(data_dir)
    first = store.add("https://blog.example/", title="Example", feed_url="https://blog.example/feed.xml")
    second = store.add("https://plain.example/", title="Plain")
    reloaded = BlogSubscriptionStore(data_dir)
    assert [item.id for item in reloaded.list()] == [first.id, second.id]
    assert reloaded.remove(first.id) is True
    assert [item.id for item in BlogSubscriptionStore(data_dir).list()] == [second.id]


@pytest.mark.asyncio
async def test_default_blogs_config_and_search(tmp_path: Path) -> None:
    plugin = BlogPlugin(http_client=FakeHttp())
    ctx = PluginContext(
        "plugin-blog",
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )
    await plugin.on_load(ctx)
    try:
        ctx.set_config("default_blogs", "https://blog.example/|Example Blog")
        result = await plugin.handle_text_source_rpc({"action": "search", "query": "second"})
        assert result.ok
        assert result.data["count"] == 1
        assert result.data["items"][0]["title"] == "Second post"
    finally:
        await plugin.on_unload()
        await ctx.acleanup("plugin-blog")


def test_create_plugin_returns_manifest_entrypoint() -> None:
    assert isinstance(create_plugin(), BlogPlugin)
