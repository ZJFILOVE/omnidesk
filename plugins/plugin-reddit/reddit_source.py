"""Domain 2 TextSource, pagination, deep comment tree expansion, and subscription sync for Reddit."""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from omnidesk_sdk import CommentNode, PluginContext, TextArticle, TextChannel, TextFeedItem, TextSource

if __package__:
    from .reddit_auth import AuthManager
    from .reddit_http import RedditHttpClient
    from .reddit_parser import (
        ArticleParseResult,
        FeedListingResult,
        MoreChildrenStub,
        attach_more_children,
        parse_listing_feed,
        parse_listing_html,
        parse_post_and_comments,
        parse_post_html,
        parse_subscribed_subreddits,
        resolve_article_target,
    )
    from .reddit_security import RedditPluginError
    from .reddit_subscriptions import SubscriptionStore, normalize_sort, normalize_subreddit_name
    from .reddit_translate import translate_article_if_enabled, translate_comments_if_enabled
else:
    from reddit_auth import AuthManager
    from reddit_http import RedditHttpClient
    from reddit_parser import (
        ArticleParseResult,
        FeedListingResult,
        MoreChildrenStub,
        attach_more_children,
        parse_listing_feed,
        parse_listing_html,
        parse_post_and_comments,
        parse_post_html,
        parse_subscribed_subreddits,
        resolve_article_target,
    )
    from reddit_security import RedditPluginError
    from reddit_subscriptions import SubscriptionStore, normalize_sort, normalize_subreddit_name
    from reddit_translate import translate_article_if_enabled, translate_comments_if_enabled

_MAX_FEED_CACHE = 500
_MAX_CURSOR_CACHE = 200


@dataclass(slots=True)
class ParsedChannelSpec:
    """Normalized Reddit channel/feed target."""

    kind: str = "default_multi"  # default_multi | home | popular | all | subreddit | search
    subreddit: str = ""
    sort: str = "hot"
    time_range: str = ""


@dataclass(slots=True)
class CommentFetchResult:
    """Result from fetching a post's recursive comment tree and deferred `more` stubs."""

    comments: list[CommentNode] = field(default_factory=list)
    more_stubs: list[MoreChildrenStub] = field(default_factory=list)
    link_fullname: str = ""
    post_author: str = ""
    transport: str = ""


def parse_channel_id(channel_id: str) -> ParsedChannelSpec:
    """Parse channel ID (`home`, `popular`, `r/cpp`, `r/cpp/top`, `search`, etc.)."""
    raw = str(channel_id or "").strip()
    if not raw or raw.lower() == "default":
        return ParsedChannelSpec(kind="default_multi", sort="hot")
    if ".." in raw or "\x00" in raw or any(ch.isspace() for ch in raw):
        raise RedditPluginError("invalid_argument", f"无效的频道标识: {channel_id!r}")

    cleaned = raw.strip("/")
    low = cleaned.lower()
    if low in {"home", "frontpage", "mine"}:
        return ParsedChannelSpec(kind="home", sort="hot")
    if low.startswith(("home/", "home:")):
        return ParsedChannelSpec(kind="home", sort=normalize_sort(cleaned[5:]))
    if low in {"popular", "r/popular"}:
        return ParsedChannelSpec(kind="popular", subreddit="popular", sort="hot")
    if low.startswith(("popular/", "r/popular/", "popular:", "r/popular:")):
        tail = cleaned.split("/", 1)[-1] if "/" in cleaned else cleaned.split(":", 1)[-1]
        if tail.lower().startswith("popular"):
            tail = tail[7:].lstrip("/:")
        return ParsedChannelSpec(kind="popular", subreddit="popular", sort=normalize_sort(tail))
    if low in {"all", "r/all"}:
        return ParsedChannelSpec(kind="all", subreddit="all", sort="hot")
    if low.startswith(("all/", "r/all/", "all:", "r/all:")):
        tail = cleaned.split("/", 1)[-1] if "/" in cleaned else cleaned.split(":", 1)[-1]
        if tail.lower().startswith("all"):
            tail = tail[3:].lstrip("/:")
        return ParsedChannelSpec(kind="all", subreddit="all", sort=normalize_sort(tail))
    if low == "search":
        return ParsedChannelSpec(kind="search")

    body = cleaned
    if low.startswith("r/"):
        body = body[2:]
    elif low.startswith("sub:"):
        body = body[4:]
    elif low.startswith("subreddit:"):
        body = body[10:]

    sort_part = "hot"
    for sep in ("/", ":"):
        if sep in body:
            body, sort_part = body.split(sep, 1)
            break

    norm_sub = normalize_subreddit_name(body)
    if not norm_sub:
        raise RedditPluginError("invalid_argument", f"无效的 Subreddit 名称: {channel_id!r}")

    sort = normalize_sort(sort_part)
    time_range = "week" if sort == "top" else ""
    return ParsedChannelSpec(kind="subreddit", subreddit=norm_sub, sort=sort, time_range=time_range)


