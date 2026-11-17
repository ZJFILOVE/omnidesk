"""OmniDesk v2 plugin-v2ex entrypoint: TextSource, text.source.v1, text.comments.v1, and workspace tab."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, TextArticle, WorkspaceTabSpec

if __package__:
    from .v2ex_auth import AuthManager
    from .v2ex_http import HttpClient
    from .v2ex_security import DEFAULT_V2EX_BASE_URL, V2exError, redact_sensitive
    from .v2ex_source import DEFAULT_MAX_REPLY_PAGES, V2exTextSource
    from .v2ex_translate import (
        summarize_article_via_llm,
        translate_article_if_enabled,
        translate_comments_if_enabled,
    )
else:
    _PLUGIN_ROOT = str(Path(__file__).resolve().parent)
    if _PLUGIN_ROOT not in sys.path:
        sys.path.insert(0, _PLUGIN_ROOT)
    from v2ex_auth import AuthManager
    from v2ex_http import HttpClient
    from v2ex_security import DEFAULT_V2EX_BASE_URL, V2exError, redact_sensitive
    from v2ex_source import DEFAULT_MAX_REPLY_PAGES, V2exTextSource
    from v2ex_translate import (
        summarize_article_via_llm,
        translate_article_if_enabled,
        translate_comments_if_enabled,
    )

_SUPPORTED_TEXT_ACTIONS = (
    "status",
    "auth_status",
    "auth_sync_chrome",
    "sync_chrome",
    "sync_cookies",
    "auth_open_login",
    "open_login",
    "login",
    "auth_set_pat",
    "set_pat",
    "auth_set_cookie",
    "set_cookie",
    "auth_logout",
    "logout",
    "channels",
    "list_channels",
    "fetch_feed",
    "search",
    "fetch_article",
    "list_nodes",
    "subscribe_node",
    "unsubscribe_node",
    "sync_nodes",
    "summarize",
    "companion",
    "ask",
)


def _coerce_bool(val: Any, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val != 0
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    return default


def _parse_int_field(req: Mapping[str, Any], key: str, default: int, *, minimum: int = 1) -> int:
    if key not in req or req[key] is None:
        return default
    val = req[key]
    if isinstance(val, bool) or not isinstance(val, int):
        raise V2exError("invalid_argument", f"{key} 必须是整数")
    if val < minimum:
        raise V2exError("invalid_argument", f"{key} 必须 >= {minimum}")
    return val


def _extract_id_or_url(req: Mapping[str, Any], *, include_topic_id: bool = False) -> str:
    keys = ("id", "article_id", "url", "topic_id") if include_topic_id else ("id", "article_id", "url")
    for k in keys:
        val = req.get(k)
        if isinstance(val, int) and not isinstance(val, bool):
            return str(val)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


class V2exPlugin(BasePlugin):
    """OmniDesk v2 V2EX Tech Community plugin (`text.source.v1`, `text.comments.v1`)."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_V2EX_BASE_URL,
        allow_loopback_http: bool = False,
    ) -> None:
        self.auth = AuthManager(allow_loopback_http=allow_loopback_http)
        self.http = HttpClient(base_url=base_url, allow_loopback_http=allow_loopback_http)
        self.source = V2exTextSource(self.auth, self.http)
        self._last_status = "ready"

    async def on_load(self, ctx: PluginContext) -> None:
        """Non-blocking plugin initialization (< 50 ms)."""
        await super().on_load(ctx)
        self.auth.set_context(ctx)
        self.http.set_context(ctx)
        self.source.set_context(ctx)
        self.reload_config()

        ctx.register_text_source(self.source)
        ctx.register_capability(
            "text.source.v1",
            self.handle_text_source_rpc,
            metadata={
                "name": "V2EX Tech Community",
                "version": "0.2.0",
                "category": "v2ex",
                "actions": list(_SUPPORTED_TEXT_ACTIONS),
            },
        )
        ctx.register_capability(
            "text.comments.v1",
            self.handle_comments_rpc,
            metadata={
                "name": "V2EX Reply Tree",
                "version": "0.2.0",
                "category": "v2ex",
                "actions": ["fetch_comments"],
            },
        )
        ctx.register_workspace_tab(self._build_workspace_tab_spec())
        ctx.subscribe("config.changed", self._on_config_changed)

    async def on_unload(self) -> None:
        self.source.set_context(None)
        self.http.set_context(None)
        self.auth.set_context(None)
        self.ctx = None
        self._last_status = "unloaded"

    def reload_config(self) -> None:
        """Reload non-secret config, SecretVault credentials, timeout, and node subscriptions."""
        if self.ctx is None:
            return
        self.auth.load_from_context(self.ctx)
        self.http.set_default_timeout_sec(self.auth.config.request_timeout_sec)
        self.source.reload_subscriptions()

    async def _on_config_changed(self, event: Any) -> None:
        payload = getattr(event, "payload", event)
        if not isinstance(payload, Mapping) or payload.get("plugin_id", "plugin-v2ex") == "plugin-v2ex":
            self.reload_config()

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight offline readiness snapshot without secrets or network calls."""
        self.reload_config()
        status = self.auth.safe_status_sync()
        return {
            **status,
            "ok": True,
            "plugin_id": self.ctx.plugin_id if self.ctx is not None else "plugin-v2ex",
            "channel_count": len(self.source.channels()),
            "last_status": self._last_status,
        }

    def _redact_obj(self, value: Any, secrets: tuple[str, ...]) -> Any:
        if isinstance(value, str):
            return redact_sensitive(value, secrets)
        if isinstance(value, Mapping):
            return {k: self._redact_obj(v, secrets) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_obj(v, secrets) for v in value]
        return value

    def _fail(self, error_code: str, message: str, *, data: Mapping[str, Any] | None = None) -> CapabilityResult:
        secrets = self.auth.secret_values()
        safe_msg = redact_sensitive(message, secrets)
        extra = self._redact_obj(dict(data or {}), secrets)
        extra["ok"] = False
        extra["error"] = safe_msg
        self._last_status = f"error:{error_code}"
        return CapabilityResult.fail(safe_msg, data=extra, error_code=error_code)

    @staticmethod
    def _ok(data: Mapping[str, Any]) -> CapabilityResult:
        payload = dict(data)
        payload["ok"] = True
        return CapabilityResult.ok(payload)

    @staticmethod
    def _serialize_nodes(nodes: list[Any]) -> list[dict[str, str]]:
        return [{"slug": n.slug, "title": n.title} for n in nodes]

    async def _handle_auth_action(self, action: str, req_obj: Mapping[str, Any]) -> CapabilityResult:
        if action in {"status", "auth_status"}:
            st = await self.auth.safe_status()
            return self._ok(st)

        if action in {"auth_sync_chrome", "sync_chrome", "sync_cookies"}:
            ok, source_used, err_code, err_msg = await self.auth.sync_from_chrome()
            if not ok:
                return self._fail(err_code or "auth_required", err_msg, data={"synced": False})
            st = await self.auth.safe_status()
            st["synced"] = True
            st["synced_from"] = source_used
            return self._ok(st)

        if action in {"auth_open_login", "open_login", "login"}:
            title = str(req_obj.get("title") or "V2EX 登录（完成后关闭窗口即可）")
            res = await self.auth.open_login_window(title)
            if not res.ok:
                code = str((res.data or {}).get("error_code") or "service_offline")
                return self._fail(code, res.error or "无法打开 V2EX 登录窗口")
            return self._ok(
                {
                    "opened": True,
                    "login_url": f"{self.http.base_url}/signin",
                    "session": dict(res.data),
                }
            )

        if action in {"auth_set_pat", "set_pat"}:
            raw_tok = req_obj.get("api_token") if "api_token" in req_obj else req_obj.get("token")
            if not isinstance(raw_tok, str) or not raw_tok.strip():
                return self._fail("invalid_argument", "api_token 不能为空（清除凭据请使用 auth_logout）")
            self.auth.save_vault_api_token(raw_tok)
            return self._ok(await self.auth.safe_status())

        if action in {"auth_set_cookie", "set_cookie"}:
            raw_hdr = req_obj.get("cookie_header") if "cookie_header" in req_obj else req_obj.get("cookie")
            if not isinstance(raw_hdr, str) or not raw_hdr.strip():
                return self._fail("invalid_argument", "cookie_header 不能为空（清除凭据请使用 auth_logout）")
            self.auth.save_vault_cookie_header(raw_hdr)
            return self._ok(await self.auth.safe_status())

        if action in {"auth_logout", "logout"}:
            self.auth.clear_credentials()
            return self._ok(await self.auth.safe_status())

        return self._fail("invalid_argument", f"Unknown auth action: {action}")

    async def handle_text_source_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Handle `text.source.v1` actions cleanly and fail-closed."""
        if req is not None and not isinstance(req, Mapping):
            return self._fail("invalid_argument", "text.source.v1 请求必须是 JSON 对象")
        req_obj = dict(req) if req is not None else {}

        try:
            self.reload_config()
        except V2exError as exc:
            return self._fail(exc.error_code, exc.message)

        if "action" not in req_obj or req_obj.get("action") is None:
            if _extract_id_or_url(req_obj) and not any(
                req_obj.get(k) is not None for k in ("channel_id", "node", "query")
            ):
                action = "fetch_article"
            else:
                action = "fetch_feed"
        else:
            raw_act = req_obj.get("action")
            if not isinstance(raw_act, str) or not raw_act.strip():
                return self._fail("invalid_argument", "action 必须是非空字符串")
            action = raw_act.strip()

        if (
            action in {"status", "sync_chrome", "sync_cookies", "open_login", "login", "set_pat", "set_cookie", "logout"}
            or action.startswith("auth_")
        ):
            return await self._handle_auth_action(action, req_obj)

        try:
            if action in {"channels", "list_channels"}:
                st = await self.auth.safe_status()
                chans = self.source.channels()
                return self._ok(
                    {
                        "channels": [c.model_dump() for c in chans],
                        "auth": st,
                    }
                )

            if action in {"fetch_feed", "search"}:
                channel_id = str(req_obj.get("channel_id") or "").strip()
                if not channel_id and req_obj.get("node"):
                    channel_id = f"node:{str(req_obj['node']).strip()}"
                query = str(req_obj.get("query") or "")
                page = _parse_int_field(req_obj, "page", 1, minimum=1)
                force_refresh = bool(req_obj.get("force_refresh", False))
                res = await self.source.fetch_feed_detailed(
                    channel_id=channel_id,
                    query=query,
                    page=page,
                    force_refresh=force_refresh,
                )
                if not res.ok:
                    return self._fail(
                        res.error_code or "service_offline",
                        res.error or "获取 V2EX 话题列表失败",
                        data={"http_status": res.http_status} if res.http_status else None,
                    )
                self._last_status = f"feed:{res.channel_id}:{len(res.items)}"
                return self._ok(
                    {
                        "channel_id": res.channel_id,
                        "items": [it.model_dump() for it in res.items],
                        "count": len(res.items),
                        "page": res.page,
                        "source_mode": res.source_mode,
                    }
                )

            if action == "fetch_article":
                aid = _extract_id_or_url(req_obj)
                if not aid:
                    return self._fail("invalid_argument", "article id or url is required")
                max_pages = _parse_int_field(req_obj, "max_reply_pages", DEFAULT_MAX_REPLY_PAGES, minimum=1)
                res = await self.source.fetch_article_detailed(aid, max_reply_pages=max_pages)
                if not res.ok or res.article is None:
                    return self._fail(
                        res.error_code or "service_offline",
                        res.error or "Failed to fetch V2EX topic",
                        data={"http_status": res.http_status} if res.http_status else None,
                    )

                default_tr = self.auth.config.auto_translate
                want_tr = _coerce_bool(
                    req_obj.get("translate", req_obj.get("auto_translate", default_tr)),
                    default_tr,
                )
                target_lang = str(req_obj.get("target_lang") or self.auth.config.target_lang or "zh-CN").strip()
                article, tr_meta = await translate_article_if_enabled(
                    self.ctx,
                    res.article,
                    auto_translate=want_tr,
                    target_lang=target_lang,
                )
                if want_tr and article.comments and _coerce_bool(req_obj.get("translate_comments", True), True):
                    tr_comments, _ = await translate_comments_if_enabled(
                        self.ctx,
                        list(article.comments),
                        auto_translate=True,
                        title=article.title,
                        target_lang=target_lang,
                    )
                    article = article.model_copy(update={"comments": tr_comments})

                self._last_status = f"article:{article.id}:{res.source_mode}"
                return self._ok(
                    {
                        "article": article.model_dump(),
                        "total_replies": res.total_replies,
                        "pages_fetched": res.pages_fetched,
                        "total_pages": res.total_pages,
                        "source_mode": res.source_mode,
                        **tr_meta,
                    }
                )

            if action == "list_nodes":
                nodes = self.source.subscriptions.list_nodes()
                return self._ok({"nodes": self._serialize_nodes(nodes)})

            if action == "subscribe_node":
                slug = str(req_obj.get("slug") or req_obj.get("node") or "")
                title = str(req_obj.get("title") or "")
                nodes = self.source.subscriptions.subscribe(slug, title)
                return self._ok({"nodes": self._serialize_nodes(nodes)})

            if action == "unsubscribe_node":
                slug = str(req_obj.get("slug") or req_obj.get("node") or "")
                nodes = self.source.subscriptions.unsubscribe(slug)
                return self._ok({"nodes": self._serialize_nodes(nodes)})

            if action == "sync_nodes":
                added, nodes = await self.source.sync_followed_nodes_from_web()
                return self._ok({"added": added, "nodes": self._serialize_nodes(nodes)})

            if action in {"summarize", "companion", "ask"}:
                return await self._handle_summarize(req_obj)

            return self._fail(
                "invalid_argument",
                f"Unknown text.source.v1 action: {action}",
                data={"supported_actions": list(_SUPPORTED_TEXT_ACTIONS)},
            )
        except V2exError as exc:
            return self._fail(exc.error_code, exc.message, data=exc.details)

    async def handle_comments_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Handle `text.comments.v1` paginated comment tree retrieval."""
        if req is not None and not isinstance(req, Mapping):
            return self._fail("invalid_argument", "text.comments.v1 请求必须是 JSON 对象")
        req_obj = dict(req) if req is not None else {}

        try:
            self.reload_config()
            raw_action = req_obj.get("action")
            action = "fetch_comments" if raw_action is None else str(raw_action).strip()
            if action != "fetch_comments":
                return self._fail("invalid_argument", f"Unknown text.comments.v1 action: {action!r}")

            aid = _extract_id_or_url(req_obj, include_topic_id=True)
            if not aid:
                return self._fail("invalid_argument", "topic id or url is required")

            max_pages = _parse_int_field(req_obj, "max_reply_pages", DEFAULT_MAX_REPLY_PAGES, minimum=1)
            reply_page = (
                _parse_int_field(req_obj, "page", 1, minimum=1)
                if "page" in req_obj and req_obj["page"] is not None
                else None
            )
            res = await self.source.fetch_article_detailed(
                aid,
                max_reply_pages=max_pages,
                reply_page=reply_page,
            )
            if not res.ok or res.article is None:
                return self._fail(
                    res.error_code or "service_offline",
                    res.error or "Failed to fetch V2EX replies",
                    data={"http_status": res.http_status} if res.http_status else None,
                )

            default_tr = self.auth.config.auto_translate
            want_tr = _coerce_bool(
                req_obj.get("translate", req_obj.get("auto_translate", default_tr)),
                default_tr,
            )
            target_lang = str(req_obj.get("target_lang") or self.auth.config.target_lang or "zh-CN").strip()
            comments, tr_meta = await translate_comments_if_enabled(
                self.ctx,
                list(res.article.comments),
                auto_translate=want_tr,
                title=res.article.title,
                target_lang=target_lang,
            )
            return self._ok(
                {
                    "id": res.article.id,
                    "comments": [c.model_dump() for c in comments],
                    "total_replies": res.total_replies,
                    "pages_fetched": res.pages_fetched,
                    "total_pages": res.total_pages,
                    "source_mode": res.source_mode,
                    **tr_meta,
                }
            )
        except V2exError as exc:
            return self._fail(exc.error_code, exc.message, data=exc.details)

    async def _handle_summarize(self, req_obj: Mapping[str, Any]) -> CapabilityResult:
        raw_article = req_obj.get("article")
        if isinstance(raw_article, Mapping):
            try:
                article = TextArticle.model_validate(dict(raw_article))
            except ValidationError as exc:
                return self._fail("invalid_argument", f"article 参数格式无效: {exc}")
        else:
            aid = _extract_id_or_url(req_obj, include_topic_id=True)
            if not aid:
                return self._fail("invalid_argument", "article or article_id is required for summarization")
            res = await self.source.fetch_article_detailed(aid, max_reply_pages=DEFAULT_MAX_REPLY_PAGES)
            if not res.ok or res.article is None:
                return self._fail(
                    res.error_code or "service_offline",
                    res.error or "Failed to fetch V2EX topic",
                    data={"http_status": res.http_status} if res.http_status else None,
                )
            article = res.article

        question = str(req_obj.get("question") or req_obj.get("prompt") or "").strip()
        return await summarize_article_via_llm(
            self.ctx,
            article,
            question=question,
            secrets=self.auth.secret_values(),
        )

    def _build_ui_schema(self) -> dict[str, Any]:
        st = self.auth.safe_status_sync()
        return {
            "type": "Section",
            "id": "v2ex-workspace-root",
            "title": "V2EX Tech Community",
            "description": "支持 API v2 PAT、Chrome A2 Cookie 同步、多节点流、附言、楼中楼评论树与双语翻译",
            "children": [
                {
                    "type": "KeyValue",
                    "id": "v2ex-auth-status",
                    "items": [
                        {"key": "active_mode", "label": "认证模式", "value": st.get("active_mode", "guest")},
                        {"key": "has_api_token", "label": "API v2 PAT", "value": "已配置" if st.get("has_api_token") else "未配置"},
                        {"key": "has_a2", "label": "A2 Cookie", "value": "已同步" if st.get("has_a2") else "未同步"},
                        {"key": "auto_translate", "label": "自动翻译", "value": "开启" if st.get("auto_translate") else "关闭"},
                        {"key": "last_status", "label": "最近状态", "value": self._last_status},
                    ],
                },
                {
                    "type": "Form",
                    "id": "v2ex-controls-form",
                    "fields": [
                        {
                            "name": "channel_id", "label": "栏目 / 节点", "type": "combobox", "default": "hot",
                            "options": [{"value": c.id, "label": c.name + (" · 需登录" if c.requires_auth else "")}
                                        for c in self.source.channels()],
                            "placeholder": "例如 node:python 或 node:linux",
                        },
                        {"name": "query", "label": "关键词过滤", "type": "string", "default": ""},
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "v2ex-actions",
                    "actions": [
                        {"id": "fetch_feed", "label": "刷新话题", "variant": "primary"},
                        {"id": "auth_open_login", "label": "Chrome 登录", "variant": "secondary"},
                        {"id": "auth_sync_chrome", "label": "同步 Cookie", "variant": "secondary"},
                        {"id": "sync_nodes", "label": "同步关注节点", "variant": "secondary"},
                    ],
                },
            ],
        }

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="v2ex",
            title="V2EX",
            domain="text",
            badge="V2EX",
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
    """Manifest entrypoint returning a `V2exPlugin` instance."""
    return V2exPlugin()
