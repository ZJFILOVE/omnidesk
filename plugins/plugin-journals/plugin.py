"""OmniDesk v2 plugin-journals: arXiv, Nature, bioRxiv, PubMed, and citations."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import (
    BasePlugin,
    CapabilityResult,
    PluginContext,
    TextArticle,
    TextChannel,
    TextFeedItem,
    TextSource,
    WorkspaceTabSpec,
)

if __package__:
    from .journals_client import (
        JournalClient,
        JournalError,
        entry_to_article,
        entry_to_feed_item,
    )
    from .journals_translate import translate_article, translate_feed_items
else:
    _PLUGIN_DIR = str(Path(__file__).resolve().parent)
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from journals_client import (  # type: ignore[no-redef]
        JournalClient,
        JournalError,
        entry_to_article,
        entry_to_feed_item,
    )
    from journals_translate import translate_article, translate_feed_items  # type: ignore[no-redef]

_TEXT_ACTIONS = (
    "channels",
    "fetch_feed",
    "search",
    "fetch_article",
    "lookup_doi",
    "fetch_citation",
    "reload_config",
)
_CITATION_ACTIONS = ("lookup", "resolve", "fetch_citation")


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


class JournalTextSource(TextSource):
    """TextSource adapter exposing the enabled academic channels."""

    source_id = "plugin-journals"
    display_name = "学术期刊与预印本"
    category = "journals"

    def __init__(self, owner: JournalPlugin) -> None:
        self._owner = owner

    def channels(self) -> list[TextChannel]:
        self._owner.apply_config()
        return [
            TextChannel(id=channel_id, name=name, badge=source.upper(), count=0)
            for channel_id, name, source in self._owner.client.channel_specs()
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


class JournalPlugin(BasePlugin):
    """Scientific literature provider with DOI citation lookup."""

    def __init__(self, *, http_client: Any | None = None) -> None:
        self.client = JournalClient(http=http_client)
        self.text_source = JournalTextSource(self)

    async def on_load(self, ctx: PluginContext) -> None:
        await super().on_load(ctx)
        self.client.set_context(ctx)
        self.apply_config()
        ctx.register_text_source(self.text_source)
        ctx.register_capability(
            "text.source.v1",
            self.handle_text_source_rpc,
            metadata={"name": "学术期刊与预印本", "version": "0.2.0", "actions": list(_TEXT_ACTIONS)},
        )
        ctx.register_capability(
            "text.citation.v1",
            self.handle_citation_rpc,
            metadata={"name": "DOI / Crossref 引用查询", "version": "0.2.0", "actions": list(_CITATION_ACTIONS)},
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
            "plugin_id": ctx.plugin_id if ctx is not None else "plugin-journals",
            "config_error": error,
            "enabled_sources": list(self.client.enabled_sources),
            "llm_translate_available": bool(ctx and ctx.capabilities.has("llm.translate.v1")),
            "llm_chat_available": bool(ctx and ctx.capabilities.has("llm.chat.v1")),
            "chrome_extract_available": bool(ctx and ctx.capabilities.has("web.extract.v1")),
        }

    def apply_config(self) -> None:
        if self.ctx is None:
            self.client.configure({})
            return
        config = self.ctx.get_config()
        self.client.configure(config if isinstance(config, Mapping) else {})

    def _auto_translate(self) -> bool:
        if self.ctx is None:
            return False
        return _coerce_bool(self.ctx.get_config("prefer_bilingual_abstract", True), True)

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
        self.apply_config()
        entries = await self.client.fetch_feed(
            channel_id=channel_id,
            query=query,
            page=page,
            force_refresh=force_refresh,
        )
        items = [entry_to_feed_item(entry) for entry in entries]
        if channel_id:
            items = [item.model_copy(update={"channel_id": channel_id}) for item in items]
        if self._auto_translate():
            items = await translate_feed_items(self.ctx, items, target_lang=self._target_lang())
        return items

    async def fetch_article(self, article_id: str, *, channel_id: str = "") -> TextArticle:
        self.apply_config()
        entry = await self.client.fetch_article(article_id)
        article = entry_to_article(entry, channel_id=channel_id)
        if self._auto_translate():
            article = await translate_article(self.ctx, article, target_lang=self._target_lang())
        return article

    async def lookup_doi(self, doi: str) -> TextArticle:
        self.apply_config()
        entry = await self.client.lookup_doi(doi)
        article = entry_to_article(entry)
        if self._auto_translate():
            article = await translate_article(self.ctx, article, target_lang=self._target_lang())
        return article

    async def handle_text_source_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        payload = dict(req) if isinstance(req, Mapping) else {}
        action = str(payload.get("action") or "channels").strip()
        try:
            self.apply_config()
            if action == "channels":
                return CapabilityResult.ok(
                    {
                        "channels": [channel.model_dump() for channel in self.text_source.channels()],
                        "enabled_sources": list(self.client.enabled_sources),
                    }
                )
            if action == "reload_config":
                return CapabilityResult.ok(
                    {
                        "channels": [channel.model_dump() for channel in self.text_source.channels()],
                        "enabled_sources": list(self.client.enabled_sources),
                    }
                )
            if action in {"fetch_feed", "search"}:
                channel_id = _first_str(payload, ("channel_id", "channel", "source"))
                query = _first_str(payload, ("query", "q", "keyword"))
                page_value = payload.get("page", 1)
                page = max(1, int(page_value)) if isinstance(page_value, int) and not isinstance(page_value, bool) else 1
                items = await self.fetch_feed(
                    channel_id=channel_id,
                    query=query,
                    page=page,
                    force_refresh=_coerce_bool(payload.get("force_refresh", False), False),
                )
                return CapabilityResult.ok(
                    {
                        "items": [item.model_dump() for item in items],
                        "count": len(items),
                        "channel_id": channel_id or (items[0].channel_id if items else ""),
                        "page": page,
                    }
                )
            if action in {"fetch_article", "article"}:
                article_id = _first_str(payload, ("article_id", "id", "identifier", "url", "doi"))
                if not article_id:
                    return self._fail("invalid_argument", "article_id 或 url 不能为空")
                channel_id = _first_str(payload, ("channel_id", "channel"))
                article = await self.fetch_article(article_id, channel_id=channel_id)
                return CapabilityResult.ok({"article": article.model_dump(), "id": article.id})
            if action in {"lookup_doi", "fetch_citation"}:
                doi = _first_str(payload, ("doi", "identifier", "id", "query"))
                if not doi:
                    return self._fail("invalid_argument", "doi 不能为空")
                return await self.handle_citation_rpc({"action": "lookup", "doi": doi})
            return self._fail("invalid_argument", f"不支持 text.source.v1 动作: {action}")
        except JournalError as exc:
            return self._fail(exc.code, exc.message, http_status=exc.http_status)
        except Exception as exc:
            return self._fail("service_offline", f"学术数据源请求失败: {exc}")

    async def handle_citation_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        payload = dict(req) if isinstance(req, Mapping) else {}
        action = str(payload.get("action") or "lookup").strip()
        if action not in _CITATION_ACTIONS:
            return self._fail("invalid_argument", f"不支持 text.citation.v1 动作: {action}")
        doi = _first_str(payload, ("doi", "identifier", "id", "query"))
        if not doi:
            return self._fail("invalid_argument", "doi 不能为空")
        try:
            self.apply_config()
            entry = await self.client.lookup_doi(doi)
            item = entry_to_feed_item(entry)
            article = entry_to_article(entry)
            authors = entry.author.split(",") if entry.author else []
            citation = {
                "doi": entry.doi,
                "title": entry.title,
                "authors": [author.strip() for author in authors if author.strip()],
                "journal": entry.journal,
                "published_at": entry.published_at,
                "url": entry.url,
                "plain": self._format_citation(entry),
            }
            return CapabilityResult.ok(
                {
                    "citation": citation,
                    "item": item.model_dump(),
                    "article": article.model_dump(),
                }
            )
        except JournalError as exc:
            return self._fail(exc.code, exc.message, http_status=exc.http_status)
        except Exception as exc:
            return self._fail("service_offline", f"DOI 引用查询失败: {exc}")

    @staticmethod
    def _format_citation(entry: Any) -> str:
        parts = [entry.author or "Unknown authors"]
        if entry.published_at:
            parts.append(f"({entry.published_at})")
        parts.append(entry.title)
        if entry.journal:
            parts.append(entry.journal)
        if entry.doi:
            parts.append(f"https://doi.org/{entry.doi}")
        return ". ".join(part for part in parts if part)

    def _fail(self, code: str, message: str, **extra: Any) -> CapabilityResult:
        return CapabilityResult.fail(message, data={"error_code": code, **extra}, error_code=code)

    def _build_workspace_tab(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="journals",
            title="学术期刊",
            domain="text",
            badge="ARXIV",
            ui_schema={
                "type": "Section",
                "id": "journals-root",
                "title": "学术期刊与预印本",
                "children": [
                    {
                        "type": "Form",
                        "id": "journals-search",
                        "fields": [
                            {"name": "channel_id", "label": "数据源", "type": "string", "default": "arxiv"},
                            {"name": "query", "label": "检索词 / PubMed 检索式", "type": "string", "default": ""},
                        ],
                    },
                    {
                        "type": "ActionRow",
                        "id": "journals-actions",
                        "actions": [
                            {"id": "fetch_feed", "label": "刷新论文", "variant": "primary"},
                            {"id": "channels", "label": "数据源", "variant": "secondary"},
                        ],
                    },
                ],
            },
        )


def create_plugin() -> BasePlugin:
    """Manifest entrypoint."""
    return JournalPlugin()
