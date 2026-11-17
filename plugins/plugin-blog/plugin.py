"""OmniDesk v2 plugin-blog: any-blog subscriptions, feeds, articles, and bilingual reading."""

from __future__ import annotations

import asyncio
import sys
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from omnidesk_sdk import (
    BasePlugin,
    CapabilityResult,
    PluginContext,
    TextArticle,
    TextChannel,
    TextFeedItem,
    TextParagraph,
    TextSource,
    WorkspaceTabSpec,
)

if __package__:
    from .blog_client import BlogClient, BlogError
    from .blog_parser import BlogArticle, BlogFeedEntry
    from .blog_store import BlogStoreError, BlogSubscriptionStore, normalize_blog_url
    from .blog_translate import translate_article, translate_feed_items
else:
    _PLUGIN_DIR = str(Path(__file__).resolve().parent)
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from blog_client import BlogClient, BlogError  # type: ignore[no-redef]
    from blog_parser import BlogArticle, BlogFeedEntry  # type: ignore[no-redef]
    from blog_store import BlogStoreError, BlogSubscriptionStore, normalize_blog_url  # type: ignore[no-redef]
    from blog_translate import translate_article, translate_feed_items  # type: ignore[no-redef]

_TEXT_ACTIONS = (
    "channels",
    "fetch_feed",
    "search",
    "fetch_article",
    "list_subscriptions",
    "add_subscription",
    "remove_subscription",
    "refresh_subscription",
    "discover",
    "reload_config",
)
_SUBSCRIPTION_ACTIONS = (
    "list",
    "add",
    "remove",
    "refresh",
    "discover",
)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0
    return default