def build_listing_endpoint(
    spec: ParsedChannelSpec,
    query: str,
    after: str,
    limit: int,
    subs: SubscriptionStore,
) -> str:
    """Build Reddit `.json` listing or search endpoint path+query."""
    clamped_limit = max(1, min(100, int(limit)))
    trimmed_q = str(query or "").strip()

    if trimmed_q or spec.kind == "search":
        q_enc = urllib.parse.quote(trimmed_q, safe="")
        if spec.kind == "subreddit" and spec.subreddit:
            ep = f"/r/{spec.subreddit}/search.json?q={q_enc}&restrict_sr=on&sort=relevance&limit={clamped_limit}"
        else:
            ep = f"/search.json?q={q_enc}&sort=relevance&limit={clamped_limit}"
        if after:
            ep += f"&after={urllib.parse.quote(after, safe='')}"
        return ep + "&raw_json=1"

    if spec.kind == "home":
        ep = f"/{spec.sort}.json?limit={clamped_limit}"
    elif spec.kind == "popular":
        ep = f"/r/popular/{spec.sort}.json?limit={clamped_limit}"
    elif spec.kind == "all":
        ep = f"/r/all/{spec.sort}.json?limit={clamped_limit}"
    elif spec.kind == "subreddit":
        ep = f"/r/{spec.subreddit}/{spec.sort}.json?limit={clamped_limit}"
        if spec.time_range:
            ep += f"&t={spec.time_range}"
    else:
        sub_list = subs.list_subscriptions()
        if not sub_list:
            ep = f"/r/popular/hot.json?limit={clamped_limit}"
        else:
            joined = "+".join(s.name for s in sub_list[:12])
            ep = f"/r/{joined}/hot.json?limit={clamped_limit}"

    if after:
        ep += f"&after={urllib.parse.quote(after, safe='')}"
    return ep + "&raw_json=1"


