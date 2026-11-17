"""OmniDesk v2 plugin-linuxdo: TextSource, nested comments, Cloudflare-aware Chrome auth, and AI companion."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

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
    from .auth import AuthManager
    from .discourse_client import DiscourseClient, FetchStatus
    from .http_client import HttpClientProtocol
    from .security import DEFAULT_BASE_URL, LinuxdoError, json_bool, json_int, json_str, json_str_chain, redact_text
    from .subscriptions import SubscriptionStore
    from .translator import (
        summarize_article_via_llm,
        translate_article_if_enabled,
        translate_comments_if_enabled,
    )
else:
    _PLUGIN_DIR = str(Path(__file__).resolve().parent)
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from auth import AuthManager  # type: ignore[no-redef]
    from discourse_client import DiscourseClient, FetchStatus  # type: ignore[no-redef]
    from http_client import HttpClientProtocol  # type: ignore[no-redef]
    from security import DEFAULT_BASE_URL, LinuxdoError, json_bool, json_int, json_str, json_str_chain, redact_text  # type: ignore[no-redef]
    from subscriptions import SubscriptionStore  # type: ignore[no-redef]
    from translator import (  # type: ignore[no-redef]
        summarize_article_via_llm,
        translate_article_if_enabled,
        translate_comments_if_enabled,
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
    "summarize",
    "companion",
    "ask",
)


def _coerce_bool(val: Any, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_float(val: Any, default: float) -> float:
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.strip())
        except ValueError:
            return default
    return default


def _parse_optional_int(req: Mapping[str, Any], key: str, default: int) -> int:
    if key not in req or req[key] is None:
        return default
    val = req[key]
    if isinstance(val, bool) or not isinstance(val, int):
        raise LinuxdoError("invalid_argument", f"{key} 必须为整数")
    return val


class LinuxdoTextSource(TextSource):
    """Domain 2 TextSource exposing Linux.do channels, topics, and structured articles."""

    source_id: str = "plugin-linuxdo"
    display_name: str = "Linux.do 社区"
    category: str = "linuxdo"

    def __init__(self, owner: LinuxdoPlugin) -> None:
        self._owner = owner
        self.last_error: dict[str, Any] | None = None

    def channels(self) -> list[TextChannel]:
        self._owner.apply_config()
        res = self._owner.auth.resolve_cookies_sync()
        is_authed = res.has_t_cookie and not self._owner.auth.is_session_expired
        return self._owner.subscriptions.build_channels(is_authenticated=is_authed)

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
            channel_id=channel_id, query=query, page=page
        )
        if items:
            return items
        if not status.ok:
            if page <= 1 and (status.auth_required or status.challenge_detected):
                if self._owner.auth.auto_login_on_expire and (
                    status.session_expired or status.challenge_detected or bool(channel_id.strip())
                ):
                    await self._owner.auth.open_login_window(force=False)
            self.last_error = self._owner._status_error_payload(status)
        return []

    async def fetch_article(self, article_id: str) -> TextArticle:
        self.last_error = None
        self._owner.apply_config()
        article, status = await self._owner.client.fetch_topic_article(article_id, max_initial_posts=60)
        if not status.ok or article is None:
            if status.auth_required or status.challenge_detected:
                if self._owner.auth.auto_login_on_expire and (
                    status.session_expired or status.challenge_detected
                ):
                    await self._owner.auth.open_login_window(force=False)
            err = self._owner._status_error_payload(status)
            self.last_error = err
            raise LinuxdoError(
                err["error_code"],
                err["error"],
                http_status=status.http_status,
                auth_required=status.auth_required,
                challenge_detected=status.challenge_detected,
                session_expired=status.session_expired,
                transport=status.transport,
                fallback_used=status.fallback_used,
            )
        article, _ = await translate_article_if_enabled(
            self._owner.ctx, article, auto_translate=self._owner.auto_translate()
        )
        return article


class LinuxdoPlugin(BasePlugin):
    """OmniDesk v2 Linux.do Discourse client (`text.source.v1`, `text.comments.v1`)."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        allow_loopback_http: bool = False,
        http_client: HttpClientProtocol | None = None,
    ) -> None:
        self._default_base_url = base_url.rstrip("/") or DEFAULT_BASE_URL
        self._allow_loopback_http = allow_loopback_http
        self.auth = AuthManager()
        self.subscriptions = SubscriptionStore()
        self.client = DiscourseClient(
            http_client=http_client,
            auth=self.auth,
            subs=self.subscriptions,
            allow_loopback_http=allow_loopback_http,
        )
        self.text_source = LinuxdoTextSource(self)

    async def on_load(self, ctx: PluginContext) -> None:
        """Non-blocking plugin initialization (< 50 ms)."""
        await super().on_load(ctx)
        self.auth.set_context(ctx)
        self.client.set_context(ctx)
        self.subscriptions.set_data_dir(ctx.data_dir)
        self.apply_config()

        ctx.register_text_source(self.text_source)
        ctx.register_capability(
            "text.source.v1",
            self.handle_text_source_rpc,
            metadata={
                "name": "Linux.do Discourse Client",
                "version": "0.2.0",
                "actions": list(_TEXT_SOURCE_ACTIONS),
            },
        )
        ctx.register_capability(
            "text.comments.v1",
            self.handle_comments_rpc,
            metadata={
                "name": "Linux.do 嵌套评论树",
                "version": "0.2.0",
                "actions": ["fetch_comments"],
            },
        )
        ctx.register_workspace_tab(self._build_workspace_tab_spec())

    async def on_unload(self) -> None:
        self.client.set_context(None)
        self.auth.set_context(None)
        self.ctx = None

    async def on_health_check(self) -> dict[str, Any]:
        """Lightweight offline readiness report without network calls or secrets."""
        try:
            self.apply_config()
            base_url = self.client.base_url
            base_valid = True
        except LinuxdoError:
            base_url = ""
            base_valid = False
        ctx = self.ctx
        return {
            "ok": base_valid,
            "plugin_id": ctx.plugin_id if ctx is not None else "plugin-linuxdo",
            "base_url": base_url,
            "last_transport": self.client.last_transport,
            "fallback_count": self.client.fallback_count,
            "pinned_categories_count": len(self.subscriptions.list_pinned()),
            "auth": self.auth.safe_summary(),
            "llm_companion_available": bool(ctx and ctx.capabilities.has("llm.chat.v1")),
            "llm_translate_available": bool(ctx and ctx.capabilities.has("llm.translate.v1")),
        }

    def auto_translate(self) -> bool:
        if self.ctx is None:
            return False
        return _coerce_bool(self.ctx.get_config("auto_translate", False), False)

    def apply_config(self) -> None:
        if self.ctx is None:
            self.client.configure(base_url=self._default_base_url)
            self.auth.configure(base_url=self._default_base_url)
            return
        cfg = self.ctx.get_config()
        cookie_mode = str(cfg.get("chrome_cookie_source") or "auto").strip()
        cookie_file = str(cfg.get("cookie_file_path") or "").strip()
        base_url = str(cfg.get("base_url") or self._default_base_url).strip() or self._default_base_url
        prefer_chrome = _coerce_bool(cfg.get("prefer_chrome_fetch", True), True)
        auto_login = _coerce_bool(cfg.get("auto_login_on_expire", True), True)
        timeout_sec = _coerce_float(cfg.get("request_timeout_sec", 15), 15.0)
        pinned_spec = str(cfg.get("pinned_categories") or "").strip()

        self.client.configure(base_url=base_url, timeout_sec=timeout_sec, prefer_chrome_fetch=prefer_chrome)
        self.auth.configure(
            mode=cookie_mode,
            cookie_file_path=cookie_file,
            base_url=self.client.base_url,
            auto_login_on_expire=auto_login,
        )
        self.subscriptions.set_data_dir(self.ctx.data_dir)
        if pinned_spec:
            self.subscriptions.apply_config_pinned(pinned_spec)

    def _status_error_payload(self, status: FetchStatus) -> dict[str, Any]:
        secrets = self.auth.secrets_for_redaction()
        code = status.error_code or (
            "not_configured" if (status.auth_required or status.challenge_detected) else "service_offline"
        )
        msg = redact_text(status.error or "Linux.do 请求失败", secrets)
        reason = (
            "challenge_detected"
            if status.challenge_detected
            else ("session_expired" if status.session_expired else ("auth_required" if status.auth_required else code))
        )
        return {
            "error_code": code,
            "error": msg,
            "reason": reason,
            "http_status": status.http_status,
            "transport": status.transport,
            "fallback_used": status.fallback_used,
            "auth_required": status.auth_required,
            "challenge_detected": status.challenge_detected,
            "session_expired": status.session_expired,
        }

    def _fail(self, code: str, message: str, *, data: Mapping[str, Any] | None = None) -> CapabilityResult:
        safe_msg = redact_text(message, self.auth.secrets_for_redaction())
        extra = dict(data or {})
        extra["ok"] = False
        extra["error"] = safe_msg
        return CapabilityResult.fail(safe_msg, data=extra, error_code=code)

    def _fail_status(self, status: FetchStatus) -> CapabilityResult:
        payload = self._status_error_payload(status)
        code = str(payload.pop("error_code"))
        msg = str(payload.pop("error"))
        return self._fail(code, msg, data=payload)

    async def handle_text_source_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Handle `text.source.v1` actions cleanly and fail-closed."""
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        try:
            self.apply_config()
        except LinuxdoError as exc:
            return self._fail(exc.error_code, str(exc))

        if "action" not in req_obj or req_obj.get("action") is None:
            if any(req_obj.get(k) for k in ("article_id", "id", "url")):
                action = "fetch_article"
            elif any(req_obj.get(k) is not None for k in ("channel_id", "query")):
                action = "fetch_feed"
            else:
                await self.auth.resolve_cookies(persist_chrome_snapshot=True)
                chans = self.text_source.channels()
                return CapabilityResult.ok(
                    {
                        "plugin_id": self.ctx.plugin_id if self.ctx else "plugin-linuxdo",
                        "channels": [c.model_dump() for c in chans],
                        "auth": self.auth.safe_summary(),
                        "echo": req_obj,
                    }
                )
        else:
            action = json_str(req_obj, "action", "")
            if not action:
                return self._fail("invalid_argument", "action must be a non-empty string")

        try:
            if action == "channels":
                await self.auth.resolve_cookies(persist_chrome_snapshot=True)
                chans = self.text_source.channels()
                return CapabilityResult.ok(
                    {"channels": [c.model_dump() for c in chans], "auth": self.auth.safe_summary()}
                )

            if action in {"fetch_feed", "search"}:
                channel_id = json_str(req_obj, "channel_id", "")
                query = json_str(req_obj, "query", "")
                page = _parse_optional_int(req_obj, "page", 1)
                if page < 1:
                    return self._fail("invalid_argument", "page 必须为 >= 1 的整数")
                items, status = await self.client.fetch_topic_list(
                    channel_id=channel_id, query=query, page=page
                )
                if not status.ok:
                    if status.auth_required or status.challenge_detected:
                        if self.auth.auto_login_on_expire and (
                            status.session_expired or status.challenge_detected or bool(channel_id.strip())
                        ):
                            await self.auth.open_login_window(force=False)
                    return self._fail_status(status)
                return CapabilityResult.ok(
                    {
                        "items": [it.model_dump() for it in items],
                        "count": len(items),
                        "page": page,
                        "transport": status.transport,
                        "fallback_used": status.fallback_used,
                        "auth_required": False,
                        "challenge_detected": False,
                        "session_expired": False,
                    }
                )

            if action == "fetch_article":
                aid = json_str_chain(req_obj, ("article_id", "id", "url"))
                if not aid:
                    return self._fail("invalid_argument", "article_id, id, or url is required")
                max_posts = max(1, min(_parse_optional_int(req_obj, "max_posts", 60), 200))
                article, status = await self.client.fetch_topic_article(aid, max_initial_posts=max_posts)
                if not status.ok or article is None:
                    if status.auth_required or status.challenge_detected:
                        if self.auth.auto_login_on_expire and (
                            status.session_expired or status.challenge_detected
                        ):
                            await self.auth.open_login_window(force=False)
                    return self._fail_status(status)

                want_tr = _coerce_bool(
                    req_obj.get("translate", req_obj.get("auto_translate", self.auto_translate())),
                    self.auto_translate(),
                )
                target_lang = json_str(req_obj, "target_lang", "zh-CN") or "zh-CN"
                article, tr_meta = await translate_article_if_enabled(
                    self.ctx, article, auto_translate=want_tr, target_lang=target_lang
                )
                return CapabilityResult.ok(
                    {
                        "article": article.model_dump(),
                        "transport": status.transport,
                        "fallback_used": status.fallback_used,
                        "auth_required": False,
                        **tr_meta,
                    }
                )

            if action in {"login", "open_login"}:
                opened, err = await self.auth.open_login_window(force=True)
                if not opened:
                    return self._fail("service_offline", err or "无法呼起 Chrome 登录窗口")
                return CapabilityResult.ok(
                    {
                        "opened": True,
                        "login_url": f"{self.client.base_url}/login",
                        "auth": self.auth.safe_summary(),
                    }
                )

            if action == "sync_cookies":
                synced, err, res = await self.auth.sync_from_chrome()
                if not synced:
                    return self._fail(
                        "not_configured",
                        err or "Chrome 中暂无有效 _t 登录凭据",
                        data={
                            "synced": False,
                            "has_t_cookie": res.has_t_cookie,
                            "has_session_cookie": res.has_session_cookie,
                            "has_cf_clearance": res.has_cf_clearance,
                            "auth": self.auth.safe_summary(),
                        },
                    )
                return CapabilityResult.ok(
                    {
                        "synced": True,
                        "has_t_cookie": res.has_t_cookie,
                        "has_session_cookie": res.has_session_cookie,
                        "has_cf_clearance": res.has_cf_clearance,
                        "auth": self.auth.safe_summary(),
                    }
                )

            if action == "auth_status":
                await self.auth.resolve_cookies(persist_chrome_snapshot=True)
                return CapabilityResult.ok({"auth": self.auth.safe_summary()})

            if action == "set_cookie":
                hdr = json_str_chain(req_obj, ("cookie_header", "cookie"))
                if not hdr:
                    return self._fail("invalid_argument", "cookie_header is required")
                self.auth.set_manual_cookie_header(hdr)
                return CapabilityResult.ok({"auth": self.auth.safe_summary()})

            if action == "logout":
                self.auth.logout()
                return CapabilityResult.ok({"auth": self.auth.safe_summary()})

            if action == "list_categories":
                refresh = json_bool(req_obj, "refresh", False)
                status = FetchStatus(ok=True, transport=self.client.last_transport)
                if refresh or not self.subscriptions.list_discovered():
                    _, status = await self.client.fetch_categories()
                    if not status.ok and (refresh or not self.subscriptions.list_discovered()):
                        return self._fail_status(status)
                pinned = [c.to_dict() for c in self.subscriptions.list_pinned()]
                discovered = [c.to_dict() for c in self.subscriptions.list_discovered()]
                return CapabilityResult.ok(
                    {"pinned": pinned, "categories": discovered, "transport": status.transport}
                )

            if action == "pin_category":
                cid = _parse_optional_int(req_obj, "id", 0)
                slug = json_str(req_obj, "slug", "")
                name = json_str(req_obj, "name", "")
                if not self.subscriptions.pin_category(cid, slug, name):
                    return self._fail("invalid_argument", "pin_category requires id or slug")
                chans = self.text_source.channels()
                return CapabilityResult.ok({"channels": [c.model_dump() for c in chans]})

            if action == "unpin_category":
                key = json_str_chain(req_obj, ("slug", "key"))
                if not key and "id" in req_obj and req_obj["id"] is not None:
                    raw_id = req_obj["id"]
                    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                        key = str(raw_id)
                    elif isinstance(raw_id, str):
                        key = raw_id
                if not self.subscriptions.unpin_category(key):
                    return self._fail("invalid_argument", "category not found in pinned list")
                chans = self.text_source.channels()
                return CapabilityResult.ok({"channels": [c.model_dump() for c in chans]})

            if action in {"summarize", "companion", "ask"}:
                return await self._handle_summarize(req_obj)

            return self._fail("invalid_argument", f"Unsupported text.source.v1 action: {action}")
        except LinuxdoError as exc:
            return self._fail(exc.error_code, str(exc))

    async def handle_comments_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Handle `text.comments.v1` nested comment tree pagination."""
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        try:
            self.apply_config()
            raw_action = req_obj.get("action")
            action = "fetch_comments" if raw_action is None else json_str(req_obj, "action", "")
            if action != "fetch_comments":
                return self._fail("invalid_argument", f"Unsupported text.comments.v1 action: {action or raw_action!r}")

            aid = json_str_chain(req_obj, ("article_id", "id", "url"))
            if not aid and req_obj.get("topic_id") is not None:
                raw_tid = req_obj["topic_id"]
                if isinstance(raw_tid, int) and not isinstance(raw_tid, bool):
                    aid = str(raw_tid)
                elif isinstance(raw_tid, str):
                    aid = raw_tid
            if not aid:
                return self._fail("invalid_argument", "article_id, id, url, or topic_id is required")

            offset = _parse_optional_int(req_obj, "offset", 0)
            limit = _parse_optional_int(req_obj, "limit", 40)
            if offset < 0:
                return self._fail("invalid_argument", "offset 必须 >= 0")
            if limit < 1:
                return self._fail("invalid_argument", "limit 必须 >= 1")

            page_res, status = await self.client.fetch_topic_comments_page(
                aid, offset=offset, limit=limit
            )
            if not status.ok:
                if status.auth_required or status.challenge_detected:
                    if self.auth.auto_login_on_expire and (
                        status.session_expired or status.challenge_detected
                    ):
                        await self.auth.open_login_window(force=False)
                return self._fail_status(status)

            want_tr = _coerce_bool(
                req_obj.get("translate", req_obj.get("auto_translate", self.auto_translate())),
                self.auto_translate(),
            )
            comments = await translate_comments_if_enabled(
                self.ctx,
                page_res.comments,
                auto_translate=want_tr,
                target_lang=json_str(req_obj, "target_lang", "zh-CN") or "zh-CN",
            )
            return CapabilityResult.ok(
                {
                    "comments": [c.model_dump() for c in comments],
                    "total_posts": page_res.total_posts,
                    "loaded_count": page_res.loaded_count,
                    "offset": page_res.offset,
                    "next_offset": page_res.next_offset,
                    "has_more": page_res.has_more,
                    "transport": page_res.transport,
                }
            )
        except LinuxdoError as exc:
            return self._fail(exc.error_code, str(exc))

    async def _handle_summarize(self, req: Mapping[str, Any]) -> CapabilityResult:
        raw_article = req.get("article")
        if isinstance(raw_article, Mapping):
            try:
                article = TextArticle.model_validate(dict(raw_article))
            except ValidationError as exc:
                return self._fail("invalid_argument", f"article 参数格式无效: {exc}")
        else:
            aid = json_str_chain(req, ("article_id", "id", "url"))
            if not aid:
                return self._fail("invalid_argument", "article or article_id is required for summarization")
            article, status = await self.client.fetch_topic_article(aid, max_initial_posts=60)
            if not status.ok or article is None:
                return self._fail_status(status)

        question = json_str_chain(req, ("question", "prompt")).strip()
        return await summarize_article_via_llm(
            self.ctx, article, question=question, secrets=self.auth.secrets_for_redaction()
        )

    def _build_ui_schema(self) -> dict[str, Any]:
        summary = self.auth.safe_summary()
        return {
            "type": "Section",
            "id": "linuxdo-workspace-root",
            "title": "Linux.do Discourse 社区",
            "description": "技术话题、代码块与公式保真排版、嵌套评论树及 Chrome Cloudflare 代理会话",
            "children": [
                {
                    "type": "KeyValue",
                    "id": "linuxdo-auth-status",
                    "items": [
                        {"key": "cookie_source", "label": "Cookie 来源", "value": summary.get("cookie_source", "guest")},
                        {"key": "authenticated", "label": "登录状态", "value": "已登录" if summary.get("authenticated") else "未登录"},
                        {"key": "cf_clearance", "label": "Cloudflare 凭证", "value": "已检测" if summary.get("has_cf_clearance") else "未检测"},
                        {"key": "transport", "label": "最近通道", "value": self.client.last_transport},
                    ],
                },
                {
                    "type": "Form",
                    "id": "linuxdo-search-form",
                    "fields": [
                        {"name": "query", "label": "搜索关键词或帖子链接", "type": "string", "default": ""},
                        {"name": "channel_id", "label": "栏目 ID", "type": "string", "default": "latest"},
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "linuxdo-actions",
                    "actions": [
                        {"id": "fetch_feed", "label": "刷新话题", "variant": "primary"},
                        {"id": "login", "label": "Chrome 登录", "variant": "secondary"},
                        {"id": "sync_cookies", "label": "同步 Cookie", "variant": "secondary"},
                        {"id": "list_categories", "label": "发现版面", "variant": "secondary"},
                    ],
                },
            ],
        }

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="linuxdo",
            title="Linux.do",
            domain="text",
            badge="LDO",
            ui_schema=self._build_ui_schema(),
            render=self._build_ui_schema,
            on_action=self.handle_tab_action,
        )

    async def handle_tab_action(self, action_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = {**(dict(state) if state is not None else {}), "action": action_id}
        res = await self.handle_text_source_rpc(payload)
        return {"ok": res.ok, "result": res.data, "error": res.error, "ui_schema": self._build_ui_schema()}


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `LinuxdoPlugin` instance."""
    return LinuxdoPlugin()