def _first_str(req: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = req.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class BlogTextSource(TextSource):
    """TextSource adapter exposing saved blogs as channels."""

    source_id = "plugin-blog"
    display_name = "个人博客订阅"
    category = "blog"

    def __init__(self, owner: BlogPlugin) -> None:
        self._owner = owner

    def channels(self) -> list[TextChannel]:
        self._owner.apply_config()
        subscriptions = self._owner.store.list()
        return [
            TextChannel(
                id=sub.id,
                name=sub.title or urllib.parse.urlsplit(sub.url).hostname or sub.url,
                badge="RSS" if sub.feed_url else "WEB",
                count=0,
            )
            for sub in subscriptions
        ]

    async def fetch_feed(
        self,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[TextFeedItem]:
        return await self._owner.fetch_feed(
            channel_id=channel_id,
            query=query,
            page=page,
            force_refresh=force_refresh,
        )

    async def fetch_article(self, article_id: str) -> TextArticle:
        return await self._owner.fetch_article(article_id)


class BlogPlugin(BasePlugin):
    """Persisted blog subscriptions with feed discovery and Chrome fallback."""

    def __init__(self, *, http_client: Any | None = None) -> None:
        self.store = BlogSubscriptionStore()
        self.client = BlogClient(http=http_client)
        self.text_source = BlogTextSource(self)

    async def on_load(self, ctx: PluginContext) -> None:
        await super().on_load(ctx)
        self.store.set_data_dir(ctx.data_dir)
        self.client.set_context(ctx)
        self.apply_config()
        ctx.register_text_source(self.text_source)
        ctx.register_capability(
            "text.source.v1",
            self.handle_text_source_rpc,
            metadata={"name": "个人博客订阅", "version": "0.2.0", "actions": list(_TEXT_ACTIONS)},
        )
        ctx.register_capability(
            "blog.subscription.v1",
            self.handle_subscription_rpc,
            metadata={
                "name": "博客订阅管理",
                "version": "0.2.0",
                "actions": list(_SUBSCRIPTION_ACTIONS),
            },
        )
        ctx.register_workspace_tab(self._build_workspace_tab())

    async def on_unload(self) -> None:
        self.client.set_context(None)
        self.ctx = None

    async def on_health_check(self) -> dict[str, Any]:
        error = ""
        try:
            self.apply_config()
        except Exception as exc:
            error = str(exc)
        ctx = self.ctx
        return {
            "ok": not error,
            "plugin_id": ctx.plugin_id if ctx is not None else "plugin-blog",
            "config_error": error,
            "subscriptions": len(self.store.list()),
            "llm_translate_available": bool(ctx and ctx.capabilities.has("llm.translate.v1")),
            "llm_chat_available": bool(ctx and ctx.capabilities.has("llm.chat.v1")),
            "chrome_extract_available": bool(ctx and ctx.capabilities.has("web.extract.v1")),
            "cookie_capability_available": bool(ctx and ctx.capabilities.has("web.cookies.v1")),
        }

    def apply_config(self) -> None:
        if self.ctx is None:
            self.client.configure({})
            return
        config = self.ctx.get_config()
        values = config if isinstance(config, Mapping) else {}
        self.client.configure(values)
        default_blogs = str(values.get("default_blogs") or "").strip()
        if default_blogs:
            self.store.apply_default_spec(default_blogs)

    def _auto_translate(self) -> bool:
        if self.ctx is None:
            return False
        return _coerce_bool(self.ctx.get_config("auto_translate", True), True)

    def _target_lang(self) -> str:
        if self.ctx is None:
            return "zh-CN"
        return str(self.ctx.get_config("target_lang", "zh-CN") or "zh-CN")

    async def fetch_feed(
        self,
        *,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
        force_refresh: bool = False,
    ) -> list[TextFeedItem]:
        del force_refresh
        self.apply_config()
        subscriptions = self.store.list()
        target = (channel_id or "").strip()
        if target and target not in {"all", "*"}:
            selected = [sub for sub in subscriptions if sub.id == target]
            if not selected:
                raise BlogError("not_found", f"未找到博客订阅 {target!r}")
        else:
            selected = subscriptions
        if not selected:
            return []

        results = await asyncio.gather(
            *(self.client.fetch_feed(sub) for sub in selected),
            return_exceptions=True,
        )
        items: list[TextFeedItem] = []
        first_error: Exception | None = None
        for sub, result in zip(selected, results):
            if isinstance(result, Exception):
                first_error = first_error or result
                sub.last_error = str(result)
            else:
                sub.last_error = ""
                items.extend(self._entry_to_item(entry, sub) for entry in result)
            sub.last_checked = _now()
            self.store.update(sub)
        if not items and first_error is not None:
            raise first_error

        if query:
            needle = query.casefold()
            items = [
                item
                for item in items
                if needle in item.title.casefold()
                or needle in item.excerpt.casefold()
                or needle in item.author.casefold()
            ]
        items.sort(key=lambda item: _published_sort_key(item.published_at), reverse=True)
        page = max(1, page)
        page_size = max(1, self.client.max_articles_per_feed)
        start = (page - 1) * page_size
        items = items[start : start + page_size]
        if self._auto_translate():
            items = await translate_feed_items(self.ctx, items, target_lang=self._target_lang())
        return items

    async def fetch_article(self, article_id: str, *, channel_id: str = "") -> TextArticle:
        self.apply_config()
        subscriptions = self.store.list()
        target = (article_id or "").strip()
        if not target:
            raise BlogError("invalid_argument", "article_id 不能为空")
        sub = None
        url = target
        if "::" in target:
            sub_id, url = target.split("::", 1)
            sub = self.store.get(sub_id)
        elif channel_id:
            sub = self.store.get(channel_id)
        if sub is None:
            sub = self.client.match_subscription(subscriptions, url=url)
        if sub is None and subscriptions:
            sub = subscriptions[0]
        if sub is None:
            raise BlogError("not_found", "请先添加至少一个博客订阅")
        article = await self.client.fetch_article(sub, url)
        mapped = self._article_to_text(article, sub.id)
        if self._auto_translate():
            mapped = await translate_article(self.ctx, mapped, target_lang=self._target_lang())
        return mapped

    async def add_subscription(
        self,
        url: str,
        *,
        title: str = "",
    ) -> Any:
        self.apply_config()
        try:
            discovered = await self.client.discover(url)
        except BlogError as exc:
            if exc.code not in {"challenge_detected", "upstream_status", "service_offline", "invalid_response"}:
                raise
            normalized = normalize_blog_url(url)
            return self.store.add(
                normalized,
                title=title or urllib.parse.urlsplit(normalized).hostname or normalized,
            )
        return self.store.add(
            discovered.url,
            title=title or discovered.title,
            feed_url=discovered.feed_url,
            site_url=discovered.site_url,
        )

    async def handle_text_source_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        payload = dict(req) if isinstance(req, Mapping) else {}
        action = str(payload.get("action") or "channels").strip()
        try:
            self.apply_config()
            if action in {"channels", "list_subscriptions", "reload_config"}:
                return self._channels_result()
            if action == "discover":
                url = _first_str(payload, ("url", "blog_url", "query"))
                if not url:
                    return self._fail("invalid_argument", "url 不能为空")
                discovered = await self.client.discover(url)
                return CapabilityResult.ok({"subscription": discovered.to_dict()})
            if action == "add_subscription":
                url = _first_str(payload, ("url", "blog_url", "value"))
                if not url:
                    return self._fail("invalid_argument", "url 不能为空")
                sub = await self.add_subscription(url, title=_first_str(payload, ("title", "name")))
                return CapabilityResult.ok({"subscription": sub.to_dict()})
            if action == "remove_subscription":
                identifier = _first_str(payload, ("id", "subscription_id", "url", "blog_id"))
                removed = self.store.remove(identifier)
                if not removed:
                    return self._fail("not_found", f"未找到博客订阅 {identifier!r}")
                return CapabilityResult.ok({"removed": True, "id": identifier})
            if action == "refresh_subscription":
                identifier = _first_str(payload, ("id", "subscription_id", "url", "blog_id"))
                sub = self.store.get(identifier)
                if sub is None:
                    return self._fail("not_found", f"未找到博客订阅 {identifier!r}")
                entries = await self.client.fetch_feed(sub)
                sub.last_checked = _now()
                sub.last_error = ""
                self.store.update(sub)
                return CapabilityResult.ok(
                    {
                        "subscription": sub.to_dict(),
                        "items": [self._entry_to_item(entry, sub).model_dump() for entry in entries],
                        "count": len(entries),
                    }
                )
            if action in {"fetch_feed", "search"}:
                channel_id = _first_str(payload, ("channel_id", "channel", "blog_id"))
                query = _first_str(payload, ("query", "q", "keyword"))
                page_value = payload.get("page", 1)
                page = max(1, page_value) if isinstance(page_value, int) and not isinstance(page_value, bool) else 1
                items = await self.fetch_feed(channel_id=channel_id, query=query, page=page)
                return CapabilityResult.ok(
                    {
                        "items": [item.model_dump() for item in items],
                        "count": len(items),
                        "channel_id": channel_id,
                        "page": page,
                    }
                )
            if action in {"fetch_article", "article"}:
                article_id = _first_str(payload, ("article_id", "id", "url"))
                if not article_id:
                    return self._fail("invalid_argument", "article_id 或 url 不能为空")
                article = await self.fetch_article(
                    article_id,
                    channel_id=_first_str(payload, ("channel_id", "blog_id")),
                )
                return CapabilityResult.ok({"article": article.model_dump(), "id": article.id})
            return self._fail("invalid_argument", f"不支持 text.source.v1 动作: {action}")
        except BlogStoreError as exc:
            return self._fail(exc.code, str(exc))
        except BlogError as exc:
            return self._fail(exc.code, exc.message, http_status=exc.http_status)
        except Exception as exc:
            return self._fail("service_offline", f"博客操作失败: {exc}")

    async def handle_subscription_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        payload = dict(req) if isinstance(req, Mapping) else {}
        action = str(payload.get("action") or "list").strip()
        if action == "list":
            return self._channels_result()
        mapped = {
            "add": "add_subscription",
            "remove": "remove_subscription",
            "refresh": "refresh_subscription",
            "discover": "discover",
        }.get(action)
        if mapped is None:
            return self._fail("invalid_argument", f"不支持 blog.subscription.v1 动作: {action}")
        return await self.handle_text_source_rpc({**payload, "action": mapped})

    def _channels_result(self) -> CapabilityResult:
        return CapabilityResult.ok(
            {
                "channels": [channel.model_dump() for channel in self.text_source.channels()],
                "subscriptions": [sub.to_dict() for sub in self.store.list()],
            }
        )

    @staticmethod
    def _entry_to_item(entry: BlogFeedEntry, sub: Any) -> TextFeedItem:
        return TextFeedItem(
            id=entry.id,
            source_id="plugin-blog",
            source_name=sub.title,
            category="blog",
            channel_id=sub.id,
            title=entry.title,
            excerpt=entry.summary[:500],
            author=entry.author,
            published_at=entry.published_at,
            url=entry.url,
            cover_url=entry.cover_url,
            tags=entry.tags,
            extra={"blog_id": sub.id, "blog_title": sub.title},
        )

    @staticmethod
    def _article_to_text(article: BlogArticle, channel_id: str) -> TextArticle:
        paragraphs = [
            TextParagraph(
                id=f"p{index + 1}",
                kind=kind,
                original=text,
            )
            for index, (kind, text) in enumerate(article.paragraphs)
            if text.strip()
        ]
        words = sum(len(paragraph.original.split()) for paragraph in paragraphs)
        return TextArticle(
            id=article.url,
            source_id="plugin-blog",
            source_name=article.site_name or "Blog",
            category="blog",
            channel_id=channel_id,
            title=article.title,
            authors=article.author,
            author=article.author,
            published_at=article.published_at,
            read_time=f"{max(1, round(words / 220))} min",
            url=article.url,
            cover_url=article.cover_url,
            tags=article.tags,
            paragraphs=paragraphs,
            extra={"site_name": article.site_name, "summary": article.summary},
        )

    def _fail(self, code: str, message: str, **extra: Any) -> CapabilityResult:
        return CapabilityResult.fail(message, data={"error_code": code, **extra}, error_code=code)

    def _build_workspace_tab(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="blog",
            title="博客订阅",
            domain="text",
            badge="BLOG",
            ui_schema={
                "type": "Section",
                "id": "blog-root",
                "title": "个人博客订阅",
                "children": [
                    {
                        "type": "Form",
                        "id": "blog-add",
                        "fields": [
                            {"name": "url", "label": "博客地址", "type": "string", "default": ""},
                            {"name": "title", "label": "显示名称（可选）", "type": "string", "default": ""},
                        ],
                    },
                    {
                        "type": "ActionRow",
                        "id": "blog-actions",
                        "actions": [
                            {"id": "add_subscription", "label": "添加订阅", "variant": "primary"},
                            {"id": "fetch_feed", "label": "刷新", "variant": "secondary"},
                            {"id": "list_subscriptions", "label": "订阅列表", "variant": "secondary"},
                        ],
                    },
                ],
            },
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _published_sort_key(value: str) -> float:
    raw = (value or "").strip()
    if not raw:
        return 0.0
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


def create_plugin() -> BasePlugin:
    """Manifest entrypoint."""
    return BlogPlugin()