class RedditTextSource(TextSource):
    """Domain 2 TextSource exposing Reddit channels, paginated feeds, articles, and comment trees."""

    source_id: str = "plugin-reddit"
    display_name: str = "Reddit"
    category: str = "reddit"

    def __init__(
        self,
        auth: AuthManager,
        http: RedditHttpClient,
        subscriptions: SubscriptionStore,
        *,
        on_reload_config: Callable[[], None] | None = None,
    ) -> None:
        self.auth = auth
        self.http = http
        self.subscriptions = subscriptions
        self._on_reload_config = on_reload_config
        self._ctx: PluginContext | None = None
        self._feed_cache: dict[str, TextFeedItem] = {}
        self._page_cursors: dict[tuple[str, int], str] = {}
        self.last_error: dict[str, Any] | None = None

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    def clear_caches(self) -> None:
        self._feed_cache.clear()
        self._page_cursors.clear()
        self.last_error = None

    def _reload_config(self) -> None:
        if self._on_reload_config is not None:
            self._on_reload_config()

    def _cache_feed_items(self, items: list[TextFeedItem]) -> None:
        for item in items:
            if not item.id:
                continue
            if len(self._feed_cache) >= _MAX_FEED_CACHE:
                self._feed_cache.pop(next(iter(self._feed_cache)), None)
            self._feed_cache[item.id] = item

    def _remember_cursor(self, cursor_key: str, next_page: int, after: str) -> None:
        key = (cursor_key, next_page)
        if after:
            if len(self._page_cursors) >= _MAX_CURSOR_CACHE:
                self._page_cursors.pop(next(iter(self._page_cursors)), None)
            self._page_cursors[key] = after
        else:
            self._page_cursors.pop(key, None)

    def _clear_cursors_for(self, cursor_key: str) -> None:
        for k in [k for k in self._page_cursors if k[0] == cursor_key]:
            self._page_cursors.pop(k, None)

    def channels(self) -> list[TextChannel]:
        self._reload_config()
        res = self.auth.resolve_cached_sync("https://www.reddit.com/")
        return self.subscriptions.build_channels(res.is_authenticated())

    async def fetch_feed_listing(
        self,
        channel_id: str = "",
        query: str = "",
        after: str = "",
        limit: int = 25,
    ) -> tuple[FeedListingResult, str]:
        """Fetch and parse a listing page, failing closed with RedditPluginError on errors."""
        spec = parse_channel_id(channel_id)
        if spec.kind == "home":
            auth_res = await self.auth.resolve_for_url("https://www.reddit.com/")
            if not auth_res.is_authenticated():
                raise RedditPluginError(
                    "auth_required",
                    "Reddit Home 个人订阅流需要登录会话（reddit_session / token_v2）",
                    status_code=401,
                    details={"auth_required": True},
                )

        endpoint = build_listing_endpoint(spec, query, after, limit, self.subscriptions)
        resp = await self.http.fetch_with_fallback(endpoint, self.auth)
        if not resp.ok:
            raise RedditPluginError(
                resp.error_code or "service_offline",
                resp.error or f"Reddit 请求失败 (HTTP {resp.status_code})",
                status_code=resp.status_code,
                details={"auth_required": resp.error_code == "auth_required", "transport": resp.transport},
            )

        if resp.transport == "web.extract.v1:html":
            parsed = parse_listing_html(resp.body, fallback_subreddit=spec.subreddit)
        else:
            parsed = parse_listing_feed(resp.body, fallback_subreddit=spec.subreddit)
        self._cache_feed_items(parsed.items)
        return parsed, resp.transport

    async def fetch_feed(
        self,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[TextFeedItem]:
        """TextSource.fetch_feed implementation returning [] on empty results or fail-closed errors."""
        self.last_error = None
        self._reload_config()
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            return []
        if channel_id.strip().lower() == "search" and not query.strip():
            return []

        cursor_key = f"{channel_id.strip().lower()}\n{query.strip()}"
        if page == 1:
            if force_refresh:
                self._clear_cursors_for(cursor_key)
            after = ""
        else:
            after = self._page_cursors.get((cursor_key, page), "")
            if not after:
                return []

        try:
            parsed, _ = await self.fetch_feed_listing(channel_id, query=query, after=after, limit=25)
        except RedditPluginError as exc:
            self.last_error = {
                "error_code": exc.error_code,
                "error": str(exc),
                "status_code": exc.status_code,
                **exc.details,
            }
            return []

        self._remember_cursor(cursor_key, page + 1, parsed.after)
        return parsed.items

    def _build_article_endpoint(
        self,
        article_id: str,
        *,
        sort: str = "",
        limit: int = 200,
        depth: int = 10,
    ) -> tuple[str, str, str]:
        target = resolve_article_target(
            article_id,
            allow_loopback_http=self.http._allow_loopback_http,
            allowed_loopback_origins=self.http.allowed_loopback_origins(),
        )
        cached_item = self._feed_cache.get(article_id)
        if not target.subreddit and cached_item is not None:
            target.subreddit = normalize_subreddit_name(cached_item.source_name)

        eff_sort = (sort or target.comment_sort or "confidence").strip() or "confidence"
        clamped_limit = max(1, min(500, int(limit)))
        clamped_depth = max(1, min(20, int(depth)))
        prefix = f"/r/{target.subreddit}" if target.subreddit else ""
        ep = (
            f"{prefix}/comments/{target.post_id}.json"
            f"?limit={clamped_limit}&depth={clamped_depth}&sort={urllib.parse.quote(eff_sort, safe='')}&raw_json=1"
        )
        return ep, target.post_id, target.subreddit

    async def fetch_article_detail(
        self,
        article_id: str,
        *,
        sort: str = "",
        limit: int = 200,
        depth: int = 10,
        expand_more: bool = True,
    ) -> tuple[ArticleParseResult, str]:
        """Fetch and parse full Reddit post and recursive comment tree (fail-closed on errors)."""
        ep, post_id, _ = self._build_article_endpoint(article_id, sort=sort, limit=limit, depth=depth)
        resp = await self.http.fetch_with_fallback(ep, self.auth)
        if not resp.ok:
            raise RedditPluginError(
                resp.error_code or "service_offline",
                resp.error or f"Reddit 请求失败 (HTTP {resp.status_code})",
                status_code=resp.status_code,
                details={"auth_required": resp.error_code == "auth_required", "transport": resp.transport},
            )

        if resp.transport == "web.extract.v1:html":
            parsed = parse_post_html(resp.body, fallback_post_id=post_id)
        else:
            parsed = parse_post_and_comments(resp.body, fallback_post_id=post_id)

        if expand_more and parsed.more_stubs and parsed.link_fullname:
            batch_ids: list[str] = []
            for stub in parsed.more_stubs:
                for cid in stub.children:
                    if len(batch_ids) >= 20:
                        break
                    batch_ids.append(cid)
                if len(batch_ids) >= 20:
                    break
            if batch_ids:
                post_author = parsed.article.authors.removeprefix("u/")
                try:
                    await self.expand_more_children(
                        parsed.article.comments,
                        parsed.link_fullname,
                        post_author,
                        batch_ids,
                    )
                except RedditPluginError:
                    pass
        return parsed, resp.transport

    async def fetch_article(self, article_id: str) -> TextArticle:
        """TextSource.fetch_article implementation (raises RedditPluginError fail-closed on errors)."""
        self.last_error = None
        self._reload_config()
        try:
            parsed, _ = await self.fetch_article_detail(article_id)
        except RedditPluginError as exc:
            self.last_error = {
                "error_code": exc.error_code,
                "error": str(exc),
                "status_code": exc.status_code,
                **exc.details,
            }
            raise

        cfg = self.auth.config()
        article = parsed.article
        if cfg.auto_translate:
            article, _ = await translate_article_if_enabled(
                self._ctx, article, auto_translate=True, target_lang=cfg.target_lang
            )
            if article.comments:
                tr_comments, _, _ = await translate_comments_if_enabled(
                    self._ctx, article.comments, auto_translate=True, title=article.title, target_lang=cfg.target_lang
                )
                article = article.model_copy(update={"comments": tr_comments})
        return article

    async def fetch_comments(
        self,
        article_id: str,
        *,
        sort: str = "confidence",
        limit: int = 200,
        depth: int = 10,
        expand_more: bool = True,
    ) -> CommentFetchResult:
        """Fetch recursive comment tree and `more` stubs for `text.comments.v1`."""
        parsed, transport = await self.fetch_article_detail(
            article_id, sort=sort, limit=limit, depth=depth, expand_more=False
        )
        target = resolve_article_target(
            article_id,
            allow_loopback_http=self.http._allow_loopback_http,
            allowed_loopback_origins=self.http.allowed_loopback_origins(),
        )
        link_fullname = parsed.link_fullname or f"t3_{target.post_id}"
        post_author = parsed.article.authors.removeprefix("u/")
        comments = list(parsed.article.comments)
        more_stubs = list(parsed.more_stubs)

        if expand_more and more_stubs and link_fullname:
            batch_ids: list[str] = []
            for stub in more_stubs:
                for cid in stub.children:
                    if len(batch_ids) >= 25:
                        break
                    batch_ids.append(cid)
                if len(batch_ids) >= 25:
                    break
            if batch_ids:
                try:
                    await self.expand_more_children(comments, link_fullname, post_author, batch_ids)
                except RedditPluginError:
                    pass

        return CommentFetchResult(
            comments=comments,
            more_stubs=more_stubs,
            link_fullname=link_fullname,
            post_author=post_author,
            transport=transport,
        )

    async def expand_more_children(
        self,
        root_comments: list[CommentNode],
        link_fullname: str,
        post_author: str,
        children_ids: list[str],
    ) -> int:
        """Call `/api/morechildren.json` and stitch nested replies into `root_comments`."""
        clean_ids = [str(c).strip() for c in children_ids if str(c).strip()][:50]
        if not clean_ids or not link_fullname.strip():
            raise RedditPluginError("invalid_argument", "link_id and children are required")
        full_link = link_fullname.strip()
        if not full_link.startswith("t3_"):
            full_link = f"t3_{full_link}"

        ep = (
            "/api/morechildren.json?api_type=json"
            f"&link_id={urllib.parse.quote(full_link, safe='')}"
            f"&children={urllib.parse.quote(','.join(clean_ids), safe='')}&raw_json=1"
        )
        resp = await self.http.fetch_with_fallback(ep, self.auth, allow_html_extract=False)
        if not resp.ok:
            raise RedditPluginError(
                resp.error_code or "service_offline",
                resp.error or f"Reddit morechildren failed (HTTP {resp.status_code})",
                status_code=resp.status_code,
            )
        return attach_more_children(root_comments, full_link, post_author, resp.body)

    async def sync_remote_subscriptions(self) -> int:
        """Fetch `/subreddits/mine/subscriber.json` for authenticated user and merge locally."""
        auth_res = await self.auth.resolve_for_url("https://www.reddit.com/")
        if not auth_res.is_authenticated():
            raise RedditPluginError(
                "auth_required",
                "需要登录 Reddit 会话后才能同步个人订阅的 Subreddit 列表",
                status_code=401,
            )
        resp = await self.http.fetch_with_fallback(
            "/subreddits/mine/subscriber.json?limit=100&raw_json=1",
            self.auth,
            allow_html_extract=False,
        )
        if not resp.ok:
            raise RedditPluginError(
                resp.error_code or "service_offline",
                resp.error or f"同步订阅失败 (HTTP {resp.status_code})",
                status_code=resp.status_code,
            )
        remote_subs = parse_subscribed_subreddits(resp.body)
        return self.subscriptions.merge_remote_subscriptions(remote_subs)
