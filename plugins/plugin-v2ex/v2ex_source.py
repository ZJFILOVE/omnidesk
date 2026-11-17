"""V2EX TextSource, node subscription store, and fail-closed paginated fetch flows."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnidesk_sdk import PluginContext, TextArticle, TextChannel, TextFeedItem, TextSource

if __package__:
    from .v2ex_auth import AuthManager
    from .v2ex_http import HttpClient, classify_http_error
    from .v2ex_translate import translate_article_if_enabled, translate_comments_if_enabled
    from .v2ex_parser import (
        SubscribedNode,
        build_reply_tree,
        count_comment_nodes,
        parse_api_v1_replies_json,
        parse_api_v1_topic_json,
        parse_api_v2_node_topics_json,
        parse_api_v2_replies_json,
        parse_api_v2_topic_json,
        parse_html_topic_list,
        parse_html_topic_page,
        parse_my_nodes_html,
    )
    from .v2ex_security import (
        V2exError,
        atomic_write_json,
        parse_topic_id_or_url,
        redact_sensitive,
        sanitize_node_slug,
        validate_safe_file_path,
    )
else:
    from v2ex_auth import AuthManager
    from v2ex_http import HttpClient, classify_http_error
    from v2ex_translate import translate_article_if_enabled, translate_comments_if_enabled
    from v2ex_parser import (
        SubscribedNode,
        build_reply_tree,
        count_comment_nodes,
        parse_api_v1_replies_json,
        parse_api_v1_topic_json,
        parse_api_v2_node_topics_json,
        parse_api_v2_replies_json,
        parse_api_v2_topic_json,
        parse_html_topic_list,
        parse_html_topic_page,
        parse_my_nodes_html,
    )
    from v2ex_security import (
        V2exError,
        atomic_write_json,
        parse_topic_id_or_url,
        redact_sensitive,
        sanitize_node_slug,
        validate_safe_file_path,
    )

DEFAULT_MAX_REPLY_PAGES = 20
_MAX_ALLOWED_REPLY_PAGES = 50
_DEFAULT_SUBSCRIBED_NODES = "programmer,python,linux,macos"

_KNOWN_NODE_TITLES: dict[str, str] = {
    "programmer": "程序员",
    "python": "Python",
    "linux": "Linux",
    "macos": "macOS",
    "apple": "Apple",
    "jobs": "酷工作",
    "career": "职场话题",
    "create": "分享创造",
    "share": "分享发现",
    "qna": "问与答",
    "deals": "优惠信息",
    "go": "Go 编程语言",
    "rust": "Rust",
    "nodejs": "Node.js",
    "openai": "OpenAI",
    "claude": "Claude",
    "android": "Android",
    "iphone": "iPhone",
    "ipad": "iPad",
    "nas": "NAS",
    "homelab": "HomeLab",
    "宽带症候群": "宽带症候群",
    "bb": "宽带症候群",
}

_TAB_FALLBACK_NODES: dict[str, str] = {
    "tech": "programmer",
    "creative": "create",
    "play": "share",
    "apple": "apple",
    "jobs": "jobs",
    "qna": "qna",
    "deals": "deals",
}

_KNOWN_TABS: frozenset[str] = frozenset(
    {"hot", "all", "tech", "creative", "play", "apple", "jobs", "qna", "deals", "city", "r2", "nodes", "members"}
)


def default_node_title(slug: str) -> str:
    """Return human-readable title for a known V2EX node slug."""
    low = (slug or "").strip().lower()
    return _KNOWN_NODE_TITLES.get(low, slug.strip())


def map_tab_to_fallback_node(tab_id: str) -> str:
    """Map a V2EX homepage tab to its representative node slug for API/pagination fallbacks."""
    return _TAB_FALLBACK_NODES.get((tab_id or "").strip().lower(), "programmer")


def _parse_comma_nodes(csv_str: str) -> list[SubscribedNode]:
    out: list[SubscribedNode] = []
    seen: set[str] = set()
    for part in (csv_str or "").split(","):
        slug = sanitize_node_slug(part)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append(SubscribedNode(slug=slug, title=default_node_title(slug)))
    return out


def _filter_feed_items(items: list[TextFeedItem], query: str) -> list[TextFeedItem]:
    q = (query or "").strip().lower()
    if not q:
        return items
    return [
        item
        for item in items
        if q in item.title.lower()
        or q in item.excerpt.lower()
        or q in item.author.lower()
        or any(q in tag.lower() for tag in item.tags)
    ]


@dataclass(slots=True)
class FeedFetchResult:
    """Detailed feed fetch outcome carrying source mode and fail-closed error fields."""

    ok: bool = False
    channel_id: str = "hot"
    items: list[TextFeedItem] = field(default_factory=list)
    page: int = 1
    source_mode: str = "guest"
    error: str = ""
    error_code: str = ""
    http_status: int | None = None


@dataclass(slots=True)
class ArticleFetchResult:
    """Detailed topic fetch outcome matching v1 ArticleFetchResult."""

    article: TextArticle | None = None
    total_replies: int = 0
    pages_fetched: int = 0
    total_pages: int = 1
    source_mode: str = "guest"
    ok: bool = False
    error: str = ""
    error_code: str = ""
    http_status: int | None = None


class SubscriptionStore:
    """Thread-safe node subscription store persisted to `subscriptions.json`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._file_path: Path | None = None
        self._loaded = False
        self._last_config_spec = ""
        self._nodes: list[SubscribedNode] = _parse_comma_nodes(_DEFAULT_SUBSCRIBED_NODES)

    def bind(self, data_dir: Path | str | None, config_nodes_csv: str) -> None:
        """Bind `subscriptions.json` inside plugin `data_dir` and sync initial/updated config."""
        with self._lock:
            if data_dir:
                root = Path(str(data_dir)).expanduser()
                next_path = validate_safe_file_path(str(root / "subscriptions.json"), must_exist=False)
                if next_path != self._file_path:
                    self._file_path = next_path
                    self._loaded = False
            else:
                self._file_path = None

            if not self._loaded:
                self._load_locked(config_nodes_csv)
            else:
                self._apply_config_update_locked(config_nodes_csv)

    def _load_locked(self, config_nodes_csv: str) -> None:
        self._loaded = True
        if self._file_path is not None:
            if self._file_path.is_symlink():
                raise V2exError("path_safety", "拒绝读取符号链接 subscriptions.json")
            if self._file_path.is_file():
                try:
                    doc = json.loads(self._file_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    doc = None
                if isinstance(doc, Mapping) and isinstance(doc.get("nodes"), list):
                    loaded: list[SubscribedNode] = []
                    seen: set[str] = set()
                    for item in doc["nodes"]:
                        if isinstance(item, Mapping):
                            slug = sanitize_node_slug(str(item.get("slug") or ""))
                            if not slug or slug in seen:
                                continue
                            seen.add(slug)
                            title = str(item.get("title") or "").strip() or default_node_title(slug)
                            loaded.append(SubscribedNode(slug=slug, title=title))
                        elif isinstance(item, str):
                            slug = sanitize_node_slug(item)
                            if not slug or slug in seen:
                                continue
                            seen.add(slug)
                            loaded.append(SubscribedNode(slug=slug, title=default_node_title(slug)))
                    if loaded:
                        self._nodes = loaded
                        saved_spec = doc.get("last_config_spec")
                        self._last_config_spec = (
                            str(saved_spec).strip()
                            if isinstance(saved_spec, str)
                            else (config_nodes_csv or _DEFAULT_SUBSCRIBED_NODES).strip()
                        )
                        self._apply_config_update_locked(config_nodes_csv)
                        return

        spec = (config_nodes_csv or "").strip() or _DEFAULT_SUBSCRIBED_NODES
        seeded = _parse_comma_nodes(spec) or _parse_comma_nodes(_DEFAULT_SUBSCRIBED_NODES)
        self._nodes = seeded
        self._last_config_spec = spec
        self._save_locked()

    def _apply_config_update_locked(self, config_nodes_csv: str) -> None:
        spec = (config_nodes_csv or "").strip()
        if not spec or spec == self._last_config_spec:
            return
        self._last_config_spec = spec
        changed = False
        existing_slugs = {n.slug for n in self._nodes}
        for node in _parse_comma_nodes(spec):
            if node.slug not in existing_slugs:
                self._nodes.append(node)
                existing_slugs.add(node.slug)
                changed = True
        if changed:
            self._save_locked()

    def _save_locked(self) -> bool:
        if self._file_path is None:
            return False
        doc = {
            "schema_version": 1,
            "plugin_id": "plugin-v2ex",
            "last_config_spec": self._last_config_spec,
            "nodes": [{"slug": n.slug, "title": n.title} for n in self._nodes],
        }
        atomic_write_json(self._file_path, doc, file_mode=0o600)
        return True

    def list_nodes(self) -> list[SubscribedNode]:
        with self._lock:
            return list(self._nodes)

    def subscribe(self, raw_slug: str, title: str = "") -> list[SubscribedNode]:
        clean = sanitize_node_slug(raw_slug)
        if not clean:
            raise V2exError("invalid_argument", f"无效的 V2EX 节点 slug: {raw_slug!r}")
        clean_title = (title or "").strip()
        with self._lock:
            for idx, existing in enumerate(self._nodes):
                if existing.slug == clean:
                    if clean_title and clean_title != existing.title:
                        self._nodes[idx] = SubscribedNode(slug=clean, title=clean_title)
                        self._save_locked()
                    return list(self._nodes)
            label = clean_title or default_node_title(clean)
            self._nodes.append(SubscribedNode(slug=clean, title=label))
            self._save_locked()
            return list(self._nodes)

    def unsubscribe(self, raw_slug: str) -> list[SubscribedNode]:
        clean = sanitize_node_slug(raw_slug)
        if not clean:
            raise V2exError("invalid_argument", f"无效的 V2EX 节点 slug: {raw_slug!r}")
        with self._lock:
            before = len(self._nodes)
            self._nodes = [n for n in self._nodes if n.slug != clean]
            if len(self._nodes) == before:
                raise V2exError("not_found", f"节点 {clean!r} 不在关注列表中")
            self._save_locked()
            return list(self._nodes)

    def merge_discovered(self, discovered: list[SubscribedNode]) -> tuple[int, list[SubscribedNode]]:
        with self._lock:
            added = 0
            changed = False
            by_slug = {n.slug: i for i, n in enumerate(self._nodes)}
            for d in discovered:
                clean = sanitize_node_slug(d.slug)
                if not clean:
                    continue
                if clean not in by_slug:
                    self._nodes.append(SubscribedNode(slug=clean, title=d.title or default_node_title(clean)))
                    by_slug[clean] = len(self._nodes) - 1
                    added += 1
                    changed = True
                elif d.title and self._nodes[by_slug[clean]].title != d.title:
                    self._nodes[by_slug[clean]] = SubscribedNode(slug=clean, title=d.title)
                    changed = True
            if changed:
                self._save_locked()
            return added, list(self._nodes)


class V2exTextSource(TextSource):
    """Domain 2 TextSource providing V2EX tabs, nodes, following feeds, articles, and reply trees."""

    source_id: str = "plugin-v2ex"
    display_name: str = "V2EX"
    category: str = "v2ex"

    def __init__(self, auth: AuthManager, http: HttpClient) -> None:
        self.auth = auth
        self.http = http
        self._ctx: PluginContext | None = None
        self.subscriptions = SubscriptionStore()
        self.last_error: dict[str, Any] | None = None

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx
        self.reload_subscriptions()

    def reload_subscriptions(self) -> None:
        data_dir = self._ctx.data_dir if self._ctx is not None else None
        self.subscriptions.bind(data_dir, self.auth.config.subscribed_nodes)

    def channels(self) -> list[TextChannel]:
        """Return V2EX built-in tabs, following feed, and subscribed node channels."""
        if self._ctx is not None:
            self.auth.load_from_context(self._ctx)
            self.reload_subscriptions()
        try:
            snap = self.auth.resolve_sync_snapshot(f"{self.http.base_url}/")
        except V2exError:
            snap = None
        cookie_authed = bool(snap and snap.is_cookie_authenticated)
        has_pat = bool(snap and snap.has_pat)
        mode_badge = "PAT" if has_pat else ("A2" if cookie_authed else "")

        out: list[TextChannel] = [
            TextChannel(id="hot", name="最热", badge=mode_badge, requires_auth=False),
            TextChannel(id="all", name="全部", badge="", requires_auth=False),
            TextChannel(id="tech", name="技术", badge="", requires_auth=False),
            TextChannel(id="creative", name="创意", badge="", requires_auth=False),
            TextChannel(id="play", name="好玩", badge="", requires_auth=False),
            TextChannel(id="apple", name="Apple", badge="", requires_auth=False),
            TextChannel(id="jobs", name="酷工作", badge="", requires_auth=False),
            TextChannel(id="qna", name="问与答", badge="", requires_auth=False),
            TextChannel(
                id="following",
                name="关注",
                badge="" if cookie_authed else "需登录",
                requires_auth=True,
            ),
        ]
        for node in self.subscriptions.list_nodes():
            if not node.slug:
                continue
            out.append(
                TextChannel(
                    id=f"node:{node.slug}",
                    name=f"节点 · {node.title or node.slug}",
                    badge="API v2" if has_pat else "",
                    requires_auth=False,
                )
            )
        return out

    async def fetch_feed(
        self,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[TextFeedItem]:
        """Fetch paginated feed items; on error sets `last_error` and returns `[]`."""
        self.last_error = None
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            self.last_error = {"error_code": "invalid_argument", "error": "page 必须是 >= 1 的整数"}
            return []
        res = await self.fetch_feed_detailed(
            channel_id=channel_id,
            query=query,
            page=page,
            force_refresh=force_refresh,
        )
        if not res.ok:
            self.last_error = {
                "error_code": res.error_code or "service_offline",
                "error": res.error or "获取 V2EX 列表失败",
                **({"http_status": res.http_status} if res.http_status else {}),
            }
            return []
        return res.items

    async def fetch_feed_detailed(
        self,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> FeedFetchResult:
        """Fetch paginated V2EX feed with explicit fail-closed errors."""
        _ = force_refresh
        if self._ctx is not None:
            self.auth.load_from_context(self._ctx)
            self.http.set_default_timeout_sec(self.auth.config.request_timeout_sec)
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            return FeedFetchResult(ok=False, error_code="invalid_argument", error="page 必须是 >= 1 的整数")

        trimmed_q = (query or "").strip()
        if trimmed_q.startswith(("node:", "/go/")):
            q_slug = sanitize_node_slug(trimmed_q)
            if not q_slug:
                return FeedFetchResult(ok=False, error_code="invalid_argument", error=f"无效节点名称: {trimmed_q!r}")
            return await self._fetch_node_feed(q_slug, default_node_title(q_slug), "", page)

        raw_ch = (channel_id or "").strip()
        if not raw_ch:
            return await self._fetch_tab_feed("hot", query, page)

        low_ch = raw_ch.lower()
        if low_ch.startswith(("node:", "/go/")):
            slug = sanitize_node_slug(raw_ch)
            if not slug:
                return FeedFetchResult(ok=False, error_code="invalid_argument", error=f"无效节点名称: {raw_ch!r}")
            title = default_node_title(slug)
            for sub in self.subscriptions.list_nodes():
                if sub.slug == slug and sub.title:
                    title = sub.title
                    break
            return await self._fetch_node_feed(slug, title, query, page)

        if low_ch in {"following", "my_following"}:
            return await self._fetch_following_feed(query, page)

        if low_ch in _KNOWN_TABS:
            return await self._fetch_tab_feed(low_ch, query, page)

        slug = sanitize_node_slug(raw_ch)
        if not slug:
            return FeedFetchResult(ok=False, error_code="invalid_argument", error=f"无效频道或节点标识: {raw_ch!r}")
        return await self._fetch_node_feed(slug, default_node_title(slug), query, page)

    def _error_result_from_http(
        self,
        resp: Any,
        *,
        expect_json: bool = False,
        channel_id: str = "hot",
        page: int = 1,
    ) -> FeedFetchResult:
        err = classify_http_error(resp, expect_json=expect_json)
        msg = redact_sensitive(err.message, self.auth.secret_values())
        return FeedFetchResult(
            ok=False,
            channel_id=channel_id,
            page=page,
            error=msg,
            error_code=err.error_code,
            http_status=resp.status_code or None,
        )

    async def _fetch_node_feed(
        self,
        node_slug: str,
        node_title: str,
        query: str,
        page: int = 1,
    ) -> FeedFetchResult:
        slug = sanitize_node_slug(node_slug)
        if not slug:
            return FeedFetchResult(ok=False, error_code="invalid_argument", error=f"无效节点标识: {node_slug!r}")
        channel_id = f"node:{slug}"
        base = self.http.base_url
        node_web_url = f"{base}/go/{slug}"

        try:
            resolved = await self.auth.resolve_for_url(node_web_url)
        except V2exError as exc:
            return FeedFetchResult(
                ok=False,
                channel_id=channel_id,
                page=page,
                error=redact_sensitive(exc.message, self.auth.secret_values()),
                error_code=exc.error_code,
            )

        if resolved.has_pat:
            api_url = f"{base}/api/v2/nodes/{slug}/topics?p={page}"
            api_resp = await self.http.fetch_api_v2(api_url, self.auth)
            if api_resp.ok:
                try:
                    data = json.loads(api_resp.body)
                    items = parse_api_v2_node_topics_json(
                        data,
                        slug,
                        node_title,
                        channel_id=channel_id,
                        base_url=base,
                    )
                except (json.JSONDecodeError, V2exError) as exc:
                    msg = exc.message if isinstance(exc, V2exError) else "V2EX API v2 主题列表 JSON 解析失败"
                    return FeedFetchResult(ok=False, channel_id=channel_id, page=page, error=msg, error_code="parse_error")
                return FeedFetchResult(
                    ok=True,
                    channel_id=channel_id,
                    items=_filter_feed_items(items, query),
                    page=page,
                    source_mode="api_v2_pat",
                )
            if not resolved.is_cookie_authenticated or api_resp.error_code in {
                "not_found",
                "timeout",
                "parse_error",
                "rate_limited",
                "service_offline",
            }:
                return self._error_result_from_http(api_resp, expect_json=True, channel_id=channel_id, page=page)

        web_url = f"{node_web_url}?p={page}" if page > 1 else node_web_url
        web_resp = await self.http.fetch_with_fallback(
            web_url,
            self.auth,
            prefer_chrome_fetch=self.auth.config.prefer_chrome_fetch,
            expect_json=False,
        )
        if not web_resp.ok:
            return self._error_result_from_http(web_resp, expect_json=False, channel_id=channel_id, page=page)

        try:
            items = parse_html_topic_list(web_resp.body, channel_id, node_title, base_url=base)
        except V2exError as exc:
            return FeedFetchResult(ok=False, channel_id=channel_id, page=page, error=exc.message, error_code=exc.error_code)

        return FeedFetchResult(
            ok=True,
            channel_id=channel_id,
            items=_filter_feed_items(items, query),
            page=page,
            source_mode="html_cookie" if resolved.is_cookie_authenticated else "html_guest",
        )

    async def _fetch_tab_feed(self, tab_id: str, query: str, page: int = 1) -> FeedFetchResult:
        tab = (tab_id or "hot").strip().lower()
        base = self.http.base_url
        try:
            resolved = await self.auth.resolve_for_url(f"{base}/?tab={tab}")
        except V2exError as exc:
            return FeedFetchResult(
                ok=False,
                channel_id=tab,
                page=page,
                error=redact_sensitive(exc.message, self.auth.secret_values()),
                error_code=exc.error_code,
            )

        if page > 1:
            if tab in {"hot", "all"}:
                recent_url = f"{base}/recent?p={page}"
                recent_resp = await self.http.fetch_with_fallback(
                    recent_url,
                    self.auth,
                    prefer_chrome_fetch=self.auth.config.prefer_chrome_fetch,
                    expect_json=False,
                )
                if recent_resp.ok:
                    try:
                        items = parse_html_topic_list(recent_resp.body, tab, base_url=base)
                    except V2exError as exc:
                        return FeedFetchResult(
                            ok=False, channel_id=tab, page=page, error=exc.message, error_code=exc.error_code
                        )
                    return FeedFetchResult(
                        ok=True,
                        channel_id=tab,
                        items=_filter_feed_items(items, query),
                        page=page,
                        source_mode="html_cookie" if resolved.is_cookie_authenticated else "html_guest",
                    )
                if recent_resp.error_code == "auth_required" and resolved.has_pat:
                    fb_node = map_tab_to_fallback_node(tab)
                    return await self._fetch_node_feed(fb_node, default_node_title(fb_node), query, page)
                return self._error_result_from_http(recent_resp, expect_json=False, channel_id=tab, page=page)

            fb_node = map_tab_to_fallback_node(tab)
            return await self._fetch_node_feed(fb_node, default_node_title(fb_node), query, page)

        tab_url = f"{base}/?tab={tab}"
        web_resp = await self.http.fetch_with_fallback(
            tab_url,
            self.auth,
            prefer_chrome_fetch=self.auth.config.prefer_chrome_fetch,
            expect_json=False,
        )
        if web_resp.ok:
            try:
                items = parse_html_topic_list(web_resp.body, tab, base_url=base)
            except V2exError as exc:
                return FeedFetchResult(ok=False, channel_id=tab, page=page, error=exc.message, error_code=exc.error_code)
            return FeedFetchResult(
                ok=True,
                channel_id=tab,
                items=_filter_feed_items(items, query),
                page=page,
                source_mode="html_cookie" if resolved.is_cookie_authenticated else "html_guest",
            )

        primary_err = self._error_result_from_http(web_resp, expect_json=False, channel_id=tab, page=page)
        can_try_fallback = primary_err.error_code in {"cloudflare_blocked", "permission_denied"}
        if can_try_fallback and resolved.has_pat:
            fb_node = map_tab_to_fallback_node(tab)
            api_url = f"{base}/api/v2/nodes/{fb_node}/topics?p=1"
            api_resp = await self.http.fetch_api_v2(api_url, self.auth)
            if api_resp.ok:
                try:
                    data = json.loads(api_resp.body)
                    items = parse_api_v2_node_topics_json(
                        data, fb_node, default_node_title(fb_node), channel_id=tab, base_url=base
                    )
                    return FeedFetchResult(
                        ok=True,
                        channel_id=tab,
                        items=_filter_feed_items(items, query),
                        page=page,
                        source_mode="api_v2_pat",
                    )
                except (json.JSONDecodeError, V2exError):
                    pass

        if can_try_fallback and tab in {"hot", "all"}:
            v1_url = f"{base}/api/topics/hot.json" if tab == "hot" else f"{base}/api/topics/latest.json"
            v1_resp = await self.http.fetch_with_fallback(
                v1_url,
                self.auth,
                prefer_chrome_fetch=self.auth.config.prefer_chrome_fetch,
                expect_json=True,
            )
            if v1_resp.ok:
                try:
                    data = json.loads(v1_resp.body)
                    items = parse_api_v1_topic_list_json(data, tab, base_url=base)
                    return FeedFetchResult(
                        ok=True,
                        channel_id=tab,
                        items=_filter_feed_items(items, query),
                        page=page,
                        source_mode="api_v1_fallback",
                    )
                except (json.JSONDecodeError, V2exError):
                    pass

        return primary_err

    async def _fetch_following_feed(self, query: str, page: int = 1) -> FeedFetchResult:
        base = self.http.base_url
        url = f"{base}/my/following?p={page}" if page > 1 else f"{base}/my/following"
        try:
            resolved = await self.auth.resolve_for_url(url)
        except V2exError as exc:
            return FeedFetchResult(
                ok=False,
                channel_id="following",
                page=page,
                error=redact_sensitive(exc.message, self.auth.secret_values()),
                error_code=exc.error_code,
            )

        if not resolved.is_cookie_authenticated:
            return FeedFetchResult(
                ok=False,
                channel_id="following",
                page=page,
                error="查看 V2EX 关注流需要 A2 登录 Cookie，请先通过 web.embed.v1 登录或同步 Cookie",
                error_code="auth_required",
                http_status=401,
            )

        resp = await self.http.fetch_with_fallback(
            url,
            self.auth,
            prefer_chrome_fetch=self.auth.config.prefer_chrome_fetch,
            expect_json=False,
        )
        if not resp.ok:
            return self._error_result_from_http(resp, expect_json=False, channel_id="following", page=page)

        try:
            items = parse_html_topic_list(resp.body, "following", base_url=base)
        except V2exError as exc:
            return FeedFetchResult(
                ok=False, channel_id="following", page=page, error=exc.message, error_code=exc.error_code
            )

        return FeedFetchResult(
            ok=True,
            channel_id="following",
            items=_filter_feed_items(items, query),
            page=page,
            source_mode="html_cookie",
        )

    async def sync_followed_nodes_from_web(self) -> tuple[int, list[SubscribedNode]]:
        """Sync followed V2EX nodes from `/my/nodes` (requires A2 Cookie)."""
        url = f"{self.http.base_url}/my/nodes"
        resolved = await self.auth.resolve_for_url(url)
        if not resolved.is_cookie_authenticated:
            raise V2exError("auth_required", "同步 V2EX 收藏节点需要 A2 登录 Cookie")
        resp = await self.http.fetch_with_fallback(
            url,
            self.auth,
            prefer_chrome_fetch=self.auth.config.prefer_chrome_fetch,
            expect_json=False,
        )
        if not resp.ok:
            raise classify_http_error(resp, expect_json=False)
        discovered = parse_my_nodes_html(resp.body)
        if not discovered:
            return 0, self.subscriptions.list_nodes()
        return self.subscriptions.merge_discovered(discovered)

    async def fetch_article(self, article_id: str) -> TextArticle:
        """Fetch full structured TextArticle; raises V2exError fail-closed on failure."""
        res = await self.fetch_article_detailed(article_id, max_reply_pages=DEFAULT_MAX_REPLY_PAGES)
        if not res.ok or res.article is None:
            raise V2exError(
                res.error_code or "service_offline",
                res.error or f"Failed to fetch V2EX topic {article_id}",
                details={"http_status": res.http_status} if res.http_status else {},
            )
        article = res.article
        if self.auth.config.auto_translate:
            target_lang = self.auth.config.target_lang
            article, _ = await translate_article_if_enabled(
                self._ctx,
                article,
                auto_translate=True,
                target_lang=target_lang,
            )
            if article.comments:
                translated_comments, _ = await translate_comments_if_enabled(
                    self._ctx,
                    list(article.comments),
                    auto_translate=True,
                    title=article.title,
                    target_lang=target_lang,
                )
                article = article.model_copy(update={"comments": translated_comments})
        return article

    async def fetch_article_detailed(
        self,
        article_id: str,
        *,
        max_reply_pages: int = DEFAULT_MAX_REPLY_PAGES,
        reply_page: int | None = None,
    ) -> ArticleFetchResult:
        """Fetch topic content and nested reply tree via API v2 PAT, HTML, or API v1 fallback."""
        if self._ctx is not None:
            self.auth.load_from_context(self._ctx)
            self.http.set_default_timeout_sec(self.auth.config.request_timeout_sec)
        if isinstance(max_reply_pages, bool) or not isinstance(max_reply_pages, int) or max_reply_pages < 1:
            return ArticleFetchResult(ok=False, error_code="invalid_argument", error="max_reply_pages 必须是 >= 1 的整数")
        if reply_page is not None and (isinstance(reply_page, bool) or not isinstance(reply_page, int) or reply_page < 1):
            return ArticleFetchResult(ok=False, error_code="invalid_argument", error="page 必须是 >= 1 的整数")

        clamped_max_pages = max(1, min(max_reply_pages, _MAX_ALLOWED_REPLY_PAGES))
        base = self.http.base_url
        try:
            target = parse_topic_id_or_url(
                article_id,
                base_url=base,
                allow_loopback_http=self.http.allow_loopback_http,
                allowed_loopback_origins=self.http.allowed_loopback_origins(),
            )
            resolved = await self.auth.resolve_for_url(target.web_url)
        except V2exError as exc:
            return ArticleFetchResult(
                ok=False,
                error=redact_sensitive(exc.message, self.auth.secret_values()),
                error_code=exc.error_code,
            )

        # Path A: API v2 Personal Access Token
        if resolved.has_pat:
            topic_resp = await self.http.fetch_api_v2(target.api_v2_url, self.auth)
            if topic_resp.ok:
                try:
                    topic_json = json.loads(topic_resp.body)
                    parsed = parse_api_v2_topic_json(topic_json, base_url=base)
                except (json.JSONDecodeError, V2exError) as exc:
                    msg = exc.message if isinstance(exc, V2exError) else "V2EX API v2 主题 JSON 解析失败"
                    return ArticleFetchResult(ok=False, error=msg, error_code="parse_error")

                all_replies = []
                pages_done = 0
                if reply_page is not None:
                    if reply_page <= parsed.total_pages:
                        rep_resp = await self.http.fetch_api_v2(f"{target.api_v2_url}/replies?p={reply_page}", self.auth)
                        if not rep_resp.ok:
                            err = classify_http_error(rep_resp, expect_json=True)
                            return ArticleFetchResult(
                                ok=False,
                                error=redact_sensitive(err.message, self.auth.secret_values()),
                                error_code=err.error_code,
                                http_status=rep_resp.status_code or None,
                            )
                        try:
                            rep_json = json.loads(rep_resp.body)
                            all_replies = parse_api_v2_replies_json(
                                rep_json, parsed.article.authors, page_number=reply_page, page_size=20
                            )
                        except (json.JSONDecodeError, V2exError) as exc:
                            msg = exc.message if isinstance(exc, V2exError) else "V2EX API v2 回复 JSON 解析失败"
                            return ArticleFetchResult(ok=False, error=msg, error_code="parse_error")
                        pages_done = 1
                else:
                    pages_to_fetch = max(1, min(parsed.total_pages, clamped_max_pages))
                    for p in range(1, pages_to_fetch + 1):
                        rep_resp = await self.http.fetch_api_v2(f"{target.api_v2_url}/replies?p={p}", self.auth)
                        if not rep_resp.ok:
                            if p == 1 or rep_resp.error_code in {
                                "timeout",
                                "parse_error",
                                "auth_required",
                                "cloudflare_blocked",
                                "rate_limited",
                                "service_offline",
                            }:
                                err = classify_http_error(rep_resp, expect_json=True)
                                return ArticleFetchResult(
                                    ok=False,
                                    error=redact_sensitive(err.message, self.auth.secret_values()),
                                    error_code=err.error_code,
                                    http_status=rep_resp.status_code or None,
                                )
                            break
                        try:
                            rep_json = json.loads(rep_resp.body)
                            page_replies = parse_api_v2_replies_json(
                                rep_json, parsed.article.authors, page_number=p, page_size=20
                            )
                        except (json.JSONDecodeError, V2exError) as exc:
                            msg = exc.message if isinstance(exc, V2exError) else "V2EX API v2 回复 JSON 解析失败"
                            return ArticleFetchResult(ok=False, error=msg, error_code="parse_error")
                        pages_done += 1
                        all_replies.extend(page_replies)
                        if len(page_replies) < 20:
                            break

                comments = build_reply_tree(all_replies, parsed.article.authors)
                article = parsed.article.model_copy(update={"comments": comments})
                return ArticleFetchResult(
                    article=article,
                    total_replies=count_comment_nodes(comments),
                    pages_fetched=pages_done if reply_page is not None else max(1, pages_done),
                    total_pages=parsed.total_pages,
                    source_mode="api_v2_pat",
                    ok=True,
                )

            if not resolved.is_cookie_authenticated or topic_resp.error_code in {
                "not_found",
                "timeout",
                "parse_error",
                "rate_limited",
                "service_offline",
            }:
                err = classify_http_error(topic_resp, expect_json=True)
                return ArticleFetchResult(
                    ok=False,
                    error=redact_sensitive(err.message, self.auth.secret_values()),
                    error_code=err.error_code,
                    http_status=topic_resp.status_code or None,
                )

        # Path B: Web HTML + A2 Cookie or Guest
        first_p = reply_page if reply_page is not None else 1
        page1_resp = await self.http.fetch_with_fallback(
            f"{target.web_url}?p={first_p}",
            self.auth,
            prefer_chrome_fetch=self.auth.config.prefer_chrome_fetch,
            expect_json=False,
        )
        if page1_resp.ok:
            try:
                page1 = parse_html_topic_page(page1_resp.body, target.topic_id, base_url=base)
            except V2exError as exc:
                return ArticleFetchResult(ok=False, error=exc.message, error_code=exc.error_code)

            all_replies = []
            pages_done = 0
            if reply_page is not None:
                if reply_page <= page1.total_pages and page1.current_page >= reply_page:
                    all_replies = list(page1.raw_replies)
                    pages_done = 1
            else:
                all_replies = list(page1.raw_replies)
                pages_done = 1
                pages_to_fetch = max(1, min(page1.total_pages, clamped_max_pages))
                for p in range(2, pages_to_fetch + 1):
                    p_resp = await self.http.fetch_with_fallback(
                        f"{target.web_url}?p={p}",
                        self.auth,
                        prefer_chrome_fetch=self.auth.config.prefer_chrome_fetch,
                        expect_json=False,
                    )
                    if not p_resp.ok:
                        if p_resp.error_code in {
                            "timeout",
                            "auth_required",
                            "cloudflare_blocked",
                            "parse_error",
                            "rate_limited",
                            "service_offline",
                        }:
                            err = classify_http_error(p_resp, expect_json=False)
                            return ArticleFetchResult(
                                ok=False,
                                error=redact_sensitive(err.message, self.auth.secret_values()),
                                error_code=err.error_code,
                                http_status=p_resp.status_code or None,
                            )
                        break
                    try:
                        sub_page = parse_html_topic_page(p_resp.body, target.topic_id, base_url=base)
                    except V2exError as exc:
                        return ArticleFetchResult(ok=False, error=exc.message, error_code=exc.error_code)
                    if not sub_page.raw_replies:
                        break
                    pages_done += 1
                    all_replies.extend(sub_page.raw_replies)

            comments = build_reply_tree(all_replies, page1.article.authors)
            article = page1.article.model_copy(update={"comments": comments})
            return ArticleFetchResult(
                article=article,
                total_replies=count_comment_nodes(comments),
                pages_fetched=pages_done,
                total_pages=page1.total_pages,
                source_mode="html_cookie" if resolved.is_cookie_authenticated else "html_guest",
                ok=True,
            )

        primary_err = classify_http_error(page1_resp, expect_json=False)
        primary_res = ArticleFetchResult(
            ok=False,
            error=redact_sensitive(primary_err.message, self.auth.secret_values()),
            error_code=primary_err.error_code,
            http_status=page1_resp.status_code or None,
        )

        # Path C: V2EX API v1 public JSON fallback ONLY when HTML was blocked by Cloudflare/403
        if primary_res.error_code in {"cloudflare_blocked", "permission_denied"} and reply_page in {None, 1}:
            t_resp = await self.http.fetch_with_fallback(
                f"{base}/api/topics/show.json?id={target.topic_id}",
                self.auth,
                prefer_chrome_fetch=self.auth.config.prefer_chrome_fetch,
                expect_json=True,
            )
            if t_resp.ok:
                try:
                    tj = json.loads(t_resp.body)
                    parsed = parse_api_v1_topic_json(tj, base_url=base)
                    raw_replies = []
                    r_resp = await self.http.fetch_with_fallback(
                        f"{base}/api/replies/show.json?topic_id={target.topic_id}",
                        self.auth,
                        prefer_chrome_fetch=self.auth.config.prefer_chrome_fetch,
                        expect_json=True,
                    )
                    if r_resp.ok:
                        rj = json.loads(r_resp.body)
                        raw_replies = parse_api_v1_replies_json(rj, parsed.article.authors)
                    comments = build_reply_tree(raw_replies, parsed.article.authors)
                    article = parsed.article.model_copy(update={"comments": comments})
                    return ArticleFetchResult(
                        article=article,
                        total_replies=count_comment_nodes(comments),
                        pages_fetched=1,
                        total_pages=1,
                        source_mode="api_v1_fallback",
                        ok=True,
                    )
                except (json.JSONDecodeError, V2exError):
                    pass

        return primary_res
