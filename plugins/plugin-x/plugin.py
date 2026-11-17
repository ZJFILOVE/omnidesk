"""OmniDesk v2 plugin-x: X timelines, lists, threads, comments, auth, and bilingual reading."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from omnidesk_sdk import (
    BasePlugin,
    CapabilityResult,
    CommentNode,
    PluginContext,
    TextArticle,
    TextChannel,
    TextFeedItem,
    TextParagraph,
    TextSource,
    WorkspaceTabSpec,
)

if __package__:
    from .x_auth import AuthManager
    from .x_client import HttpRequest, HttpResponse, XClient
    from .x_parser import (
        ParsedXTarget,
        build_article_from_thread,
        parse_html_fallback,
        parse_lists_graphql,
        parse_single_tweet_result_graphql,
        parse_syndication_tweet_json,
        parse_target_id_or_url,
        parse_timeline_graphql,
        parse_tweet_detail_graphql,
    )
    from .x_security import XPluginError, redact_x_secrets
    from .x_subscriptions import PinnedList, XSubscriptionStore
else:
    _PLUGIN_DIR = str(Path(__file__).resolve().parent)
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from x_auth import AuthManager  # type: ignore[no-redef]
    from x_client import HttpRequest, HttpResponse, XClient  # type: ignore[no-redef]
    from x_parser import (  # type: ignore[no-redef]
        ParsedXTarget,
        build_article_from_thread,
        parse_html_fallback,
        parse_lists_graphql,
        parse_single_tweet_result_graphql,
        parse_syndication_tweet_json,
        parse_target_id_or_url,
        parse_timeline_graphql,
        parse_tweet_detail_graphql,
    )
    from x_security import XPluginError, redact_x_secrets  # type: ignore[no-redef]
    from x_subscriptions import PinnedList, XSubscriptionStore  # type: ignore[no-redef]

_TEXT_SOURCE_ACTIONS = (
    "channels",
    "fetch_feed",
    "fetch_article",
    "fetch_thread",
    "search",
    "translate_article",
    "translate_text",
    "auth_status",
    "login",
    "open_login",
    "sync_cookies",
    "set_cookie_header",
    "set_tokens",
    "logout",
    "list_subscriptions",
    "list_lists",
    "add_list",
    "pin_list",
    "remove_list",
    "unpin_list",
    "reload_config",
)
_COMMENT_ACTIONS = ("fetch_comments", "fetch_thread")
_SKIP_TRANSLATION_KINDS = frozenset({"code", "formula", "image"})
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# Keep auto-translation responsive: X timelines often return far more entries
# than the requested page size, and translating all of them blows past the
# desktop UI request timeout. Only the visible head of the feed is translated.
_MAX_AUTO_TRANSLATE_ITEMS = 5
# Hard ceiling for automatic translation so the desktop UI never times out on
# a slow LLM; untranslated text is a better outcome than missing feed data.
_AUTO_TRANSLATE_BUDGET_SEC = 12.0


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


def _optional_int(req: Mapping[str, Any], key: str, default: int, *, minimum: int = 0) -> int:
    value = req.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise XPluginError("invalid_argument", f"{key} 必须为整数")
    if value < minimum:
        raise XPluginError("invalid_argument", f"{key} 必须 >= {minimum}")
    return value


def _str_chain(req: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = req.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_translation_list(data: Mapping[str, Any], expected: int) -> list[str] | None:
    for key in ("translations", "translated_texts", "results", "segments"):
        raw = data.get(key)
        if isinstance(raw, list) and len(raw) == expected:
            values: list[str] = []
            for item in raw:
                text = (
                    str(item.get("translation") or item.get("translated") or item.get("text") or "").strip()
                    if isinstance(item, Mapping)
                    else str(item or "").strip()
                )
                if not text:
                    return None
                values.append(text)
            return values
    reply = data.get("reply") or data.get("translated") or data.get("translation")
    if not isinstance(reply, str) or not reply.strip():
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        return lines if len(lines) == expected else (cleaned if expected == 1 else None)
    if not isinstance(parsed, list) or len(parsed) != expected:
        return None
    values = [
        str(item.get("translation") or item.get("translated") or item.get("text") or "").strip()
        if isinstance(item, Mapping)
        else str(item or "").strip()
        for item in parsed
    ]
    return values if all(values) else None


async def _translate_texts(
    ctx: PluginContext | None,
    texts: Sequence[str],
    *,
    target_lang: str,
    title: str = "",
) -> list[str] | None:
    if ctx is None or not texts:
        return [] if not texts else None
    results: list[str] = []
    for start in range(0, len(texts), 20):
        chunk = list(texts[start : start + 20])
        translated: list[str] | None = None
        if ctx.capabilities.has("llm.translate.v1"):
            try:
                res = await ctx.capabilities.call(
                    "llm.translate.v1",
                    {
                        "action": "translate",
                        "texts": chunk,
                        "target_lang": target_lang,
                        "context_title": title,
                    },
                )
            except Exception:
                res = None
            if res is not None and res.ok and isinstance(res.data, Mapping):
                translated = _extract_translation_list(res.data, len(chunk))
        if translated is None and ctx.capabilities.has("llm.chat.v1"):
            prompt = (
                "Translate each item into "
                f"{target_lang}. Preserve code and URLs. Return only a JSON array of strings in order.\n"
                + json.dumps(chunk, ensure_ascii=False)
            )
            try:
                res = await ctx.capabilities.call(
                    "llm.chat.v1",
                    {"action": "chat", "domain": "text", "prompt": prompt},
                )
            except Exception:
                res = None
            if res is not None and res.ok and isinstance(res.data, Mapping):
                translated = _extract_translation_list(res.data, len(chunk))
        if translated is None:
            return None
        results.extend(translated)
    return results


def _translate_comment_nodes(
    nodes: Sequence[CommentNode],
    translated: list[str],
    cursor: list[int],
) -> list[CommentNode]:
    out: list[CommentNode] = []
    for node in nodes:
        idx = cursor[0]
        cursor[0] += 1
        replies = _translate_comment_nodes(node.replies, translated, cursor)
        out.append(node.model_copy(update={"translated_body": translated[idx], "replies": replies}))
    return out


async def _translate_article(
    ctx: PluginContext | None,
    article: TextArticle,
    target_lang: str,
) -> TextArticle:
    if ctx is None:
        return article
    texts: list[str] = []
    paragraph_indices: list[int] = []
    if article.title.strip() and not article.title_zh:
        texts.append(article.title)
    for idx, paragraph in enumerate(article.paragraphs):
        if paragraph.kind in _SKIP_TRANSLATION_KINDS or not paragraph.original.strip() or paragraph.translated:
            continue
        paragraph_indices.append(idx)
        texts.append(paragraph.original)

    comments: list[CommentNode] = []

    def _collect(nodes: Sequence[CommentNode]) -> None:
        for node in nodes:
            if node.body.strip() and not node.translated_body:
                comments.append(node)
                texts.append(node.body)
            _collect(node.replies)

    _collect(article.comments)
    translated = await _translate_texts(ctx, texts, target_lang=target_lang, title=article.title)
    if translated is None or len(translated) != len(texts):
        return article

    cursor = 0
    title_zh = article.title_zh
    if article.title.strip() and not article.title_zh:
        title_zh = translated[cursor]
        cursor += 1
    paragraphs = list(article.paragraphs)
    for idx in paragraph_indices:
        paragraphs[idx] = paragraphs[idx].model_copy(update={"translated": translated[cursor]})
        cursor += 1
    comment_translations = translated[cursor:]
    if comments:
        translated_comments = _translate_comment_nodes(article.comments, comment_translations, [0])
    else:
        translated_comments = list(article.comments)
    extra = dict(article.extra)
    extra["bilingual_translated"] = True
    return article.model_copy(
        update={
            "title_zh": title_zh,
            "paragraphs": paragraphs,
            "comments": translated_comments,
            "extra": extra,
        }
    )


class XTextSource(TextSource):
    """TextSource adapter for X timelines, lists, and threads."""

    source_id = "plugin-x"
    display_name = "X / Twitter"
    category = "x"

    def __init__(self, owner: XPlugin) -> None:
        self._owner = owner
        self.last_error: dict[str, Any] | None = None
        self._page_cursors: dict[tuple[str, str, int], str] = {}

    def channels(self) -> list[TextChannel]:
        self._owner.apply_config()
        return self._owner.subscriptions.build_channels()

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
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            return []
        key = (channel_id.strip(), query.strip())
        if page == 1 or force_refresh:
            self._page_cursors = {k: v for k, v in self._page_cursors.items() if k[:2] != key}
        cursor = self._page_cursors.get((*key, page), "")
        if page > 1 and not cursor:
            return []
        try:
            items, next_cursor, _raw = await self._owner.fetch_timeline(
                channel_id=channel_id,
                query=query,
                count=20,
                cursor=cursor,
            )
        except XPluginError as exc:
            self.last_error = {
                "error_code": exc.error_code,
                "error": exc.message,
                "http_status": exc.http_status,
            }
            raise
        if self._owner.auto_translate():
            items = await self._owner.translate_feed_items(items)
        if next_cursor:
            self._page_cursors[(*key, page + 1)] = next_cursor
        return items

    async def fetch_article(self, article_id: str) -> TextArticle:
        self.last_error = None
        self._owner.apply_config()
        try:
            article, _meta = await self._owner.fetch_article(article_id)
        except XPluginError as exc:
            self.last_error = {
                "error_code": exc.error_code,
                "error": exc.message,
                "http_status": exc.http_status,
            }
            raise
        if self._owner.auto_translate():
            article = await self._owner.translate_article(article)
        return article


class XPlugin(BasePlugin):
    """OmniDesk v2 pure Python X plugin."""

    def __init__(
        self,
        *,
        base_url: str = "https://x.com",
        syndication_base_url: str = "https://cdn.syndication.twimg.com",
        allow_loopback_http: bool = False,
        transport_override: Callable[[HttpRequest], Any] | None = None,
    ) -> None:
        self._default_base_url = base_url.rstrip("/") or "https://x.com"
        self._allow_loopback_http = bool(allow_loopback_http)
        self.auth = AuthManager()
        self.client = XClient(
            base_url=self._default_base_url,
            syndication_base_url=syndication_base_url,
            allow_loopback_http=self._allow_loopback_http,
            transport_override=transport_override,
        )
        self.subscriptions = XSubscriptionStore()
        self.text_source = XTextSource(self)

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
            metadata={"name": "X (Twitter) Timelines & Threads", "version": "0.2.0", "actions": list(_TEXT_SOURCE_ACTIONS)},
        )
        ctx.register_capability(
            "text.comments.v1",
            self.handle_comments_rpc,
            metadata={"name": "X Thread Comments", "version": "0.2.0", "actions": list(_COMMENT_ACTIONS)},
        )
        ctx.register_workspace_tab(self._build_workspace_tab_spec())

    async def on_unload(self) -> None:
        self.client.set_context(None)
        self.auth.set_context(None)
        self.ctx = None

    async def on_health_check(self) -> dict[str, Any]:
        try:
            self.apply_config()
            status = await self.auth.safe_status_async()
            config_error = ""
        except XPluginError as exc:
            status = self.auth.safe_status()
            config_error = exc.message
        ctx = self.ctx
        return {
            "ok": not config_error,
            "plugin_id": ctx.plugin_id if ctx is not None else "plugin-x",
            "config_error": config_error,
            "auth": status,
            "pinned_lists_count": len(self.subscriptions.list_pinned()),
            "llm_translate_available": bool(ctx and ctx.capabilities.has("llm.translate.v1")),
            "llm_chat_available": bool(ctx and ctx.capabilities.has("llm.chat.v1")),
        }

    def apply_config(self) -> None:
        if self.ctx is None:
            return
        self.auth.load_from_context(self.ctx)
        self.client.set_default_timeout_sec(self.auth.config.request_timeout_sec)
        self.subscriptions.set_data_dir(self.ctx.data_dir)
        self.subscriptions.apply_default_spec(self.auth.config.default_list_ids)

    def auto_translate(self) -> bool:
        return bool(self.auth.config.auto_translate)

    def target_lang(self) -> str:
        return self.auth.config.target_lang or "zh-CN"

    async def translate_feed_items(
        self,
        items: Sequence[TextFeedItem],
        *,
        limit: int = _MAX_AUTO_TRANSLATE_ITEMS,
    ) -> list[TextFeedItem]:
        targets = [
            idx
            for idx, item in enumerate(items)
            if item.title.strip()
            and not item.title_zh
            and not _CJK_RE.search(item.title)
        ][: max(0, limit)]
        if not targets:
            return list(items)
        try:
            translated = await asyncio.wait_for(
                _translate_texts(
                    self.ctx,
                    [items[idx].title for idx in targets],
                    target_lang=self.target_lang(),
                    title="X timeline",
                ),
                timeout=_AUTO_TRANSLATE_BUDGET_SEC,
            )
        except (asyncio.TimeoutError, TimeoutError):
            translated = None
        if translated is None:
            return list(items)
        out = list(items)
        for idx, text in zip(targets, translated):
            out[idx] = out[idx].model_copy(update={"title_zh": text, "bilingual_cached": True})
        return out

    async def translate_article(
        self,
        article: TextArticle,
        *,
        budget: float | None = _AUTO_TRANSLATE_BUDGET_SEC,
    ) -> TextArticle:
        try:
            return await asyncio.wait_for(
                _translate_article(self.ctx, article, self.target_lang()),
                timeout=budget,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return article

    def _fail(
        self,
        code: str,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> CapabilityResult:
        safe = redact_x_secrets(message, self.auth.extra_secrets())
        payload = dict(data or {})
        payload.update({"ok": False, "error": safe})
        return CapabilityResult.fail(safe, data=payload, error_code=code)

    async def fetch_timeline(
        self,
        *,
        channel_id: str,
        query: str,
        count: int,
        cursor: str,
    ) -> tuple[list[TextFeedItem], str, dict[str, Any]]:
        channel = (channel_id or "").strip()
        clean_query = (query or "").strip()
        if clean_query:
            doc = await self.client.fetch_search_timeline(
                self.auth,
                clean_query,
                count=count,
                cursor=cursor,
            )
            parsed = parse_timeline_graphql(doc, channel_tag="search", channel_id="search")
        elif channel == "bookmarks":
            doc = await self.client.fetch_bookmarks_timeline(self.auth, count=count, cursor=cursor)
            parsed = parse_timeline_graphql(doc, channel_tag="bookmarks", channel_id="bookmarks")
        elif channel.startswith("list:"):
            list_id = channel[5:].strip()
            if not list_id:
                raise XPluginError("invalid_argument", "list:<id> channel requires a list ID")
            doc = await self.client.fetch_list_latest_tweets_timeline(
                self.auth,
                list_id,
                count=count,
                cursor=cursor,
            )
            parsed = parse_timeline_graphql(doc, channel_tag="list", channel_id=channel)
        elif channel == "lists":
            pinned = self.subscriptions.list_pinned()
            if not pinned:
                raise XPluginError("invalid_argument", "尚未订阅任何 X List")
            doc = await self.client.fetch_list_latest_tweets_timeline(
                self.auth,
                pinned[0].id,
                count=count,
                cursor=cursor,
            )
            parsed = parse_timeline_graphql(doc, channel_tag="list", channel_id=f"list:{pinned[0].id}")
        elif channel in {"following", "home"}:
            doc = await self.client.fetch_home_timeline(self.auth, count=count, cursor=cursor)
            parsed = parse_timeline_graphql(doc, channel_tag="following", channel_id="following")
        else:
            doc = await self.client.fetch_home_latest_timeline(self.auth, count=count, cursor=cursor)
            parsed = parse_timeline_graphql(doc, channel_tag="for_you", channel_id="for_you")
        return parsed.items, parsed.bottom_cursor, doc

    async def _fetch_article_via_extract(self, target: ParsedXTarget) -> TextArticle:
        ctx = self.ctx
        if ctx is None or not ctx.capabilities.has("web.extract.v1"):
            raise XPluginError("not_configured", "未加载 web.extract.v1，无法回退 Chrome 渲染")
        res = await ctx.capabilities.call(
            "web.extract.v1",
            {
                "action": "extract",
                "url": target.web_url,
                "mode": "auto",
                "use_cookies": True,
                "render_js": True,
                "timeout_ms": int(self.auth.config.request_timeout_sec * 1000),
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            raise XPluginError(
                str(res.data.get("error_code") or "service_offline") if isinstance(res.data, Mapping) else "service_offline",
                res.error or "Chrome 抽取推文失败",
            )
        html = str(res.data.get("html") or res.data.get("text") or "")
        parsed = parse_html_fallback(html, target)
        if parsed.article is None:
            raise XPluginError("not_found", "未能从 Chrome 渲染结果提取推文正文")
        return parsed.article

    async def fetch_article(self, article_id: str) -> tuple[TextArticle, dict[str, Any]]:
        target = parse_target_id_or_url(article_id)
        if target.unsafe_scheme:
            raise XPluginError("invalid_argument", "只允许 https:// X 链接")
        if target.kind != "tweet" or not target.tweet_id:
            raise XPluginError("invalid_argument", "article_id/tweet_id 必须是推文 ID 或 https://x.com/.../status/... 链接")
        tweet_id = target.tweet_id
        errors: list[str] = []
        auth_res = await self.auth.resolve_for_url(target.web_url or "https://x.com/")

        if auth_res.is_authenticated:
            try:
                doc = await self.client.fetch_tweet_detail(self.auth, tweet_id)
                parsed = parse_tweet_detail_graphql(doc, tweet_id)
                if parsed.article is not None:
                    return parsed.article, {"transport": "graphql:TweetDetail", "tweet_id": tweet_id}
            except XPluginError as exc:
                errors.append(f"{exc.error_code}: {exc.message}")
            try:
                doc = await self.client.fetch_tweet_result_by_rest_id(self.auth, tweet_id)
                parsed = parse_single_tweet_result_graphql(doc, tweet_id)
                if parsed.article is not None:
                    return parsed.article, {"transport": "graphql:TweetResultByRestId", "tweet_id": tweet_id}
            except XPluginError as exc:
                errors.append(f"{exc.error_code}: {exc.message}")

        try:
            response = await self.client.fetch_tweet_syndication(tweet_id)
            if response.ok:
                parsed = parse_syndication_tweet_json(json.loads(response.body), tweet_id)
                if parsed.article is not None:
                    return parsed.article, {"transport": "syndication", "tweet_id": tweet_id}
            elif response.error:
                errors.append(f"{response.error_code or 'service_offline'}: {response.error}")
        except (XPluginError, json.JSONDecodeError) as exc:
            errors.append(str(exc))

        try:
            article = await self._fetch_article_via_extract(target)
            return article, {"transport": "web.extract.v1", "tweet_id": tweet_id}
        except XPluginError as exc:
            errors.append(f"{exc.error_code}: {exc.message}")

        raise XPluginError(
            "not_found",
            "无法获取推文正文；" + ("; ".join(errors[-3:]) or "上游返回空结果"),
            extra_secrets=self.auth.extra_secrets(),
        )

    async def handle_text_source_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        try:
            self.apply_config()
            raw_action = req_obj.get("action")
            action = str(raw_action).strip() if isinstance(raw_action, str) and raw_action.strip() else "channels"

            if action in {"channels", "status", "echo"}:
                status = await self.auth.safe_status_async()
                return CapabilityResult.ok(
                    {
                        "channels": [channel.model_dump() for channel in self.text_source.channels()],
                        "auth": status,
                        "lists": [item.to_dict() for item in self.subscriptions.list_pinned()],
                    }
                )

            if action in {"fetch_feed", "feed", "search"}:
                query = str(req_obj.get("query") or "").strip()
                channel_id = str(req_obj.get("channel_id") or "").strip()
                if action == "search" and not query:
                    return self._fail("invalid_argument", "search requires query")
                count = max(1, min(_optional_int(req_obj, "count", 20, minimum=1), 100))
                cursor = str(req_obj.get("cursor") or "").strip()
                items, next_cursor, _doc = await self.fetch_timeline(
                    channel_id=channel_id,
                    query=query,
                    count=count,
                    cursor=cursor,
                )
                if _coerce_bool(req_obj.get("translate", self.auto_translate()), self.auto_translate()):
                    items = await self.translate_feed_items(items)
                return CapabilityResult.ok(
                    {
                        "items": [item.model_dump() for item in items],
                        "count": len(items),
                        "next_cursor": next_cursor,
                        "has_more": bool(next_cursor),
                        "channel_id": channel_id or "for_you",
                    }
                )

            if action in {"fetch_article", "article", "fetch_thread"}:
                article_id = _str_chain(req_obj, ("article_id", "tweet_id", "id", "url"))
                if not article_id:
                    return self._fail("invalid_argument", "article_id, tweet_id, id, or url is required")
                article, meta = await self.fetch_article(article_id)
                if _coerce_bool(req_obj.get("translate", self.auto_translate()), self.auto_translate()):
                    article = await self.translate_article(article)
                return CapabilityResult.ok({"article": article.model_dump(), **meta})

            if action == "translate_article":
                article_id = _str_chain(req_obj, ("article_id", "tweet_id", "id", "url"))
                if not article_id:
                    return self._fail("invalid_argument", "article_id is required")
                article, _meta = await self.fetch_article(article_id)
                article = await self.translate_article(article, budget=None)
                return CapabilityResult.ok({"article": article.model_dump(), "translated": True})

            if action == "translate_text":
                raw_texts = req_obj.get("texts")
                if isinstance(raw_texts, str):
                    texts = [raw_texts]
                elif isinstance(raw_texts, list) and all(isinstance(item, str) for item in raw_texts):
                    texts = list(raw_texts)
                else:
                    text = str(req_obj.get("text") or "").strip()
                    if not text:
                        return self._fail("invalid_argument", "text or texts is required")
                    texts = [text]
                translated = await _translate_texts(
                    self.ctx,
                    texts,
                    target_lang=str(req_obj.get("target_lang") or self.target_lang()),
                )
                if translated is None:
                    return self._fail("not_configured", "未检测到可用的 llm.translate.v1 或 llm.chat.v1")
                return CapabilityResult.ok({"translations": translated, "target_lang": self.target_lang()})

            if action == "auth_status":
                return CapabilityResult.ok({"auth": await self.auth.safe_status_async()})

            if action in {"login", "open_login"}:
                result = await self.auth.open_login_window(str(req_obj.get("title") or ""))
                return result

            if action == "sync_cookies":
                ok, source, code, message = await self.auth.sync_from_chrome()
                if not ok:
                    return self._fail(
                        code or "auth_required",
                        message or "未能从 Chrome 同步 X 登录凭证",
                        data={"synced": False, "source": source, "auth": await self.auth.safe_status_async()},
                    )
                return CapabilityResult.ok(
                    {"synced": True, "source": source, "auth": await self.auth.safe_status_async()}
                )

            if action in {"set_cookie_header", "set_tokens"}:
                auth_token = str(req_obj.get("auth_token") or "").strip()
                ct0 = str(req_obj.get("ct0") or "").strip()
                header = _str_chain(req_obj, ("cookie_header", "cookie"))
                if header:
                    self.auth.save_vault_cookie_header(header)
                elif auth_token or ct0:
                    self.auth.save_vault_tokens(auth_token, ct0)
                else:
                    return self._fail("invalid_argument", "cookie_header or auth_token/ct0 is required")
                return CapabilityResult.ok({"auth": await self.auth.safe_status_async()})

            if action == "logout":
                if self.ctx is not None:
                    for key in ("cookie_header", "auth_token", "ct0"):
                        self.ctx.set_secret(key, None)
                self.auth = AuthManager()
                self.auth.set_context(self.ctx)
                return CapabilityResult.ok({"auth": self.auth.safe_status()})

            if action in {"list_subscriptions", "list_lists"}:
                return CapabilityResult.ok({"lists": [item.to_dict() for item in self.subscriptions.list_pinned()]})

            if action in {"add_list", "pin_list"}:
                list_id = _str_chain(req_obj, ("list_id", "id"))
                name = str(req_obj.get("name") or "").strip()
                if not list_id:
                    return self._fail("invalid_argument", "list_id is required")
                self.subscriptions.pin(list_id, name)
                return CapabilityResult.ok({"lists": [item.to_dict() for item in self.subscriptions.list_pinned()]})

            if action in {"remove_list", "unpin_list"}:
                list_id = _str_chain(req_obj, ("list_id", "id"))
                if not list_id:
                    return self._fail("invalid_argument", "list_id is required")
                if not self.subscriptions.remove(list_id):
                    return self._fail("not_found", f"List {list_id} is not subscribed")
                return CapabilityResult.ok({"lists": [item.to_dict() for item in self.subscriptions.list_pinned()]})

            if action == "reload_config":
                self.apply_config()
                return CapabilityResult.ok({"auth": await self.auth.safe_status_async()})

            return self._fail("invalid_argument", f"Unsupported text.source.v1 action: {action}")
        except XPluginError as exc:
            return self._fail(exc.error_code, exc.message)
        except Exception as exc:
            return self._fail("service_offline", f"X 插件内部错误: {type(exc).__name__}")

    async def handle_comments_rpc(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        req_obj = dict(req) if isinstance(req, Mapping) else {}
        try:
            self.apply_config()
            action = str(req_obj.get("action") or "fetch_comments")
            if action not in _COMMENT_ACTIONS:
                return self._fail("invalid_argument", f"Unsupported text.comments.v1 action: {action}")
            article_id = _str_chain(req_obj, ("article_id", "tweet_id", "id", "url"))
            if not article_id:
                return self._fail("invalid_argument", "article_id or tweet_id is required")
            article, meta = await self.fetch_article(article_id)
            comments = article.comments
            if _coerce_bool(req_obj.get("translate", self.auto_translate()), self.auto_translate()):
                article = await self.translate_article(article)
                comments = article.comments
            return CapabilityResult.ok(
                {
                    "comments": [comment.model_dump() for comment in comments],
                    "count": len(comments),
                    "article_id": article.id,
                    **meta,
                }
            )
        except XPluginError as exc:
            return self._fail(exc.error_code, exc.message)

    def _build_ui_schema(self) -> dict[str, Any]:
        return {
            "type": "Section",
            "id": "x-workspace-root",
            "title": "X / Twitter",
            "description": "关注时间线、列表、书签与串推阅读",
            "children": [
                {
                    "type": "Form",
                    "id": "x-search-form",
                    "fields": [
                        {"name": "query", "label": "搜索 X", "type": "string", "default": ""},
                        {"name": "channel_id", "label": "栏目", "type": "string", "default": "for_you"},
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "x-actions",
                    "actions": [
                        {"id": "fetch_feed", "label": "刷新", "variant": "primary"},
                        {"id": "login", "label": "Chrome 登录", "variant": "secondary"},
                        {"id": "sync_cookies", "label": "同步会话", "variant": "secondary"},
                        {"id": "list_lists", "label": "列表", "variant": "secondary"},
                    ],
                },
            ],
        }

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="x",
            title="X",
            domain="text",
            badge="X",
            ui_schema=self._build_ui_schema(),
            render=self._build_ui_schema,
            on_action=self.handle_tab_action,
        )

    async def handle_tab_action(
        self,
        action_id: str,
        state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {**(dict(state) if state is not None else {}), "action": action_id}
        result = await self.handle_text_source_rpc(payload)
        return {
            "ok": result.ok,
            "result": result.data,
            "error": result.error,
            "ui_schema": self._build_ui_schema(),
        }


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning an `XPlugin` instance."""
    return XPlugin()
