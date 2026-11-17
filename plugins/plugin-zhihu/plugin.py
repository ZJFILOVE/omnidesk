"""OmniDesk v2 plugin-zhihu: TextSource, nested comments, Chrome session auth, and AI companion."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, TextArticle, WorkspaceTabSpec

if __package__:
    from .zhihu_auth import AuthManager
    from .zhihu_errors import (
        ZhihuError,
        coerce_bool,
        json_str,
        json_str_chain,
        parse_optional_int,
        redact_zhihu_secrets,
        url_origin,
    )
    from .zhihu_http import ZhihuHttpClient
    from .zhihu_source import DEFAULT_WEB_BASE_URL, DEFAULT_ZHUANLAN_BASE_URL, ZhihuTextSource
    from .zhihu_translate import run_companion_llm, translate_comments_if_enabled
else:
    _PLUGIN_DIR = str(Path(__file__).resolve().parent)
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from zhihu_auth import AuthManager  # type: ignore[no-redef]
    from zhihu_errors import (  # type: ignore[no-redef]
        ZhihuError,
        coerce_bool,
        json_str,
        json_str_chain,
        parse_optional_int,
        redact_zhihu_secrets,
        url_origin,
    )
    from zhihu_http import ZhihuHttpClient  # type: ignore[no-redef]
    from zhihu_source import DEFAULT_WEB_BASE_URL, DEFAULT_ZHUANLAN_BASE_URL, ZhihuTextSource  # type: ignore[no-redef]
    from zhihu_translate import run_companion_llm, translate_comments_if_enabled  # type: ignore[no-redef]

_TEXT_SOURCE_ACTIONS = [
    "channels",
    "fetch_feed",
    "fetch_article",
    "search",
    "login",
    "sync_cookies",
    "auth_status",
    "companion",
    "summarize",
    "ask",
]
_COMMENTS_ACTIONS = ["fetch_comments", "fetch_child_comments"]
_AUTH_ACTIONS = ["status", "login", "sync_cookies", "set_cookie_header", "logout"]
_COMPANION_ACTIONS = ["summarize", "key_points", "explain_formulas", "ask"]


class ZhihuPlugin(BasePlugin):
    """OmniDesk v2 Zhihu reader providing `text.source.v1`, `text.comments.v1`, `zhihu.auth.v1`, and `zhihu.companion.v1`."""

    def __init__(
        self,
        *,
        web_base_url: str = DEFAULT_WEB_BASE_URL,
        zhuanlan_base_url: str = DEFAULT_ZHUANLAN_BASE_URL,
        allow_loopback_http: bool = False,
    ) -> None:
        self._web_base_url = web_base_url.rstrip("/") or DEFAULT_WEB_BASE_URL
        self._zhuanlan_base_url = zhuanlan_base_url.rstrip("/") or DEFAULT_ZHUANLAN_BASE_URL
        self._allow_loopback_http = bool(allow_loopback_http)

        allowed_origins: set[str] = set()
        for base in (self._web_base_url, self._zhuanlan_base_url):
            if base.startswith("http://"):
                allowed_origins.add(url_origin(base))

        self.auth = AuthManager()
        self.http = ZhihuHttpClient(
            allow_loopback_http=self._allow_loopback_http,
            allowed_loopback_origins=allowed_origins,
        )
        self.text_source = ZhihuTextSource(
            self.auth,
            self.http,
            web_base_url=self._web_base_url,
            zhuanlan_base_url=self._zhuanlan_base_url,
            allow_loopback_http=self._allow_loopback_http,
        )

    async def on_load(self, ctx: PluginContext) -> None:
        """Non-blocking plugin initialization (< 50 ms)."""
        await super().on_load(ctx)
        self.auth.set_context(ctx)
        self.http.set_context(ctx)
        self.text_source.set_context(ctx)
        try:
            self.reload_config()
        except ZhihuError:
            pass

        ctx.register_text_source(self.text_source)
        ctx.register_capability(
            "text.source.v1",
            self.handle_text_source_rpc,
            metadata={"name": "知乎深度问答阅读器", "version": "0.2.0", "actions": list(_TEXT_SOURCE_ACTIONS)},
        )
        ctx.register_capability(
            "text.comments.v1",
            self.handle_comments_rpc,
            metadata={"name": "知乎嵌套评论树", "version": "0.2.0", "actions": list(_COMMENTS_ACTIONS)},
        )
        ctx.register_capability(
            "zhihu.auth.v1",
            self.handle_auth_rpc,
            metadata={"actions": list(_AUTH_ACTIONS)},
        )
        ctx.register_capability(
            "zhihu.companion.v1",
            self.handle_companion_rpc,
            metadata={"actions": list(_COMPANION_ACTIONS)},
        )
        ctx.register_workspace_tab(self._build_workspace_tab_spec())

    async def on_unload(self) -> None:
        """Release references cleanly prior to PluginContext resource revocation."""
        self.text_source.set_context(None)
        self.http.set_context(None)
        self.auth.set_context(None)
        self.ctx = None

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight offline readiness status without leaking secrets."""
        config_error = ""
        try:
            self.reload_config()
        except ZhihuError as exc:
            config_error = redact_zhihu_secrets(exc.message, self.auth.secrets_for_redaction())
        ctx = self.ctx
        return {
            "ok": True,
            "plugin_id": ctx.plugin_id if ctx is not None else "plugin-zhihu",
            "source_id": self.text_source.source_id,
            "auth": self.auth.safe_status(),
            "last_transport": self.http.last_transport,
            "fallback_count": self.http.fallback_count,
            "config_error": config_error,
            "llm_companion_available": bool(ctx and ctx.capabilities.has("llm.chat.v1")),
            "llm_translate_available": bool(ctx and ctx.capabilities.has("llm.translate.v1")),
        }

    def reload_config(self) -> None:
        if self.ctx is not None:
            cfg = self.auth.load_from_context(self.ctx)
            self.http.set_default_timeout_sec(cfg.request_timeout_sec)

    def _fail(
        self,
        error_code: str,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> CapabilityResult:
        safe_msg = redact_zhihu_secrets(message, self.auth.secrets_for_redaction())
        payload = dict(data or {})
        payload["ok"] = False
        payload["error"] = safe_msg
        return CapabilityResult.fail(
            safe_msg,
            data=payload,
            error_code=error_code,
            provider_plugin_id=self.ctx.plugin_id if self.ctx is not None else "plugin-zhihu",
        )

    def _fail_from_error_dict(
        self,
        err: Mapping[str, Any],
        *,
        extra_data: Mapping[str, Any] | None = None,
    ) -> CapabilityResult:
        copy = dict(err)
        code = str(copy.pop("error_code", "service_offline"))
        msg = str(copy.pop("error", "知乎请求失败"))
        if extra_data:
            copy.update(extra_data)
        return self._fail(code, msg, data=copy)

    @staticmethod
    def _ok(data: Mapping[str, Any]) -> CapabilityResult:
        payload = dict(data)
        payload["ok"] = True
        return CapabilityResult.ok(payload)

    def _coerce_req_object(self, req: Any) -> dict[str, Any]:
        if req is None:
            return {}
        if not isinstance(req, Mapping):
            raise ZhihuError("invalid_argument", "请求参数必须为 JSON 对象")
        return dict(req)

    async def handle_text_source_rpc(self, req: Any) -> CapabilityResult:
        """Handle `text.source.v1` actions aligned with v1 manifest."""
        try:
            obj = self._coerce_req_object(req)
            self.reload_config()
            if "action" not in obj or obj.get("action") is None:
                if any(isinstance(obj.get(k), str) and str(obj.get(k)).strip() for k in ("id", "article_id", "url")):
                    action = "fetch_article"
                else:
                    action = "fetch_feed"
            else:
                action = json_str(obj, "action", "").strip()
                if not action:
                    raise ZhihuError("invalid_argument", "action 必须为非空字符串")

            if action == "channels":
                await self.auth.resolve_for_url(f"{self._web_base_url}/")
                chans = self.text_source.channels()
                return self._ok(
                    {"channels": [c.model_dump() for c in chans], "auth": self.auth.safe_status()}
                )

            if action == "fetch_feed":
                channel_id = json_str(obj, "channel_id", "").strip()
                query = json_str(obj, "query", "").strip()
                page = parse_optional_int(obj, "page", 1)
                if page < 1:
                    raise ZhihuError("invalid_argument", "page 必须为 >= 1 的整数")
                force_refresh = coerce_bool(obj.get("force_refresh"), False)
                items = await self.text_source.fetch_feed(
                    channel_id=channel_id,
                    query=query,
                    page=page,
                    force_refresh=force_refresh,
                )
                if not items and self.text_source.last_error:
                    return self._fail_from_error_dict(
                        self.text_source.last_error, extra_data={"items": [], "channel_id": channel_id or "hot"}
                    )
                auth_res = await self.auth.resolve_for_url(f"{self._web_base_url}/")
                return self._ok(
                    {
                        "channel_id": channel_id or "hot",
                        "items": [it.model_dump() for it in items],
                        "count": len(items),
                        "page": page,
                        "authenticated": auth_res.is_authenticated,
                    }
                )

            if action == "search":
                query = json_str(obj, "query", "")
                page = parse_optional_int(obj, "page", 1)
                if page < 1:
                    raise ZhihuError("invalid_argument", "page 必须为 >= 1 的整数")
                items = await self.text_source.search_zhihu(query, page)
                if not items and self.text_source.last_error:
                    return self._fail_from_error_dict(self.text_source.last_error, extra_data={"items": []})
                return self._ok({"items": [it.model_dump() for it in items], "count": len(items), "page": page})

            if action == "fetch_article":
                aid = json_str_chain(obj, ("id", "article_id", "url"))
                if not aid:
                    raise ZhihuError("invalid_argument", "id, article_id, or url is required")
                want_tr: bool | None = None
                if "translate" in obj or "auto_translate" in obj:
                    want_tr = coerce_bool(obj.get("translate", obj.get("auto_translate")), False)
                target_lang = json_str(obj, "target_lang", "zh-CN").strip() or "zh-CN"
                article = await self.text_source.fetch_article(
                    aid, auto_translate=want_tr, target_lang=target_lang
                )
                return self._ok({"article": article.model_dump()})

            if action in {"login", "sync_cookies", "auth_status"}:
                auth_req = dict(obj)
                auth_req["action"] = "status" if action == "auth_status" else action
                return await self.handle_auth_rpc(auth_req)

            if action in {"companion", "summarize", "key_points", "explain_formulas", "ask"}:
                return await self.handle_companion_rpc(obj)

            return self._fail("invalid_argument", f"Unknown text.source.v1 action: {action}")
        except ZhihuError as exc:
            return self._fail(exc.error_code, exc.message, data=exc.details)

    async def handle_comments_rpc(self, req: Any) -> CapabilityResult:
        """Handle `text.comments.v1` paginated comment tree requests."""
        try:
            obj = self._coerce_req_object(req)
            self.reload_config()
            raw_action = obj.get("action")
            action = "fetch_comments" if raw_action is None else json_str(obj, "action", "").strip()
            if action not in {"fetch_comments", "fetch_child_comments"}:
                raise ZhihuError("invalid_argument", f"Unknown text.comments.v1 action: {action or raw_action!r}")

            aid = json_str_chain(obj, ("id", "article_id", "url"))
            root_cid = json_str(obj, "root_comment_id", "").strip()
            if action == "fetch_child_comments" and not root_cid:
                raise ZhihuError("invalid_argument", "root_comment_id is required for fetch_child_comments")
            if not aid and not root_cid:
                raise ZhihuError("invalid_argument", "id / article_id or root_comment_id is required")

            offset = parse_optional_int(obj, "offset", 0)
            limit = parse_optional_int(obj, "limit", 20)
            order_by = json_str(obj, "order_by", "default").strip() or "default"
            author_hint = json_str(obj, "author_hint", "")

            page_res = await self.text_source.fetch_comments(
                aid,
                offset=offset,
                limit=limit,
                order_by=order_by,
                root_comment_id=root_cid,
                author_hint=author_hint,
            )
            want_tr = coerce_bool(
                obj.get("translate", obj.get("auto_translate")),
                self.auth.config().auto_translate,
            )
            target_lang = json_str(obj, "target_lang", "zh-CN").strip() or "zh-CN"
            comments = await translate_comments_if_enabled(
                self.ctx,
                page_res.comments,
                auto_translate=want_tr,
                target_lang=target_lang,
            )
            return self._ok(
                {
                    "comments": [c.model_dump() for c in comments],
                    "paging": {
                        "is_end": page_res.is_end,
                        "next": page_res.next_url,
                        "total": page_res.total,
                    },
                    "count": len(comments),
                    "offset": offset,
                    "limit": min(limit, 50),
                }
            )
        except ZhihuError as exc:
            return self._fail(exc.error_code, exc.message, data=exc.details)

    async def handle_auth_rpc(self, req: Any) -> CapabilityResult:
        """Handle `zhihu.auth.v1` actions (`status`, `login`, `sync_cookies`, `set_cookie_header`, `logout`)."""
        try:
            obj = self._coerce_req_object(req)
            self.reload_config()
            raw_action = obj.get("action")
            action = "status" if raw_action is None else json_str(obj, "action", "").strip()

            if action == "status":
                resolved = await self.auth.resolve_for_url(f"{self._web_base_url}/")
                status = self.auth.safe_status(resolved)
                return self._ok({**status, "auth": status})

            if action == "login":
                title = json_str(obj, "title", "知乎登录（完成后关闭窗口即可）") or "知乎登录（完成后关闭窗口即可）"
                launched, session, err_code, err_msg = await self.auth.open_login_window(title)
                if not launched:
                    return self._fail(err_code or "service_offline", err_msg)
                return self._ok({"launched": True, "session": session, "auth": self.auth.safe_status()})

            if action == "sync_cookies":
                synced, source_used, err_msg, res = await self.auth.sync_from_chrome()
                if not synced:
                    return self._fail(
                        "not_configured",
                        err_msg,
                        data={"synced": False, "source": source_used, "auth": self.auth.safe_status(res)},
                    )
                return self._ok({"synced": True, "source": source_used, "auth": self.auth.safe_status(res)})

            if action == "set_cookie_header":
                hdr = json_str_chain(obj, ("cookie_header", "cookie"))
                if not hdr:
                    raise ZhihuError("invalid_argument", "cookie_header is required")
                res = self.auth.save_vault_cookie_header(hdr)
                return self._ok({"saved": True, "auth": self.auth.safe_status(res)})

            if action == "logout":
                self.auth.logout()
                return self._ok({"logged_out": True, "auth": self.auth.safe_status()})

            raise ZhihuError("invalid_argument", f"Unknown zhihu.auth.v1 action: {action or raw_action!r}")
        except ZhihuError as exc:
            return self._fail(exc.error_code, exc.message, data=exc.details)

    async def handle_companion_rpc(self, req: Any) -> CapabilityResult:
        """Handle `zhihu.companion.v1` LLM reading companion actions."""
        try:
            obj = self._coerce_req_object(req)
            self.reload_config()
            raw_article = obj.get("article")
            if raw_article is not None:
                if not isinstance(raw_article, Mapping):
                    raise ZhihuError("invalid_argument", "article must be a JSON object")
                try:
                    article = TextArticle.model_validate(dict(raw_article))
                except ValidationError as exc:
                    raise ZhihuError("invalid_argument", f"article 参数格式无效: {exc}") from None
            else:
                aid = json_str_chain(obj, ("id", "article_id", "url"))
                if not aid:
                    raise ZhihuError("invalid_argument", "article or article_id is required for companion reading")
                article = await self.text_source.fetch_article(aid)

            res = await run_companion_llm(
                self.ctx,
                article,
                obj,
                secrets=self.auth.secrets_for_redaction(),
            )
            if not res.ok:
                return self._fail(str((res.data or {}).get("error_code") or "service_offline"), res.error)
            return self._ok(res.data)
        except ZhihuError as exc:
            return self._fail(exc.error_code, exc.message, data=exc.details)

    def _build_ui_schema(self) -> dict[str, Any]:
        status = self.auth.safe_status()
        return {
            "type": "Section",
            "id": "zhihu-workspace-root",
            "title": "知乎深度问答阅读器",
            "description": "知乎热榜/推荐/关注/收藏夹、LaTeX 公式与代码块保真、嵌套评论树及 Chrome 会话同步",
            "children": [
                {
                    "type": "KeyValue",
                    "id": "zhihu-auth-status",
                    "items": [
                        {"key": "cookie_source", "label": "Cookie 来源", "value": status.get("cookie_source", "guest")},
                        {"key": "authenticated", "label": "登录状态", "value": "已登录" if status.get("authenticated") else "未登录"},
                        {"key": "transport", "label": "最近通道", "value": self.http.last_transport},
                    ],
                },
                {
                    "type": "Form",
                    "id": "zhihu-search-form",
                    "fields": [
                        {"name": "query", "label": "搜索关键词或知乎链接", "type": "string", "default": ""},
                        {"name": "channel_id", "label": "栏目 ID", "type": "string", "default": "hot"},
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "zhihu-actions",
                    "actions": [
                        {"id": "fetch_feed", "label": "刷新栏目", "variant": "primary"},
                        {"id": "login", "label": "Chrome 登录", "variant": "secondary"},
                        {"id": "sync_cookies", "label": "同步 Cookie", "variant": "secondary"},
                    ],
                },
            ],
        }

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="zhihu",
            title="知乎",
            domain="text",
            badge="ZHIHU",
            ui_schema=self._build_ui_schema(),
            render=self._build_ui_schema,
            on_action=self.handle_tab_action,
        )

    async def handle_tab_action(self, action_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch declarative workspace tab actions to `handle_text_source_rpc`."""
        payload = {**(dict(state) if state is not None else {}), "action": action_id}
        res = await self.handle_text_source_rpc(payload)
        return {"ok": res.ok, "result": res.data, "error": res.error, "ui_schema": self._build_ui_schema()}


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `ZhihuPlugin` instance."""
    return ZhihuPlugin()
