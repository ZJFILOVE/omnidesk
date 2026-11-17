"""OmniDesk v2 plugin-shuiyuan: SJTU Discourse TextSource, comments, auth, and translation."""

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
    from .shuiyuan_auth import AuthManager
    from .shuiyuan_client import DiscourseClient, FetchStatus
    from .shuiyuan_http import HttpClientProtocol, SafeHttpClient
    from .shuiyuan_security import DEFAULT_BASE_URL, ShuiyuanError, redact_sensitive
    from .shuiyuan_subscriptions import SubscriptionStore
    from .shuiyuan_translate import translate_article, translate_comments, translate_feed_items
else:
    _PLUGIN_DIR = str(Path(__file__).resolve().parent)
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from shuiyuan_auth import AuthManager  # type: ignore[no-redef]
    from shuiyuan_client import DiscourseClient, FetchStatus  # type: ignore[no-redef]
    from shuiyuan_http import HttpClientProtocol, SafeHttpClient  # type: ignore[no-redef]
    from shuiyuan_security import DEFAULT_BASE_URL, ShuiyuanError, redact_sensitive  # type: ignore[no-redef]
    from shuiyuan_subscriptions import SubscriptionStore  # type: ignore[no-redef]
    from shuiyuan_translate import (  # type: ignore[no-redef]
        translate_article,
        translate_comments,
        translate_feed_items,
    )

_TEXT_SOURCE_ACTIONS = (
    "channels",
    "fetch_feed",
    "fetch_article",
    "search",
    "login",
    "open_login",
    "sync_cookies",
    "auth_status",
    "set_cookie",
    "logout",
    "list_categories",
    "pin_category",
    "unpin_category",
)


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


def _optional_int(req: Mapping[str, Any], key: str, default: int) -> int:
    value = req.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ShuiyuanError("invalid_argument", f"{key} 必须为整数")
    return value


