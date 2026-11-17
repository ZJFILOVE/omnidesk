"""OmniDesk v2 plugin-reddit: Reddit TextSource, deep comment tree, Chrome auth, and AI companion."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omnidesk_sdk import (
    BasePlugin,
    CapabilityResult,
    CommentNode,
    PluginContext,
    TextArticle,
    WorkspaceTabSpec,
)

if __package__:
    from .reddit_auth import AuthManager, REDDIT_LOGIN_URL
    from .reddit_http import RedditHttpClient
    from .reddit_security import RedditPluginError, redact_reddit_secrets
    from .reddit_source import RedditTextSource
    from .reddit_subscriptions import SubscriptionStore, normalize_subreddit_name
    from .reddit_translate import (
        run_companion_llm,
        translate_article_if_enabled,
        translate_comments_if_enabled,
    )
else:
    _PLUGIN_ROOT = str(Path(__file__).resolve().parent)
    if _PLUGIN_ROOT not in sys.path:
        sys.path.insert(0, _PLUGIN_ROOT)
    from reddit_auth import AuthManager, REDDIT_LOGIN_URL
    from reddit_http import RedditHttpClient
    from reddit_security import RedditPluginError, redact_reddit_secrets
    from reddit_source import RedditTextSource
    from reddit_subscriptions import SubscriptionStore, normalize_subreddit_name
    from reddit_translate import (
        run_companion_llm,
        translate_article_if_enabled,
        translate_comments_if_enabled,
    )

_TEXT_SOURCE_ACTIONS = (
    "channels",
    "fetch_feed",
    "fetch_article",
    "search",
    "list_subscriptions",
    "list_subreddits",
    "subscribe",
    "add_subreddit",
    "unsubscribe",
    "remove_subreddit",
    "sync_subscriptions",
    "login",
    "open_login",
    "sync_cookies",
    "auth_status",
    "set_cookie_header",
    "set_cookie",
    "set_oauth_token",
    "logout",
    "companion",
    "summarize",
    "ask",
)
_COMMENTS_ACTIONS = ("fetch_comments", "fetch_more_comments", "expand_more")
_AUTH_ACTIONS = ("status", "login", "sync_cookies", "set_cookie_header", "set_oauth_token", "logout")


def _json_str(obj: Mapping[str, Any], key: str, default: str = "") -> str:
    val = obj.get(key)
    return val.strip() if isinstance(val, str) else default


def _json_str_chain(obj: Mapping[str, Any], keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return default


def _coerce_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    if isinstance(val, int):
        return val != 0
    return default


def _parse_int(req: Mapping[str, Any], key: str, default: int, *, minimum: int = 1) -> int:
    if key not in req or req[key] is None:
        return default
    val = req[key]
    if isinstance(val, bool) or not isinstance(val, int):
        raise RedditPluginError("invalid_argument", f"{key} 必须为整数")
    if val < minimum:
        raise RedditPluginError("invalid_argument", f"{key} 必须 >= {minimum}")
    return val


class RedditPlugin(BasePlugin):
    """OmniDesk v2 pure Python plugin for Reddit technical subreddits and comment trees."""

    def __init__(
        self,
        *,
        api_base_url: str = "",
        allow_loopback_http: bool = False,
    ) -> None:
        self.auth = AuthManager()
        self.http = RedditHttpClient(api_base_url=api_base_url, allow_loopback_http=allow_loopback_http)
        self.subscriptions = SubscriptionStore()
        self.text_source = RedditTextSource(
            self.auth,
            self.http,
            self.subscriptions,
            on_reload_config=self.reload_config,
        )

    async def on_load(self, ctx: PluginContext) -> None:
        """Non-blocking plugin initialization (< 50 ms)."""
        await super().on_load(ctx)
        self.auth.set_context(ctx)
        self.http.set_context(ctx)
        self.text_source.set_context(ctx)
        try:
            self.subscriptions.set_data_dir(ctx.data_dir)
        except RedditPluginError:
            pass
        self.reload_config()

        ctx.register_text_source(self.text_source)
        ctx.register_capability(
            "text.source.v1",
            self.handle_text_source_rpc,
            metadata={
                "name": "Reddit Technical Subreddits",
                "version": "0.2.0",
                "actions": list(_TEXT_SOURCE_ACTIONS),
            },
        )
        ctx.register_capability(
            "text.comments.v1",
            self.handle_comments_rpc,
            metadata={
                "name": "Reddit Deep Recursive Comment Tree",
                "version": "0.2.0",
                "actions": list(_COMMENTS_ACTIONS),
            },
        )
        ctx.register_capability(
            "reddit.auth.v1",
            self.handle_auth_rpc,
            metadata={
                "name": "Reddit Session & Chrome Auth",
                "version": "0.2.0",
                "actions": list(_AUTH_ACTIONS),
            },
        )
        ctx.register_workspace_tab(self._build_workspace_tab_spec())

    async def on_unload(self) -> None:
        """Clear transient caches and detach PluginContext."""
        self.text_source.clear_caches()
        self.text_source.set_context(None)
        self.http.set_context(None)
        self.auth.set_context(None)
        self.ctx = None

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight offline readiness status without leaking secrets."""
        self.reload_config()
        ctx = self.ctx
        return {
            "ok": True,
            "plugin_id": ctx.plugin_id if ctx is not None else "plugin-reddit",
            "source_id": self.text_source.source_id,
            "user_agent": self.http.user_agent(),
            "subscribed_subreddits": len(self.subscriptions.list_subscriptions()),
            "auth": self.auth.safe_status(),
            "llm_companion_available": bool(ctx and ctx.capabilities.has("llm.chat.v1")),
            "llm_translate_available": bool(ctx and ctx.capabilities.has("llm.translate.v1")),
        }

    def reload_config(self) -> None:
        """Synchronize config from ConfigStore and credentials from 0600 SecretVault."""
        if self.ctx is not None:
            self.auth.load_from_context(self.ctx)
            try:
                self.subscriptions.set_data_dir(self.ctx.data_dir)
            except RedditPluginError:
                pass
        cfg = self.auth.config()
        self.http.set_default_timeout_sec(cfg.request_timeout_sec)
        self.http.set_user_agent(cfg.user_agent)
        self.subscriptions.apply_config_defaults(cfg.subreddits)

    def _fail(
        self,
        error_code: str,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> CapabilityResult:
        safe_msg = redact_reddit_secrets(message, self.auth.secret_values())
        payload = dict(data or {})
        payload["ok"] = False
        payload["error"] = safe_msg
        return CapabilityResult.fail(safe_msg, data=payload, error_code=error_code)

    async def handle_text_source_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Dispatch `text.source.v1` actions aligned with v1 C++ manifest and fail-closed v2 rules."""
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        try:
            self.reload_config()
        except RedditPluginError as exc:
            return self._fail(exc.error_code, str(exc))

        if (
            "action" not in req_obj
            and "id" not in req_obj
            and "article_id" not in req_obj
            and "url" not in req_obj
            and "channel_id" not in req_obj
            and "subreddit" not in req_obj
            and "query" not in req_obj
        ):
            await self.auth.resolve_for_url("https://www.reddit.com/")
            chans = self.text_source.channels()
            return CapabilityResult.ok(
                {
                    "ok": True,
                    "plugin_id": self.ctx.plugin_id if self.ctx is not None else "plugin-reddit",
                    "channels": [c.model_dump() for c in chans],
                    "subreddits": [s.to_dict() for s in self.subscriptions.list_subscriptions()],
                    "auth": self.auth.safe_status(),
                    "echo": req_obj,
                }
            )

        raw_action = req_obj.get("action")
        if raw_action is None:
            action = "fetch_article" if any(k in req_obj for k in ("id", "article_id", "url")) else "fetch_feed"
        else:
            action = _json_str(req_obj, "action", "")
            if not action:
                return self._fail("invalid_argument", "action must be a non-empty string")

        try:
            if action == "channels":
                resolved = await self.auth.resolve_for_url("https://www.reddit.com/")
                chans = self.subscriptions.build_channels(resolved.is_authenticated())
                return CapabilityResult.ok(
                    {
                        "ok": True,
                        "channels": [c.model_dump() for c in chans],
                        "subreddits": [s.to_dict() for s in self.subscriptions.list_subscriptions()],
                        "auth": self.auth.safe_status(resolved),
                    }
                )

            if action == "fetch_feed":
                return await self._rpc_fetch_feed(req_obj)

            if action == "search":
                return await self._rpc_search(req_obj)

            if action == "fetch_article":
                return await self._rpc_fetch_article(req_obj)

            if action in {"list_subscriptions", "list_subreddits"}:
                return CapabilityResult.ok(
                    {
                        "ok": True,
                        "subreddits": [s.to_dict() for s in self.subscriptions.list_subscriptions()],
                        "channels": [c.model_dump() for c in self.text_source.channels()],
                        "subscriptions_file": self.subscriptions.file_path(),
                    }
                )

            if action in {"subscribe", "add_subreddit"}:
                name = _json_str_chain(req_obj, ("subreddit", "name", "channel_id"))
                disp = _json_str(req_obj, "display_name", "")
                sort = _json_str(req_obj, "default_sort", "hot") or "hot"
                if not self.subscriptions.subscribe(name, disp, sort):
                    return self._fail("invalid_argument", f"Invalid subreddit name: {name}")
                return CapabilityResult.ok(
                    {
                        "ok": True,
                        "subscribed": normalize_subreddit_name(name),
                        "subreddits": [s.to_dict() for s in self.subscriptions.list_subscriptions()],
                        "channels": [c.model_dump() for c in self.text_source.channels()],
                    }
                )

            if action in {"unsubscribe", "remove_subreddit"}:
                name = _json_str_chain(req_obj, ("subreddit", "name", "channel_id"))
                norm = normalize_subreddit_name(name)
                if not norm:
                    return self._fail("invalid_argument", f"Invalid subreddit name: {name}")
                if not self.subscriptions.unsubscribe(norm):
                    return self._fail("not_found", f"Subreddit not found in subscriptions: {name}")
                return CapabilityResult.ok(
                    {
                        "ok": True,
                        "unsubscribed": norm,
                        "subreddits": [s.to_dict() for s in self.subscriptions.list_subscriptions()],
                        "channels": [c.model_dump() for c in self.text_source.channels()],
                    }
                )

            if action == "sync_subscriptions":
                merged = await self.text_source.sync_remote_subscriptions()
                return CapabilityResult.ok(
                    {
                        "ok": True,
                        "merged": merged,
                        "subreddits": [s.to_dict() for s in self.subscriptions.list_subscriptions()],
                        "channels": [c.model_dump() for c in self.text_source.channels()],
                    }
                )

            if action in {
                "login",
                "open_login",
                "sync_cookies",
                "auth_status",
                "set_cookie_header",
                "set_cookie",
                "set_oauth_token",
                "logout",
            }:
                auth_req = dict(req_obj)
                if action == "auth_status":
                    auth_req["action"] = "status"
                elif action == "open_login":
                    auth_req["action"] = "login"
                elif action == "set_cookie":
                    auth_req["action"] = "set_cookie_header"
                return await self.handle_auth_rpc(auth_req)

            if action in {"companion", "summarize", "ask"}:
                return await self._rpc_companion(req_obj)

            return self._fail("invalid_argument", f"Unknown text.source.v1 action: {action}")
        except RedditPluginError as exc:
            err_data = dict(exc.details)
            if action in {"fetch_feed", "search"}:
                err_data.setdefault("items", [])
            return self._fail(exc.error_code, str(exc), data=err_data)

    async def _rpc_fetch_feed(self, req_obj: Mapping[str, Any]) -> CapabilityResult:
        channel_id = _json_str_chain(req_obj, ("channel_id", "subreddit"))
        query = _json_str(req_obj, "query", "")
        after = _json_str(req_obj, "after", "")
        page = _parse_int(req_obj, "page", 1, minimum=1)
        limit = _parse_int(req_obj, "limit", 25, minimum=1)
        force_refresh = _coerce_bool(req_obj.get("force_refresh"), False)

        cursor_key = f"{channel_id.strip().lower()}\n{query.strip()}"
        if not after and page > 1:
            after = self.text_source._page_cursors.get((cursor_key, page), "")
            if not after:
                resolved = await self.auth.resolve_for_url("https://www.reddit.com/")
                return CapabilityResult.ok(
                    {
                        "ok": True,
                        "channel_id": channel_id,
                        "items": [],
                        "count": 0,
                        "after": "",
                        "before": "",
                        "has_more": False,
                        "page": page,
                        "authenticated": resolved.is_authenticated(),
                        "auth_required": False,
                    }
                )
        elif not after and page == 1 and force_refresh:
            self.text_source._clear_cursors_for(cursor_key)

        res, transport = await self.text_source.fetch_feed_listing(
            channel_id=channel_id,
            query=query,
            after=after,
            limit=limit,
        )
        if not req_obj.get("after"):
            self.text_source._remember_cursor(cursor_key, page + 1, res.after)
        resolved = await self.auth.resolve_for_url("https://www.reddit.com/")
        return CapabilityResult.ok(
            {
                "ok": True,
                "channel_id": channel_id,
                "items": [it.model_dump() for it in res.items],
                "count": len(res.items),
                "after": res.after,
                "before": res.before,
                "has_more": bool(res.after),
                "page": page,
                "transport": transport,
                "authenticated": resolved.is_authenticated(),
                "auth_required": False,
            }
        )

    async def _rpc_search(self, req_obj: Mapping[str, Any]) -> CapabilityResult:
        query = _json_str(req_obj, "query", "")
        if not query:
            return self._fail("invalid_argument", "query is required for search", data={"items": []})
        sub = _json_str_chain(req_obj, ("subreddit", "channel_id"), "search") or "search"
        after = _json_str(req_obj, "after", "")
        limit = _parse_int(req_obj, "limit", 25, minimum=1)
        res, transport = await self.text_source.fetch_feed_listing(
            channel_id=sub,
            query=query,
            after=after,
            limit=limit,
        )
        return CapabilityResult.ok(
            {
                "ok": True,
                "query": query,
                "items": [it.model_dump() for it in res.items],
                "count": len(res.items),
                "after": res.after,
                "before": res.before,
                "has_more": bool(res.after),
                "transport": transport,
            }
        )

    async def _rpc_fetch_article(self, req_obj: Mapping[str, Any]) -> CapabilityResult:
        aid = _json_str_chain(req_obj, ("id", "article_id", "url"))
        if not aid:
            return self._fail("invalid_argument", "id, article_id, or url is required")
        sort = _json_str(req_obj, "sort", "")
        limit = _parse_int(req_obj, "limit", 200, minimum=1)
        depth = _parse_int(req_obj, "depth", 10, minimum=1)
        expand_more = _coerce_bool(req_obj.get("expand_more"), True)

        parsed, transport = await self.text_source.fetch_article_detail(
            aid, sort=sort, limit=limit, depth=depth, expand_more=expand_more
        )
        cfg = self.auth.config()
        want_tr = _coerce_bool(req_obj.get("translate", req_obj.get("auto_translate", cfg.auto_translate)), cfg.auto_translate)
        target_lang = _json_str(req_obj, "target_lang", cfg.target_lang) or cfg.target_lang

        article, tr_meta = await translate_article_if_enabled(
            self.ctx, parsed.article, auto_translate=want_tr, target_lang=target_lang
        )
        if want_tr and article.comments:
            tr_comments, _, _ = await translate_comments_if_enabled(
                self.ctx, article.comments, auto_translate=True, title=article.title, target_lang=target_lang
            )
            article = article.model_copy(update={"comments": tr_comments})

        return CapabilityResult.ok(
            {
                "ok": True,
                "article": article.model_dump(),
                "link_fullname": parsed.link_fullname,
                "subreddit": parsed.subreddit,
                "more_stubs": [s.to_dict() for s in parsed.more_stubs],
                "transport": transport,
                **tr_meta,
            }
        )

    async def handle_comments_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Dispatch `text.comments.v1` actions (`fetch_comments`, `fetch_more_comments`, `expand_more`)."""
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        try:
            self.reload_config()
            raw_action = req_obj.get("action")
            action = "fetch_comments" if raw_action is None else _json_str(req_obj, "action", "")
            if not action:
                return self._fail("invalid_argument", "action must be a non-empty string")

            if action == "fetch_comments":
                aid = _json_str_chain(req_obj, ("id", "article_id", "url"))
                if not aid:
                    return self._fail("invalid_argument", "id, article_id, or url is required")
                sort = _json_str(req_obj, "sort", "confidence") or "confidence"
                limit = _parse_int(req_obj, "limit", 200, minimum=1)
                depth = _parse_int(req_obj, "depth", 10, minimum=1)
                expand_more = _coerce_bool(req_obj.get("expand_more"), True)

                res = await self.text_source.fetch_comments(
                    aid, sort=sort, limit=limit, depth=depth, expand_more=expand_more
                )
                cfg = self.auth.config()
                want_tr = _coerce_bool(
                    req_obj.get("translate", req_obj.get("auto_translate", cfg.auto_translate)),
                    cfg.auto_translate,
                )
                target_lang = _json_str(req_obj, "target_lang", cfg.target_lang) or cfg.target_lang
                comments, tr_degraded, tr_reason = await translate_comments_if_enabled(
                    self.ctx, res.comments, auto_translate=want_tr, target_lang=target_lang
                )
                return CapabilityResult.ok(
                    {
                        "ok": True,
                        "link_fullname": res.link_fullname,
                        "comments": [c.model_dump() for c in comments],
                        "count": len(comments),
                        "more_stubs": [s.to_dict() for s in res.more_stubs],
                        "transport": res.transport,
                        "translated": want_tr and not tr_degraded,
                        "translation_degraded": tr_degraded,
                        "degraded_reason": tr_reason,
                    }
                )

            if action in {"fetch_more_comments", "expand_more"}:
                link_id = _json_str_chain(req_obj, ("link_id", "link_fullname", "article_id"))
                post_author = _json_str(req_obj, "post_author", "")
                raw_children = req_obj.get("children")
                children_ids: list[str] = []
                if isinstance(raw_children, list):
                    children_ids = [str(c).strip() for c in raw_children if isinstance(c, str) and c.strip()]
                elif isinstance(raw_children, str):
                    children_ids = [tok.strip() for tok in raw_children.split(",") if tok.strip()]
                if not link_id or not children_ids:
                    return self._fail("invalid_argument", "link_id and children are required for fetch_more_comments")

                comments: list[CommentNode] = []
                raw_existing = req_obj.get("comments")
                if raw_existing is not None:
                    if not isinstance(raw_existing, list):
                        return self._fail("invalid_argument", "comments 必须为数组")
                    try:
                        comments = [CommentNode.model_validate(item) for item in raw_existing]
                    except ValidationError as exc:
                        return self._fail("invalid_argument", f"comments 格式无效: {exc}")

                attached = await self.text_source.expand_more_children(comments, link_id, post_author, children_ids)
                return CapabilityResult.ok(
                    {
                        "ok": True,
                        "attached": attached,
                        "comments": [c.model_dump() for c in comments],
                    }
                )

            return self._fail("invalid_argument", f"Unknown text.comments.v1 action: {action}")
        except RedditPluginError as exc:
            return self._fail(exc.error_code, str(exc), data=exc.details)

    async def handle_auth_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Dispatch `reddit.auth.v1` actions (`status`, `login`, `sync_cookies`, `set_*`, `logout`)."""
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        try:
            self.reload_config()
            raw_action = req_obj.get("action")
            action = "status" if raw_action is None else _json_str(req_obj, "action", "")
            if not action:
                return self._fail("invalid_argument", "action must be a non-empty string")

            if action == "status":
                resolved = await self.auth.resolve_for_url("https://www.reddit.com/")
                return CapabilityResult.ok({"ok": True, **self.auth.safe_status(resolved)})

            if action == "login":
                title = _json_str(req_obj, "title", "Reddit 登录（完成后关闭窗口即可）")
                res = await self.auth.open_login_window(title)
                if not res.ok:
                    code = str((res.data or {}).get("error_code") or "service_offline")
                    return self._fail(code, res.error or "打开 Reddit 登录窗口失败", data=res.data)
                return CapabilityResult.ok(
                    {
                        "ok": True,
                        "launched": True,
                        "login_url": REDDIT_LOGIN_URL,
                        "session": res.data,
                        "auth": self.auth.safe_status(),
                    }
                )

            if action == "sync_cookies":
                src, resolved = await self.auth.sync_from_chrome()
                return CapabilityResult.ok(
                    {
                        "ok": True,
                        "synced": True,
                        "source": src,
                        "auth": self.auth.safe_status(resolved),
                    }
                )

            if action == "set_cookie_header":
                hdr = _json_str_chain(req_obj, ("cookie_header", "cookie"))
                self.auth.save_vault_cookie_header(hdr)
                return CapabilityResult.ok({"ok": True, "saved": True, "auth": self.auth.safe_status()})

            if action == "set_oauth_token":
                tok = _json_str_chain(req_obj, ("oauth_token", "access_token"))
                self.auth.save_vault_oauth_token(tok)
                return CapabilityResult.ok({"ok": True, "saved": True, "auth": self.auth.safe_status()})

            if action == "logout":
                self.auth.clear_session()
                return CapabilityResult.ok({"ok": True, "cleared": True, "auth": self.auth.safe_status()})

            return self._fail("invalid_argument", f"Unknown reddit.auth.v1 action: {action}")
        except RedditPluginError as exc:
            return self._fail(exc.error_code, str(exc), data=exc.details)

    async def _rpc_companion(self, req_obj: Mapping[str, Any]) -> CapabilityResult:
        mode = _json_str_chain(req_obj, ("mode", "action"), "summarize") or "summarize"
        question = _json_str_chain(req_obj, ("question", "prompt"))
        raw_article = req_obj.get("article")
        if isinstance(raw_article, Mapping):
            try:
                article = TextArticle.model_validate(dict(raw_article))
            except ValidationError as exc:
                return self._fail("invalid_argument", f"article 参数格式无效: {exc}")
        else:
            aid = _json_str_chain(req_obj, ("id", "article_id", "url"))
            if not aid:
                return self._fail("invalid_argument", "article or article_id is required for companion reading")
            parsed, _ = await self.text_source.fetch_article_detail(aid)
            article = parsed.article
        return await run_companion_llm(
            self.ctx,
            article,
            mode=mode,
            question=question,
            secrets=self.auth.secret_values(),
        )

    def _build_ui_schema(self) -> dict[str, Any]:
        status = self.auth.safe_status()
        return {
            "type": "Section",
            "id": "reddit-workspace-root",
            "title": "Reddit Technical Subreddits",
            "description": "技术 Subreddit 订阅流、深层递归评论树、Chrome 会话同步与双语伴读",
            "children": [
                {
                    "type": "KeyValue",
                    "id": "reddit-auth-status",
                    "items": [
                        {"key": "source", "label": "会话来源", "value": status.get("source", "guest")},
                        {
                            "key": "authenticated",
                            "label": "登录状态",
                            "value": "已登录" if status.get("authenticated") else "游客模式",
                        },
                        {
                            "key": "subreddits",
                            "label": "已订阅板块",
                            "value": str(len(self.subscriptions.list_subscriptions())),
                        },
                    ],
                },
                {
                    "type": "Form",
                    "id": "reddit-action-form",
                    "fields": [
                        {"name": "channel_id", "label": "频道 / Subreddit", "type": "string", "default": "popular"},
                        {"name": "query", "label": "搜索关键词", "type": "string", "default": ""},
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "reddit-actions",
                    "actions": [
                        {"id": "fetch_feed", "label": "拉取帖子流", "variant": "primary"},
                        {"id": "login", "label": "Chrome 登录", "variant": "secondary"},
                        {"id": "sync_cookies", "label": "同步 Cookie", "variant": "secondary"},
                        {"id": "sync_subscriptions", "label": "同步订阅", "variant": "secondary"},
                    ],
                },
            ],
        }

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="reddit",
            title="Reddit",
            domain="text",
            badge="Reddit",
            ui_schema=self._build_ui_schema(),
            render=self._build_ui_schema,
            on_action=self.handle_tab_action,
        )

    async def handle_tab_action(self, action_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Route declarative workspace tab actions to `handle_text_source_rpc`."""
        payload = {**(dict(state) if state is not None else {}), "action": action_id}
        res = await self.handle_text_source_rpc(payload)
        return {"ok": res.ok, "result": res.data, "error": res.error, "ui_schema": self._build_ui_schema()}


def create_plugin() -> BasePlugin:
    """Manifest entrypoint (`plugin:create_plugin`)."""
    return RedditPlugin()
