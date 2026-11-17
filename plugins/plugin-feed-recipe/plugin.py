"""OmniDesk v2 plugin-feed-recipe: declarative JSON/Lua feed and article engine."""

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
    from .http_client import HttpTransport
    from .recipe_engine import RecipeEngine
    from .security import FeedRecipeError, redact_sensitive
else:
    _PLUGIN_DIR = str(Path(__file__).resolve().parent)
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from http_client import HttpTransport  # type: ignore[no-redef]
    from recipe_engine import RecipeEngine  # type: ignore[no-redef]
    from security import FeedRecipeError, redact_sensitive  # type: ignore[no-redef]

_TEXT_SOURCE_ACTIONS = (
    "channels",
    "list_recipes",
    "get_recipe",
    "reload_recipes",
    "fetch_feed",
    "fetch_article",
    "translate_article",
)
_RECIPE_ACTIONS = ("list", "get", "reload", "fetch_feed", "fetch_article")


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0
    return default


def _coerce_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _coerce_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _str_chain(req: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = req.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class FeedRecipeTextSource(TextSource):
    """TextSource exposing every loaded declarative recipe as a channel."""

    source_id = "plugin-feed-recipe"
    display_name = "声明式源配方"
    category = "blog"

    def __init__(self, owner: FeedRecipePlugin) -> None:
        self._owner = owner
        self.last_error: dict[str, Any] | None = None

    def channels(self) -> list[TextChannel]:
        self._owner.apply_config()
        return [
            TextChannel(
                id=spec.id,
                name=spec.name,
                badge=spec.format.upper(),
                requires_auth=spec.use_chrome_cookies,
            )
            for spec in self._owner.engine.recipes.values()
        ]

    async def fetch_feed(
        self,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[TextFeedItem]:
        self.last_error = None
        self._owner.apply_config()
        try:
            page_result = await self._owner.engine.fetch_feed(
                channel_id,
                query=query,
                page=page,
                force_refresh=force_refresh,
            )
            return page_result.items
        except FeedRecipeError as exc:
            self.last_error = {"error_code": exc.error_code, "error": str(exc)}
            raise

    async def fetch_article(self, article_id: str) -> TextArticle:
        self.last_error = None
        self._owner.apply_config()
        try:
            recipe_id, ref = self._owner.split_article_ref(article_id, "")
            return await self._owner.engine.fetch_article(recipe_id, ref)
        except FeedRecipeError as exc:
            self.last_error = {"error_code": exc.error_code, "error": str(exc)}
            raise


class FeedRecipePlugin(BasePlugin):
    """OmniDesk v2 declarative feed recipe runtime."""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        recipes_dir: str | Path | None = None,
        builtin_dir: str | Path | None = None,
        allow_loopback_http: bool = False,
    ) -> None:
        self._recipes_dir_override = str(recipes_dir) if recipes_dir else ""
        self._builtin_dir = Path(builtin_dir) if builtin_dir else None
        self._allow_loopback_http = bool(allow_loopback_http)
        self.engine = RecipeEngine(
            transport=transport,
            allow_loopback_http=self._allow_loopback_http,
            builtin_dir=self._builtin_dir,
        )
        self.text_source = FeedRecipeTextSource(self)
        self._configured_dir = ""

    async def on_load(self, ctx: PluginContext) -> None:
        await super().on_load(ctx)
        self.engine.set_context(ctx)
        self.apply_config()
        ctx.register_text_source(self.text_source)
        ctx.register_capability(
            "text.source.v1",
            self.handle_text_source_rpc,
            metadata={
                "name": "声明式源配方引擎",
                "version": "0.2.0",
                "actions": list(_TEXT_SOURCE_ACTIONS),
            },
        )
        ctx.register_capability(
            "text.comments.v1",
            self.handle_comments_rpc,
            metadata={"name": "声明式评论树", "version": "0.2.0", "actions": ["fetch_comments"]},
        )
        ctx.register_capability(
            "feed.recipe.v1",
            self.handle_recipe_rpc,
            metadata={"name": "Feed Recipe Manager", "version": "0.2.0", "actions": list(_RECIPE_ACTIONS)},
        )
        ctx.register_workspace_tab(self._build_workspace_tab_spec())

    async def on_unload(self) -> None:
        self.engine.set_context(None)
        self.ctx = None

    async def on_health_check(self) -> dict[str, Any]:
        config_error = ""
        try:
            self.apply_config()
        except FeedRecipeError as exc:
            config_error = str(exc)
        ctx = self.ctx
        return {
            "ok": not config_error,
            "plugin_id": ctx.plugin_id if ctx is not None else "plugin-feed-recipe",
            "config_error": config_error,
            "recipes_count": len(self.engine.recipes),
            "recipes_dir": self.engine._loaded_dir,
            "llm_translate_available": bool(ctx and ctx.capabilities.has("llm.translate.v1")),
            "llm_chat_available": bool(ctx and ctx.capabilities.has("llm.chat.v1")),
        }

    def apply_config(self) -> None:
        if self.ctx is None:
            self.engine.load_recipes(self._recipes_dir_override or None)
            return
        cfg = self.ctx.get_config()
        recipes_dir = self._recipes_dir_override or str(cfg.get("recipes_dir") or "").strip()
        self.engine.configure(
            cookie_mode=str(cfg.get("chrome_cookie_source") or "auto"),
            cookie_file_path=str(cfg.get("cookie_file_path") or ""),
            vault_cookie_header=str(self.ctx.get_secret("custom_cookie_header") or ""),
            timeout_sec=_coerce_float(cfg.get("request_timeout_sec", 15), 15.0),
            js_fallback=_coerce_bool(cfg.get("js_fallback", True), True),
            auto_translate=_coerce_bool(cfg.get("auto_translate", True), True),
            target_lang=str(cfg.get("target_lang") or "zh-CN"),
            cache_ttl_sec=_coerce_int(cfg.get("cache_ttl_sec", 300), 300),
            cache_dir=(self.ctx.data_dir / "translation-cache") if self.ctx.data_dir else None,
        )
        if recipes_dir != self._configured_dir:
            self._configured_dir = recipes_dir
            self.engine.load_recipes(recipes_dir or None, force=True)

    @staticmethod
    def split_article_ref(article_id: str, fallback_recipe: str) -> tuple[str, str]:
        raw = (article_id or "").strip()
        if "|" in raw:
            recipe_id, ref = raw.split("|", 1)
            return recipe_id.strip(), ref.strip()
        return (fallback_recipe or "").strip(), raw

    def _fail(
        self,
        code: str,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> CapabilityResult:
        details = dict(data or {})
        payload = {
            "ok": False,
            "error_code": code,
            "error": redact_sensitive(message, self._secrets()),
            **details,
        }
        return CapabilityResult.fail(str(payload["error"]), data=payload, error_code=code)

    def _secrets(self) -> tuple[str, ...]:
        return (
            self.engine.vault_cookie_header,
            self.engine.cookie_file_path,
        )

    async def handle_text_source_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        try:
            self.apply_config()
            action = str(req_obj.get("action") or "channels").strip()
            recipe_id = _str_chain(req_obj, ("recipe_id", "channel_id", "id"))
            if action in {"channels", "list_recipes"}:
                return CapabilityResult.ok(
                    {
                        "channels": [channel.model_dump() for channel in self.text_source.channels()],
                        "recipes": self.engine.recipe_dicts(),
                    }
                )
            if action == "get_recipe":
                spec = self.engine.recipe(recipe_id)
                return CapabilityResult.ok({"recipe": self.engine.recipe_dicts()[list(self.engine.recipes).index(spec.id)]})
            if action == "reload_recipes":
                self.engine.load_recipes(self._configured_dir or None, force=True)
                return CapabilityResult.ok({"recipes": self.engine.recipe_dicts()})
            if action in {"fetch_feed", "feed"}:
                page = max(1, _coerce_int(req_obj.get("page", 1), 1))
                result = await self.engine.fetch_feed(
                    recipe_id,
                    query=str(req_obj.get("query") or ""),
                    page=page,
                    force_refresh=_coerce_bool(req_obj.get("force_refresh", False), False),
                )
                return CapabilityResult.ok(
                    {
                        "items": [item.model_dump() for item in result.items],
                        "count": len(result.items),
                        "recipe_id": recipe_id or (result.items[0].channel_id if result.items else ""),
                        "effective_url": result.effective_url,
                        "cached": result.cached,
                    }
                )
            if action in {"fetch_article", "article", "translate_article"}:
                raw_article = _str_chain(req_obj, ("article_id", "url", "id"))
                if not raw_article:
                    return self._fail("invalid_argument", "article_id or url is required")
                rid, ref = self.split_article_ref(raw_article, recipe_id)
                article = await self.engine.fetch_article(rid, ref)
                return CapabilityResult.ok(
                    {
                        "article": article.model_dump(),
                        "recipe_id": article.extra.get("recipe_id", rid),
                        "translated": bool(
                            article.title_zh
                            or any(p.translated for p in article.paragraphs)
                            or any(c.translated_body for c in article.comments)
                        ),
                    }
                )
            return self._fail("invalid_argument", f"Unsupported text.source.v1 action: {action}")
        except FeedRecipeError as exc:
            return self._fail(exc.error_code, str(exc), data=exc.details)

    async def handle_comments_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        try:
            self.apply_config()
            action = str(req_obj.get("action") or "fetch_comments")
            if action != "fetch_comments":
                return self._fail("invalid_argument", f"Unsupported text.comments.v1 action: {action}")
            raw_article = _str_chain(req_obj, ("article_id", "url", "id"))
            if not raw_article:
                return self._fail("invalid_argument", "article_id or url is required")
            recipe_id = _str_chain(req_obj, ("recipe_id", "channel_id"))
            rid, ref = self.split_article_ref(raw_article, recipe_id)
            article = await self.engine.fetch_article(rid, ref)
            return CapabilityResult.ok(
                {
                    "comments": [comment.model_dump() for comment in article.comments],
                    "count": len(article.comments),
                    "recipe_id": article.extra.get("recipe_id", rid),
                }
            )
        except FeedRecipeError as exc:
            return self._fail(exc.error_code, str(exc), data=exc.details)

    async def handle_recipe_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        raw_action = req_obj.get("action")
        action = str(raw_action or "list").strip()
        if action in {"list", "get", "reload"}:
            translated_action = "channels" if action == "list" else ("get_recipe" if action == "get" else "reload_recipes")
            return await self.handle_text_source_rpc({**req_obj, "action": translated_action})
        if action in {"fetch_feed", "fetch_article"}:
            return await self.handle_text_source_rpc(req_obj)
        return self._fail("invalid_argument", f"Unsupported feed.recipe.v1 action: {action}")

    def _build_ui_schema(self) -> dict[str, Any]:
        return {
            "type": "Section",
            "id": "feed-recipe-root",
            "title": "声明式源配方",
            "description": "JSON / Lua 配方驱动的订阅、正文与评论树",
            "children": [
                {
                    "type": "KeyValue",
                    "id": "feed-recipe-stats",
                    "items": [
                        {"key": "recipes", "label": "已加载配方", "value": str(len(self.engine.recipes))},
                        {"key": "dir", "label": "自定义目录", "value": self.engine._loaded_dir or "内置"},
                    ],
                },
                {
                    "type": "Form",
                    "id": "feed-recipe-search",
                    "fields": [
                        {"name": "recipe_id", "label": "配方", "type": "string", "default": ""},
                        {"name": "query", "label": "搜索", "type": "string", "default": ""},
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "feed-recipe-actions",
                    "actions": [
                        {"id": "fetch_feed", "label": "刷新", "variant": "primary"},
                        {"id": "list_recipes", "label": "配方列表", "variant": "secondary"},
                        {"id": "reload_recipes", "label": "重新加载", "variant": "secondary"},
                    ],
                },
            ],
        }

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="feed-recipe",
            title="源配方",
            domain="text",
            badge="RCP",
            ui_schema=self._build_ui_schema(),
            render=self._build_ui_schema,
            on_action=self.handle_tab_action,
        )

    async def handle_tab_action(
        self,
        action_id: str,
        state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self.handle_text_source_rpc(
            {**(dict(state) if state is not None else {}), "action": action_id}
        )
        return {
            "ok": result.ok,
            "result": result.data,
            "error": result.error,
            "ui_schema": self._build_ui_schema(),
        }


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `FeedRecipePlugin` instance."""
    return FeedRecipePlugin()