def _str_chain(req: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = req.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class ShuiyuanTextSource(TextSource):
    """TextSource adapter for SJTU Shuiyuan channels, topics, and articles."""

    source_id = "plugin-shuiyuan"
    display_name = "交大水源社区"
    category = "shuiyuan"

    def __init__(self, owner: ShuiyuanPlugin) -> None:
        self._owner = owner
        self.last_error: dict[str, Any] | None = None

    def channels(self) -> list[TextChannel]:
        self._owner.apply_config()
        return self._owner.subscriptions.build_channels(
            is_authenticated=self._owner.auth.is_authenticated_hint()
        )

    async def fetch_feed(
        self,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[TextFeedItem]:
        del force_refresh
        self.last_error = None
        self._owner.apply_config()
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            return []
        items, status = await self._owner.client.fetch_topic_list(
            channel_id=channel_id,
            query=query,
            page=page,
        )
        if not status.ok:
            await self._owner.maybe_open_login(status)
            payload = self._owner._status_error_payload(status)
            self.last_error = payload
            raise ShuiyuanError(
                str(payload["error_code"]),
                str(payload["error"]),
                http_status=status.http_status,
                auth_required=status.auth_required,
                session_expired=status.session_expired,
                challenge_detected=status.challenge_detected,
            )
        if self._owner.auto_translate():
            items = await translate_feed_items(
                self._owner.ctx,
                items,
                target_lang=self._owner.target_lang(),
            )
        return items

    async def fetch_article(self, article_id: str) -> TextArticle:
        self.last_error = None
        self._owner.apply_config()
        article, status, _meta = await self._owner.client.fetch_topic_article(
            article_id,
            max_initial_posts=60,
        )
        if not status.ok or article is None:
            await self._owner.maybe_open_login(status)
            payload = self._owner._status_error_payload(status)
            self.last_error = payload
            raise ShuiyuanError(
                str(payload["error_code"]),
                str(payload["error"]),
                http_status=status.http_status,
                auth_required=status.auth_required,
                session_expired=status.session_expired,
                challenge_detected=status.challenge_detected,
            )
        if self._owner.auto_translate():
            article = await translate_article(
                self._owner.ctx,
                article,
                target_lang=self._owner.target_lang(),
            )
        return article


class ShuiyuanPlugin(BasePlugin):
    """OmniDesk v2 pure Python plugin for the SJTU Shuiyuan Discourse community."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        allow_loopback_http: bool = False,
        http_client: HttpClientProtocol | None = None,
    ) -> None:
        self._default_base_url = base_url.rstrip("/") or DEFAULT_BASE_URL
        self._allow_loopback_http = bool(allow_loopback_http)
        self.auth = AuthManager()
        self.subscriptions = SubscriptionStore()
        self.http = http_client or SafeHttpClient(allow_loopback_http=self._allow_loopback_http)
        self.client = DiscourseClient(
            self.http,
            self.auth,
            self.subscriptions,
            allow_loopback_http=self._allow_loopback_http,
        )
        self.text_source = ShuiyuanTextSource(self)

    async def on_load(self, ctx: PluginContext) -> None:
        await super().on_load(ctx)
        self.auth.set_context(ctx)
        self.client.set_context(ctx)
        self.subscriptions.set_data_dir(ctx.data_dir)
        self.apply_config()

        ctx.register_text_source(self.text_source)
        ctx.register_capability(
            "text.source.v1",
            self.handle_text_source_rpc,
            metadata={"name": "交大水源社区", "version": "0.2.0", "actions": list(_TEXT_SOURCE_ACTIONS)},
        )
        ctx.register_capability(
            "text.comments.v1",
            self.handle_comments_rpc,
            metadata={"name": "交大水源嵌套评论树", "version": "0.2.0", "actions": ["fetch_comments"]},
        )
        ctx.register_workspace_tab(self._build_workspace_tab_spec())

    async def on_unload(self) -> None:
        self.client.set_context(None)
        self.auth.set_context(None)
        self.ctx = None

    async def on_health_check(self) -> dict[str, Any]:
        base_valid = True
        base_url = ""
        try:
            self.apply_config()
            base_url = self.client.base_url
        except ShuiyuanError:
            base_valid = False
        ctx = self.ctx
        return {
            "ok": base_valid,
            "plugin_id": ctx.plugin_id if ctx is not None else "plugin-shuiyuan",
            "base_url": base_url,
            "auth": self.auth.safe_summary(),
            "pinned_categories_count": len(self.subscriptions.list_pinned()),
            "llm_translate_available": bool(ctx and ctx.capabilities.has("llm.translate.v1")),
            "llm_chat_available": bool(ctx and ctx.capabilities.has("llm.chat.v1")),
        }

    def apply_config(self) -> None:
        if self.ctx is None:
            self.auth.configure(base_url=self._default_base_url)
            self.client.configure(
                base_url=self._default_base_url,
                timeout_sec=15.0,
                prefer_chrome_fetch=False,
            )
            return
        cfg = self.ctx.get_config()
        base_url = str(cfg.get("base_url") or self._default_base_url).strip() or self._default_base_url
        self.client.configure(
            base_url=base_url,
            timeout_sec=max(0.1, _coerce_float(cfg.get("request_timeout_sec", 15), 15.0)),
            prefer_chrome_fetch=_coerce_bool(cfg.get("prefer_chrome_fetch", False), False),
        )
        self.auth.configure(
            mode=str(cfg.get("chrome_cookie_source") or "auto").strip() or "auto",
            cookie_file_path=str(cfg.get("cookie_file_path") or "").strip(),
            base_url=self.client.base_url,
            auto_login_on_expire=_coerce_bool(cfg.get("auto_login_on_expire", True), True),
        )
        self.subscriptions.set_data_dir(self.ctx.data_dir)
        pinned = str(cfg.get("pinned_categories") or "").strip()
        if pinned:
            self.subscriptions.apply_config_pinned(pinned)

    def auto_translate(self) -> bool:
        if self.ctx is None:
            return False
        return _coerce_bool(self.ctx.get_config("auto_translate", False), False)

    def target_lang(self) -> str:
        if self.ctx is None:
            return "zh-CN"
        return str(self.ctx.get_config("target_lang", "zh-CN") or "zh-CN")

    async def maybe_open_login(self, status: FetchStatus) -> None:
        if not status.auth_required and not status.challenge_detected:
            return
        if self.auth.auto_login_on_expire:
            await self.auth.open_login_window(force=False)

    def _status_error_payload(self, status: FetchStatus) -> dict[str, Any]:
        code = status.error_code or (
            "not_configured"
            if (status.auth_required or status.challenge_detected)
            else "service_offline"
        )
        message = redact_sensitive(
            status.error or "交大水源请求失败",
            self.auth.secrets_for_redaction(),
        )
        return {
            "error_code": code,
            "error": message,
            "http_status": status.http_status,
            "auth_required": status.auth_required,
            "challenge_detected": status.challenge_detected,
            "session_expired": status.session_expired,
            "has_more": status.has_more,
        }

    def _fail(
        self,
        code: str,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> CapabilityResult:
        safe_message = redact_sensitive(message, self.auth.secrets_for_redaction())
        payload = dict(data or {})
        payload.update({"ok": False, "error": safe_message})
        return CapabilityResult.fail(safe_message, data=payload, error_code=code)

    def _fail_status(self, status: FetchStatus) -> CapabilityResult:
        payload = self._status_error_payload(status)
        return self._fail(str(payload["error_code"]), str(payload["error"]), data=payload)

    async def handle_text_source_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        try:
            self.apply_config()
            raw_action = req_obj.get("action")
            if raw_action is None:
                if any(req_obj.get(key) for key in ("article_id", "id", "url")):
                    action = "fetch_article"
                elif any(key in req_obj for key in ("channel_id", "query", "page")):
                    action = "fetch_feed"
                else:
                    action = "channels"
            elif isinstance(raw_action, str) and raw_action.strip():
                action = raw_action.strip()
            else:
                return self._fail("invalid_argument", "action must be a non-empty string")

            if action == "channels":
                await self.auth.resolve_cookies(persist_chrome_snapshot=True)
                return CapabilityResult.ok(
                    {
                        "channels": [channel.model_dump() for channel in self.text_source.channels()],
                        "auth": self.auth.safe_summary(),
                    }
                )

            if action in {"fetch_feed", "search"}:
                page = _optional_int(req_obj, "page", 1)
                if page < 1:
                    return self._fail("invalid_argument", "page 必须 >= 1")
                items, status = await self.client.fetch_topic_list(
                    channel_id=str(req_obj.get("channel_id") or "").strip(),
                    query=str(req_obj.get("query") or "").strip(),
                    page=page,
                )
                if not status.ok:
                    await self.maybe_open_login(status)
                    return self._fail_status(status)
                if _coerce_bool(req_obj.get("translate", self.auto_translate()), self.auto_translate()):
                    items = await translate_feed_items(
                        self.ctx,
                        items,
                        target_lang=str(req_obj.get("target_lang") or self.target_lang()),
                    )
                return CapabilityResult.ok(
                    {
                        "items": [item.model_dump() for item in items],
                        "count": len(items),
                        "page": page,
                        "has_more": status.has_more,
                        "auth_required": False,
                        "challenge_detected": False,
                        "session_expired": False,
                    }
                )

            if action == "fetch_article":
                article_id = _str_chain(req_obj, ("article_id", "id", "url"))
                if not article_id:
                    return self._fail("invalid_argument", "article_id, id, or url is required")
                max_posts = max(1, min(_optional_int(req_obj, "max_posts", 60), 200))
                article, status, meta = await self.client.fetch_topic_article(
                    article_id,
                    max_initial_posts=max_posts,
                )
                if not status.ok or article is None:
                    await self.maybe_open_login(status)
                    return self._fail_status(status)
                if _coerce_bool(req_obj.get("translate", self.auto_translate()), self.auto_translate()):
                    article = await translate_article(
                        self.ctx,
                        article,
                        target_lang=str(req_obj.get("target_lang") or self.target_lang()),
                    )
                return CapabilityResult.ok({"article": article.model_dump(), **meta})

            if action in {"login", "open_login"}:
                opened, code, message = await self.auth.open_login_window(force=True)
                if not opened:
                    return self._fail(code or "service_offline", message or "无法打开 jAccount 登录窗口")
                return CapabilityResult.ok(
                    {
                        "opened": True,
                        "login_url": f"{self.client.base_url}/login",
                        "auth": self.auth.safe_summary(),
                    }
                )

            if action == "sync_cookies":
                synced, code, message, resolution = await self.auth.sync_from_chrome()
                if not synced:
                    return self._fail(
                        code or "not_configured",
                        message or "Chrome 中暂无有效水源登录 Cookie",
                        data={
                            "synced": False,
                            "has_t_cookie": resolution.has_t_cookie,
                            "has_session_cookie": resolution.has_session_cookie,
                            "auth": self.auth.safe_summary(),
                        },
                    )
                return CapabilityResult.ok(
                    {
                        "synced": True,
                        "has_t_cookie": resolution.has_t_cookie,
                        "has_session_cookie": resolution.has_session_cookie,
                        "auth": self.auth.safe_summary(),
                    }
                )

            if action == "auth_status":
                resolution = await self.auth.resolve_cookies(persist_chrome_snapshot=True)
                return CapabilityResult.ok(
                    {
                        "auth": self.auth.safe_summary(),
                        "cookie_source": resolution.source_used,
                        "has_t_cookie": resolution.has_t_cookie,
                    }
                )

            if action == "set_cookie":
                header = _str_chain(req_obj, ("cookie_header", "cookie"))
                if not header:
                    return self._fail("invalid_argument", "cookie_header is required")
                self.auth.set_manual_cookie_header(header)
                return CapabilityResult.ok({"auth": self.auth.safe_summary()})

            if action == "logout":
                self.auth.logout()
                return CapabilityResult.ok({"auth": self.auth.safe_summary()})

            if action == "list_categories":
                refresh = _coerce_bool(req_obj.get("refresh", False), False)
                status = FetchStatus(ok=True)
                if refresh or not self.subscriptions.list_discovered():
                    _, status = await self.client.fetch_categories()
                    if not status.ok:
                        return self._fail_status(status)
                return CapabilityResult.ok(
                    {
                        "pinned": [category.to_dict() for category in self.subscriptions.list_pinned()],
                        "categories": [
                            category.to_dict() for category in self.subscriptions.list_discovered()
                        ],
                    }
                )

            if action == "pin_category":
                category_id = _optional_int(req_obj, "id", 0)
                slug = str(req_obj.get("slug") or "").strip()
                name = str(req_obj.get("name") or "").strip()
                if not self.subscriptions.pin_category(category_id, slug, name):
                    return self._fail("invalid_argument", "pin_category requires id or slug")
                return CapabilityResult.ok(
                    {"channels": [channel.model_dump() for channel in self.text_source.channels()]}
                )

            if action == "unpin_category":
                key = str(req_obj.get("slug") or req_obj.get("key") or "").strip()
                if not key and req_obj.get("id") is not None:
                    key = str(req_obj["id"])
                if not self.subscriptions.unpin_category(key):
                    return self._fail("invalid_argument", "category not found in pinned list")
                return CapabilityResult.ok(
                    {"channels": [channel.model_dump() for channel in self.text_source.channels()]}
                )

            return self._fail("invalid_argument", f"Unsupported text.source.v1 action: {action}")
        except ShuiyuanError as exc:
            return self._fail(exc.error_code, str(exc))

    async def handle_comments_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        try:
            self.apply_config()
            action = str(req_obj.get("action") or "fetch_comments")
            if action != "fetch_comments":
                return self._fail("invalid_argument", f"Unsupported text.comments.v1 action: {action}")
            article_id = _str_chain(req_obj, ("article_id", "id", "url"))
            if not article_id and req_obj.get("topic_id") is not None:
                article_id = str(req_obj["topic_id"])
            if not article_id:
                return self._fail("invalid_argument", "article_id, id, url, or topic_id is required")
            offset = _optional_int(req_obj, "offset", 0)
            limit = _optional_int(req_obj, "limit", 40)
            if offset < 0 or limit < 1:
                return self._fail("invalid_argument", "offset >= 0 and limit >= 1 are required")
            page, status = await self.client.fetch_topic_comments_page(
                article_id,
                offset=offset,
                limit=limit,
            )
            if not status.ok:
                await self.maybe_open_login(status)
                return self._fail_status(status)
            comments = page.comments
            if _coerce_bool(req_obj.get("translate", self.auto_translate()), self.auto_translate()):
                comments = await translate_comments(
                    self.ctx,
                    comments,
                    target_lang=str(req_obj.get("target_lang") or self.target_lang()),
                )
            return CapabilityResult.ok(
                {
                    "comments": [comment.model_dump() for comment in comments],
                    "total_posts": page.total_posts,
                    "loaded_count": page.loaded_count,
                    "offset": page.offset,
                    "next_offset": page.next_offset,
                    "has_more": page.has_more,
                }
            )
        except ShuiyuanError as exc:
            return self._fail(exc.error_code, str(exc))

    def _build_ui_schema(self) -> dict[str, Any]:
        auth = self.auth.safe_summary()
        return {
            "type": "Section",
            "id": "shuiyuan-workspace-root",
            "title": "交大水源社区",
            "description": "jAccount 会话、版面订阅、嵌套评论与双语阅读",
            "children": [
                {
                    "type": "KeyValue",
                    "id": "shuiyuan-auth-status",
                    "items": [
                        {"key": "cookie_source", "label": "Cookie 来源", "value": auth["cookie_source"]},
                        {"key": "authenticated", "label": "登录状态", "value": "已登录" if auth["authenticated"] else "游客"},
                        {"key": "base_url", "label": "站点", "value": auth["base_url"]},
                    ],
                },
                {
                    "type": "Form",
                    "id": "shuiyuan-search-form",
                    "fields": [
                        {"name": "query", "label": "搜索水源", "type": "string", "default": ""},
                        {"name": "channel_id", "label": "栏目", "type": "string", "default": "latest"},
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "shuiyuan-actions",
                    "actions": [
                        {"id": "fetch_feed", "label": "刷新", "variant": "primary"},
                        {"id": "login", "label": "jAccount 登录", "variant": "secondary"},
                        {"id": "sync_cookies", "label": "同步 Cookie", "variant": "secondary"},
                    ],
                },
            ],
        }

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="shuiyuan",
            title="水源社区",
            domain="text",
            badge="SJTU",
            ui_schema=self._build_ui_schema(),
            render=self._build_ui_schema,
            on_action=self.handle_tab_action,
        )

    async def handle_tab_action(
        self,
        action_id: str,
        state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = {"status": "auth_status", "refresh": "fetch_feed"}.get(action_id, action_id)
        payload = {**(dict(state) if state is not None else {}), "action": action}
        result = await self.handle_text_source_rpc(payload)
        return {
            "ok": result.ok,
            "result": result.data,
            "error": result.error,
            "ui_schema": self._build_ui_schema(),
        }


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `ShuiyuanPlugin` instance."""
    return ShuiyuanPlugin()
