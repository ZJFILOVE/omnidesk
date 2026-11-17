"""Discourse JSON API client for Shuiyuan categories, topic lists, search, threads, and nested comments."""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from omnidesk_sdk import CommentNode, PluginContext, TextArticle, TextFeedItem, TextParagraph

if __package__:
    from .shuiyuan_auth import AuthManager, AuthResolution
    from .shuiyuan_html import (
        decode_html_entities,
        parse_cooked_to_comment_text,
        parse_cooked_to_paragraphs,
        strip_html_and_decode,
    )
    from .shuiyuan_http import (
        HttpClientProtocol,
        HttpRequest,
        detect_challenge,
        detect_sso_or_login_wall,
    )
    from .shuiyuan_security import (
        DEFAULT_BASE_URL,
        ShuiyuanError,
        normalize_base_url,
        redact_sensitive,
        resolve_url,
        url_origin,
    )
    from .shuiyuan_subscriptions import DiscourseCategory, SubscriptionStore
else:
    from shuiyuan_auth import AuthManager, AuthResolution
    from shuiyuan_html import (
        decode_html_entities,
        parse_cooked_to_comment_text,
        parse_cooked_to_paragraphs,
        strip_html_and_decode,
    )
    from shuiyuan_http import (
        HttpClientProtocol,
        HttpRequest,
        detect_challenge,
        detect_sso_or_login_wall,
    )
    from shuiyuan_security import (
        DEFAULT_BASE_URL,
        ShuiyuanError,
        normalize_base_url,
        redact_sensitive,
        resolve_url,
        url_origin,
    )
    from shuiyuan_subscriptions import DiscourseCategory, SubscriptionStore

_POST_BATCH_SIZE = 20


@dataclass(slots=True)
class FetchStatus:
    """Structured outcome metadata from a Discourse fetch call."""

    ok: bool = False
    auth_required: bool = False
    session_expired: bool = False
    challenge_detected: bool = False
    http_status: int = 0
    error_code: str = ""
    error: str = ""
    effective_url: str = ""
    has_more: bool = False


@dataclass(slots=True)
class CommentsPageResult:
    """Paginated nested comment tree slice returned by `text.comments.v1`."""

    comments: list[CommentNode] = field(default_factory=list)
    total_posts: int = 0
    loaded_count: int = 0
    offset: int = 0
    next_offset: int = 0
    has_more: bool = False


def _safe_int(obj: Mapping[str, Any], key: str, default: int = 0) -> int:
    val = obj.get(key)
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val.strip())
        except ValueError:
            return default
    return default


def _safe_str(obj: Mapping[str, Any], key: str, default: str = "") -> str:
    val = obj.get(key)
    return val if isinstance(val, str) else default


def _safe_str_chain(obj: Mapping[str, Any], keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        val = _safe_str(obj, key)
        if val:
            return val
    return default


def _extract_like_count(post: Mapping[str, Any]) -> int:
    lc = post.get("like_count")
    if isinstance(lc, int) and not isinstance(lc, bool):
        return lc
    actions = post.get("actions_summary")
    if isinstance(actions, list):
        for act in actions:
            if isinstance(act, Mapping) and _safe_int(act, "id", 0) == 2:
                return _safe_int(act, "count", 0)
    return 0


def _format_display_author(username: str, name: str) -> str:
    u = username.strip()
    n = name.strip()
    if n and u and n != u:
        return f"{n} (@{u})"
    if u:
        return f"@{u}"
    if n:
        return n
    return "匿名水友"


def _collect_categories_recursive(
    arr: Any,
    parent_id: int,
    out: list[DiscourseCategory],
) -> None:
    if not isinstance(arr, list):
        return
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        cid = _safe_int(item, "id", 0)
        slug = _safe_str(item, "slug").strip()
        name = decode_html_entities(_safe_str(item, "name").strip())
        desc = strip_html_and_decode(_safe_str_chain(item, ("description_text", "description")))
        cat = DiscourseCategory(
            id=cid,
            slug=slug,
            name=name or slug,
            description=desc,
            topic_count=max(0, _safe_int(item, "topic_count", 0)),
            topics_week=max(0, _safe_int(item, "topics_week", 0)),
            parent_id=_safe_int(item, "parent_category_id", parent_id),
        )
        if cat.id > 0 or cat.slug:
            out.append(cat)
        sub = item.get("subcategory_list")
        if isinstance(sub, list):
            _collect_categories_recursive(sub, cat.id, out)


class DiscourseClient:
    """Async Discourse JSON API client with Chrome `web.fetch.v1` / `web.extract.v1` escalation."""

    def __init__(
        self,
        http_client: HttpClientProtocol,
        auth: AuthManager,
        subs: SubscriptionStore,
        ctx: PluginContext | None = None,
        *,
        allow_loopback_http: bool = False,
    ) -> None:
        self._http = http_client
        self._auth = auth
        self._subs = subs
        self._ctx = ctx
        self._allow_loopback_http = allow_loopback_http
        self._base_url = DEFAULT_BASE_URL
        self._timeout_sec = 15.0
        self._prefer_chrome_fetch = False

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    @property
    def base_url(self) -> str:
        return self._base_url

    def configure(
        self,
        base_url: str,
        timeout_sec: float,
        prefer_chrome_fetch: bool,
    ) -> None:
        self._base_url = normalize_base_url(base_url, allow_loopback_http=self._allow_loopback_http)
        self._timeout_sec = max(0.1, min(float(timeout_sec), 120.0))
        self._prefer_chrome_fetch = bool(prefer_chrome_fetch)

    def _allowed_origins(self) -> set[str]:
        if self._allow_loopback_http and self._base_url.startswith("http://"):
            return {url_origin(self._base_url)}
        return set()

    @staticmethod
    def extract_topic_id(article_id: str) -> int:
        """Extract positive integer Discourse topic ID from `topic:<id>`, `<id>`, or `/t/.../<id>` URL."""
        s = str(article_id or "").strip()
        if not s:
            return 0
        if s.startswith("shuiyuan:topic:"):
            s = s[len("shuiyuan:topic:") :]
        elif s.startswith("topic:"):
            s = s[len("topic:") :]
        if s.isdigit():
            return int(s)
        m = re.search(r"/t/(?:[^/?#]+/)?(\d+)", s)
        if m:
            return int(m.group(1))
        return 0

    @staticmethod
    def format_timestamp(iso_utc: str) -> str:
        """Format ISO-8601 UTC timestamp to local `YYYY-MM-DD HH:MM`."""
        s = str(iso_utc or "").strip()
        if len(s) < 16:
            return s
        try:
            normalized = s[:-1] + "+00:00" if s.endswith("Z") else s
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            if len(s) >= 16 and s[4] == "-" and s[7] == "-" and s[10] in {"T", " "}:
                return f"{s[:10]} {s[11:16]}"
            return s

    async def _try_chrome_fetch_json(
        self,
        url: str,
        auth_res: AuthResolution,
        status: FetchStatus,
    ) -> dict[str, Any] | None:
        if self._ctx is None or not self._ctx.capabilities.has("web.fetch.v1"):
            return None
        parsed = urllib.parse.urlsplit(url)
        request_payload: dict[str, Any] = {
            "url": url,
            "method": "GET",
            "headers": {"Accept": "application/json"},
            "body": "",
            "credentials": "include",
            "timeout_ms": int(max(100, self._timeout_sec * 1000)),
        }
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            request_payload["open_tab_if_missing"] = True
            request_payload["tab_url"] = f"{parsed.scheme}://{parsed.netloc}/"
        try:
            res = await self._ctx.capabilities.call(
                "web.fetch.v1",
                request_payload,
            )
        except Exception:
            return None
        if not res.ok or not isinstance(res.data, Mapping):
            return None

        code = _safe_int(res.data, "status", 0)
        eff_url = _safe_str(res.data, "url", url)
        raw_hdrs = res.data.get("headers")
        hdrs = {str(k).lower(): str(v) for k, v in raw_hdrs.items()} if isinstance(raw_hdrs, Mapping) else {}
        body = _safe_str(res.data, "body")
        status.http_status = code
        status.effective_url = eff_url

        if detect_challenge(code, hdrs, body):
            status.challenge_detected = True
            return None
        if code in {401, 403} or detect_sso_or_login_wall(code, eff_url, hdrs, body):
            status.auth_required = True
            status.session_expired = auth_res.has_t_cookie or auth_res.has_session_cookie
            status.error_code = "permission_denied" if status.session_expired else "not_configured"
            status.error = (
                "交大水源会话已过期，需通过 jAccount 重新登录"
                if status.session_expired
                else "交大水源需要 jAccount SSO 登录后访问"
            )
            return None
        if code < 200 or code >= 300 or not body:
            return None
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        if parsed.get("error_type") in {"not_logged_in", "invalid_access"}:
            status.auth_required = True
            status.session_expired = auth_res.has_t_cookie or auth_res.has_session_cookie
            status.error_code = "permission_denied" if status.session_expired else "not_configured"
            status.error = f"需要登录交大水源 (Discourse: {parsed.get('error_type')})"
            return None
        status.ok = True
        return parsed

    async def _try_chrome_extract_json(
        self,
        url: str,
        auth_res: AuthResolution,
        status: FetchStatus,
    ) -> dict[str, Any] | None:
        if self._ctx is None or not self._ctx.capabilities.has("web.extract.v1"):
            return None
        try:
            res = await self._ctx.capabilities.call(
                "web.extract.v1",
                {
                    "action": "extract",
                    "url": url,
                    "mode": "auto",
                    "use_cookies": True,
                    "render_js": True,
                    "timeout_ms": int(max(100, self._timeout_sec * 1000)),
                },
            )
        except Exception:
            return None
        if not res.ok or not isinstance(res.data, Mapping):
            return None
        eff_url = _safe_str(res.data, "url", url)
        html_text = _safe_str(res.data, "html")
        body_text = _safe_str(res.data, "text").strip()
        status.effective_url = eff_url
        if detect_challenge(200, {}, html_text or body_text):
            status.challenge_detected = True
            return None
        if detect_sso_or_login_wall(200, eff_url, {"content-type": "text/html"}, html_text or body_text):
            status.auth_required = True
            status.session_expired = auth_res.has_t_cookie or auth_res.has_session_cookie
            status.error_code = "permission_denied" if status.session_expired else "not_configured"
            status.error = "交大水源需要 jAccount SSO 登录后访问"
            return None
        if not body_text:
            return None
        try:
            parsed = json.loads(body_text)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        if parsed.get("error_type") in {"not_logged_in", "invalid_access"}:
            status.auth_required = True
            status.session_expired = auth_res.has_t_cookie or auth_res.has_session_cookie
            status.error_code = "permission_denied" if status.session_expired else "not_configured"
            status.error = f"需要登录交大水源 (Discourse: {parsed.get('error_type')})"
            return None
        status.ok = True
        status.http_status = 200
        return parsed

    async def _perform_get_json(
        self,
        path_or_url: str,
        auth_res: AuthResolution,
        status: FetchStatus,
    ) -> dict[str, Any] | None:
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        else:
            suffix = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
            url = f"{self._base_url}{suffix}"

        if self._prefer_chrome_fetch:
            chrome_doc = await self._try_chrome_fetch_json(url, auth_res, status)
            if chrome_doc is not None:
                self._auth.mark_authenticated() if auth_res.has_t_cookie else None
                return chrome_doc
            if status.auth_required:
                if status.session_expired:
                    self._auth.mark_session_expired(status.error)
                return None

        req = HttpRequest(
            url=url,
            method="GET",
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            cookie_header=auth_res.cookie_header,
            timeout_sec=self._timeout_sec,
        )
        res = await self._http.request(req, extra_secrets=self._auth.secrets_for_redaction())
        status.http_status = res.status_code
        status.effective_url = res.effective_url
        status.challenge_detected = res.challenge_detected
        secrets = self._auth.secrets_for_redaction()

        if res.error:
            status.error_code = res.error_code or "service_offline"
            status.error = redact_sensitive(res.error, secrets)
            return None

        if res.challenge_detected:
            for proxy_fn in (self._try_chrome_fetch_json, self._try_chrome_extract_json):
                proxy_doc = await proxy_fn(url, auth_res, status)
                if proxy_doc is not None:
                    if auth_res.has_t_cookie:
                        self._auth.mark_authenticated()
                    return proxy_doc
                if status.auth_required:
                    if status.session_expired:
                        self._auth.mark_session_expired(status.error)
                    return None
            status.challenge_detected = True
            status.error_code = "permission_denied"
            status.error = f"站点返回人机验证/Cloudflare 拦截 (HTTP {res.status_code})"
            return None

        if res.sso_redirect_detected or res.status_code in {401, 403}:
            status.auth_required = True
            status.session_expired = auth_res.has_t_cookie or auth_res.has_session_cookie
            status.error_code = "permission_denied" if status.session_expired else "not_configured"
            status.error = (
                "交大水源会话已过期，需通过 jAccount 重新登录"
                if status.session_expired
                else "交大水源需要 jAccount SSO 登录后访问"
            )
            if status.session_expired:
                self._auth.mark_session_expired(status.error)
            return None

        if res.status_code == 429:
            status.error_code = "rate_limited"
            status.error = f"水源社区请求限流 (HTTP 429, {url})"
            return None

        if res.status_code in {404, 410}:
            status.error_code = "not_found"
            status.error = f"请求的资源不存在 (HTTP {res.status_code})"
            return None

        if not (200 <= res.status_code < 300):
            status.error_code = "service_offline"
            status.error = f"HTTP {res.status_code} ({url})"
            return None

        try:
            parsed = json.loads(res.body)
        except json.JSONDecodeError:
            status.error_code = "service_offline"
            status.error = "Discourse 返回非 JSON 响应"
            return None

        if not isinstance(parsed, dict):
            status.error_code = "service_offline"
            status.error = "Discourse JSON 根结构无效"
            return None

        err_type = _safe_str(parsed, "error_type")
        if err_type in {"not_logged_in", "invalid_access"}:
            status.auth_required = True
            status.session_expired = auth_res.has_t_cookie or auth_res.has_session_cookie
            status.error_code = "permission_denied" if status.session_expired else "not_configured"
            status.error = f"需要登录交大水源 (Discourse: {err_type})"
            if status.session_expired:
                self._auth.mark_session_expired(status.error)
            return None

        status.ok = True
        if auth_res.has_t_cookie:
            self._auth.mark_authenticated()
        return parsed

    async def fetch_categories(self) -> tuple[list[DiscourseCategory], FetchStatus]:
        status = FetchStatus()
        auth_res = await self._auth.resolve_cookies(persist_chrome_snapshot=True)
        doc = await self._perform_get_json("/categories.json?include_subcategories=true", auth_res, status)
        if doc is None:
            return [], status
        cat_list = doc.get("category_list")
        if not isinstance(cat_list, Mapping) or not isinstance(cat_list.get("categories"), list):
            status.ok = False
            status.error_code = "service_offline"
            status.error = "Discourse categories.json 缺少 category_list.categories"
            return [], status
        out: list[DiscourseCategory] = []
        _collect_categories_recursive(cat_list["categories"], 0, out)
        if out:
            self._subs.update_discovered(out)
        return out, status

    def _parse_topic_list_json(self, doc: Mapping[str, Any]) -> tuple[list[TextFeedItem], bool]:
        users_by_id: dict[int, str] = {}
        raw_users = doc.get("users")
        if isinstance(raw_users, list):
            for u in raw_users:
                if not isinstance(u, Mapping):
                    continue
                uid = _safe_int(u, "id", 0)
                if uid > 0:
                    users_by_id[uid] = _format_display_author(_safe_str(u, "username"), _safe_str(u, "name"))

        tl = doc.get("topic_list")
        if not isinstance(tl, Mapping) or not isinstance(tl.get("topics"), list):
            raise ShuiyuanError("service_offline", "Discourse topic_list.topics 字段缺失或格式无效")

        has_more = bool(_safe_str(tl, "more_topics_url").strip())
        items: list[TextFeedItem] = []
        allowed_origins = self._allowed_origins()
        for t in tl["topics"]:
            if not isinstance(t, Mapping):
                continue
            tid = _safe_int(t, "id", 0)
            if tid <= 0:
                continue
            raw_title = _safe_str(t, "title").strip()
            title = decode_html_entities(raw_title) if raw_title else strip_html_and_decode(_safe_str(t, "fancy_title"))
            if not title:
                continue
            slug = _safe_str(t, "slug").strip()
            topic_url = f"{self._base_url}/t/{slug}/{tid}" if slug else f"{self._base_url}/t/{tid}"

            author = ""
            posters = t.get("posters")
            if isinstance(posters, list) and posters:
                op_uid = 0
                for p in posters:
                    if not isinstance(p, Mapping):
                        continue
                    desc = _safe_str(p, "description")
                    if "Original Poster" in desc or "原始发帖人" in desc:
                        op_uid = _safe_int(p, "user_id", 0)
                        break
                if op_uid == 0 and isinstance(posters[0], Mapping):
                    op_uid = _safe_int(posters[0], "user_id", 0)
                author = users_by_id.get(op_uid, "")

            posts_count = _safe_int(t, "posts_count", 1)
            comment_count = max(0, posts_count - 1)
            score = _safe_int(t, "like_count", 0)
            published_at = self.format_timestamp(_safe_str_chain(t, ("last_posted_at", "created_at")))

            cat_id = _safe_int(t, "category_id", 0)
            cat_name = self._subs.category_name_by_id(cat_id)
            tags: list[str] = [cat_name] if cat_name else []
            raw_tags = t.get("tags")
            if isinstance(raw_tags, list):
                for tg in raw_tags:
                    if isinstance(tg, str) and tg.strip():
                        tags.append(tg.strip())
                    elif isinstance(tg, Mapping):
                        tname = _safe_str(tg, "name").strip()
                        if tname:
                            tags.append(tname)

            cover_raw = _safe_str(t, "image_url").strip()
            cover_url = (
                resolve_url(
                    self._base_url,
                    cover_raw,
                    allow_loopback_http=self._allow_loopback_http,
                    allowed_loopback_origins=allowed_origins,
                )
                if cover_raw
                else ""
            )

            excerpt = strip_html_and_decode(_safe_str(t, "excerpt"))
            if not excerpt:
                prefix = f"[{cat_name}] " if cat_name else ""
                auth_part = f"{author} · " if author else ""
                excerpt = f"{prefix}{auth_part}{comment_count} 回复 · {_safe_int(t, 'views', 0)} 浏览"

            items.append(
                TextFeedItem(
                    id=f"topic:{tid}",
                    source_id="plugin-shuiyuan",
                    source_name="交大水源",
                    category="shuiyuan",
                    channel_id=f"cat:{cat_id}" if cat_id > 0 else "",
                    title=title,
                    excerpt=excerpt,
                    author=author,
                    published_at=published_at,
                    url=topic_url,
                    cover_url=cover_url,
                    tags=tags,
                    comment_count=comment_count,
                    score=score,
                )
            )
        return items, has_more

    def _parse_search_json(self, doc: Mapping[str, Any]) -> list[TextFeedItem]:
        if not isinstance(doc.get("topics"), list):
            if doc.get("posts") == [] or (doc.get("topics") is None and "grouped_search_result" in doc):
                return []
            raise ShuiyuanError("service_offline", "Discourse search.json 缺少 topics 数组")

        post_by_topic: dict[int, Mapping[str, Any]] = {}
        raw_posts = doc.get("posts")
        if isinstance(raw_posts, list):
            for p in raw_posts:
                if isinstance(p, Mapping):
                    tid = _safe_int(p, "topic_id", 0)
                    if tid > 0 and tid not in post_by_topic:
                        post_by_topic[tid] = p

        items: list[TextFeedItem] = []
        for t in doc["topics"]:
            if not isinstance(t, Mapping):
                continue
            tid = _safe_int(t, "id", 0)
            if tid <= 0:
                continue
            title = decode_html_entities(_safe_str(t, "title").strip()) or strip_html_and_decode(
                _safe_str(t, "fancy_title")
            )
            if not title:
                continue
            slug = _safe_str(t, "slug").strip()
            topic_url = f"{self._base_url}/t/{slug}/{tid}" if slug else f"{self._base_url}/t/{tid}"
            posts_count = _safe_int(t, "posts_count", 1)
            comment_count = max(0, posts_count - 1)
            published_at = self.format_timestamp(_safe_str_chain(t, ("last_posted_at", "created_at")))

            cat_id = _safe_int(t, "category_id", 0)
            cat_name = self._subs.category_name_by_id(cat_id)
            tags: list[str] = [cat_name] if cat_name else []
            raw_tags = t.get("tags")
            if isinstance(raw_tags, list):
                for tg in raw_tags:
                    if isinstance(tg, str) and tg.strip():
                        tags.append(tg.strip())
                    elif isinstance(tg, Mapping):
                        tname = _safe_str(tg, "name").strip()
                        if tname:
                            tags.append(tname)

            author = ""
            score = 0
            excerpt = ""
            p = post_by_topic.get(tid)
            if p is not None:
                author = _format_display_author(_safe_str(p, "username"), _safe_str(p, "name"))
                score = _safe_int(p, "like_count", 0)
                excerpt = strip_html_and_decode(_safe_str(p, "blurb"))
            if not excerpt:
                prefix = f"[{cat_name}] " if cat_name else ""
                excerpt = f"{prefix}{comment_count} 回复"

            items.append(
                TextFeedItem(
                    id=f"topic:{tid}",
                    source_id="plugin-shuiyuan",
                    source_name="交大水源",
                    category="shuiyuan",
                    channel_id=f"cat:{cat_id}" if cat_id > 0 else "",
                    title=title,
                    excerpt=excerpt,
                    author=author,
                    published_at=published_at,
                    url=topic_url,
                    tags=tags,
                    comment_count=comment_count,
                    score=score,
                )
            )
        return items

    async def fetch_topic_list(
        self,
        channel_id: str = "",
        query: str = "",
        *,
        page: int = 1,
    ) -> tuple[list[TextFeedItem], FetchStatus]:
        status = FetchStatus()
        if page < 1:
            status.ok = True
            return [], status

        auth_res = await self._auth.resolve_cookies(persist_chrome_snapshot=True)
        q = str(query or "").strip()
        ch = str(channel_id or "").strip()

        if ch != "unread" and self._subs.needs_category_refresh():
            await self.fetch_categories()

        if q:
            effective_q = q
            filter_cat_id = 0
            if ch.startswith("cat:"):
                cat = self._subs.resolve_category(ch)
                if cat is not None:
                    if cat.slug:
                        effective_q += f" #{cat.slug}"
                    elif cat.id > 0:
                        filter_cat_id = cat.id
            path = f"/search.json?q={urllib.parse.quote(effective_q, safe='')}"
            if page > 1:
                path += f"&page={page}"
            doc = await self._perform_get_json(path, auth_res, status)
            if doc is None:
                return [], status
            try:
                items = self._parse_search_json(doc)
            except ShuiyuanError as exc:
                status.ok = False
                status.error_code = exc.error_code
                status.error = str(exc)
                return [], status
            if filter_cat_id > 0 and isinstance(doc.get("topics"), list):
                allowed_tids = {
                    _safe_int(t, "id", 0)
                    for t in doc["topics"]
                    if isinstance(t, Mapping) and _safe_int(t, "category_id", 0) == filter_cat_id
                }
                items = [it for it in items if self.extract_topic_id(it.id) in allowed_tids]
            return items, status

        if ch == "pinned":
            targets = self._subs.list_pinned()
            if not targets:
                disc = sorted(self._subs.list_discovered(), key=lambda c: -c.topics_week)
                targets = disc[:2]
            if targets:
                merged: list[TextFeedItem] = []
                seen_ids: set[str] = set()
                any_ok = False
                any_more = False
                first_err: FetchStatus | None = None
                for cat in targets[:3]:
                    ep = self._category_endpoint(cat, page=page)
                    sub_st = FetchStatus()
                    doc = await self._perform_get_json(ep, auth_res, sub_st)
                    if doc is not None:
                        try:
                            parsed_items, sub_more = self._parse_topic_list_json(doc)
                        except ShuiyuanError as exc:
                            if first_err is None:
                                sub_st.ok = False
                                sub_st.error_code = exc.error_code
                                sub_st.error = str(exc)
                                first_err = sub_st
                            continue
                        any_ok = True
                        any_more = any_more or sub_more
                        status = sub_st
                        for it in parsed_items:
                            if it.id not in seen_ids:
                                seen_ids.add(it.id)
                                merged.append(it)
                    elif first_err is None:
                        first_err = sub_st
                if any_ok:
                    status.has_more = any_more
                    return merged, status
                return [], (first_err or status)

        if ch == "top":
            endpoint = "/top.json?period=weekly"
        elif ch == "unread":
            endpoint = "/unread.json"
        elif ch.startswith("cat:"):
            cat = self._subs.resolve_category(ch)
            endpoint = self._category_endpoint(cat, page=1) if cat is not None else "/latest.json"
        else:
            endpoint = "/latest.json"

        if page > 1:
            sep = "&" if "?" in endpoint else "?"
            endpoint = f"{endpoint}{sep}page={page - 1}"

        doc = await self._perform_get_json(endpoint, auth_res, status)
        if doc is None:
            return [], status
        try:
            items, has_more = self._parse_topic_list_json(doc)
        except ShuiyuanError as exc:
            status.ok = False
            status.error_code = exc.error_code
            status.error = str(exc)
            return [], status
        status.has_more = has_more
        return items, status

    @staticmethod
    def _category_endpoint(cat: DiscourseCategory, *, page: int) -> str:
        if cat.id > 0 and cat.slug:
            path = f"/c/{urllib.parse.quote(cat.slug, safe='')}/{cat.id}.json"
        elif cat.id > 0:
            path = f"/c/{cat.id}.json"
        else:
            path = f"/c/{urllib.parse.quote(cat.slug, safe='')}.json"
        if page > 1:
            path += f"?page={page - 1}"
        return path

    async def _fetch_posts_by_ids(
        self,
        topic_id: int,
        post_ids: list[int],
        auth_res: AuthResolution,
        status: FetchStatus,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if topic_id <= 0 or not post_ids:
            return out
        for start in range(0, len(post_ids), _POST_BATCH_SIZE):
            batch = post_ids[start : start + _POST_BATCH_SIZE]
            qs = "&".join(f"post_ids[]={pid}" for pid in batch)
            batch_st = FetchStatus()
            doc = await self._perform_get_json(f"/t/{topic_id}/posts.json?{qs}", auth_res, batch_st)
            if doc is None:
                if not status.error:
                    status.error = batch_st.error
                    status.error_code = batch_st.error_code
                break
            ps = doc.get("post_stream")
            if isinstance(ps, Mapping) and isinstance(ps.get("posts"), list):
                for p in ps["posts"]:
                    if isinstance(p, dict):
                        out.append(p)
        return out

    @classmethod
    def build_comment_tree(
        cls,
        posts: list[dict[str, Any]],
        op_username: str,
        base_url: str = DEFAULT_BASE_URL,
    ) -> list[CommentNode]:
        """Build recursive `CommentNode` forest ordered by floor `post_number`."""
        replies: list[dict[str, Any]] = []
        seen_pids: set[int] = set()
        for p in posts:
            if not isinstance(p, Mapping):
                continue
            pnum = _safe_int(p, "post_number", 0)
            pid = _safe_int(p, "id", 0)
            if pnum <= 1:
                continue
            if pid > 0:
                if pid in seen_pids:
                    continue
                seen_pids.add(pid)
            replies.append(dict(p))

        replies.sort(key=lambda item: _safe_int(item, "post_number", 0))

        items: list[dict[str, Any]] = []
        post_num_to_idx: dict[int, int] = {}

        for p in replies:
            pid = _safe_int(p, "id", 0)
            pnum = _safe_int(p, "post_number", 0)
            username = _safe_str(p, "username").strip()
            name = _safe_str(p, "name").strip()
            reply_to_pnum = _safe_int(p, "reply_to_post_number", 0)

            badges: list[str] = []
            if op_username and username == op_username:
                badges.append("OP")
            if p.get("admin") is True:
                badges.append("管理员")
            elif p.get("moderator") is True:
                badges.append("版主")
            if pnum > 0:
                badges.append(f"#{pnum}")

            body = parse_cooked_to_comment_text(_safe_str(p, "cooked"), base_url)
            if not body:
                raw = _safe_str(p, "raw").strip()
                body = raw or "[无文本内容]"

            rtu = p.get("reply_to_user")
            if reply_to_pnum >= 2 and isinstance(rtu, Mapping):
                ruser = _safe_str(rtu, "username").strip()
                if ruser and not body.startswith(f"@{ruser}"):
                    body = f"回复 @{ruser} (#{reply_to_pnum}):\n{body}"

            node = CommentNode(
                id=f"post-{pid}" if pid > 0 else f"floor-{pnum}",
                author=_format_display_author(username, name),
                role_badge=" · ".join(badges),
                body=body,
                created_at=cls.format_timestamp(_safe_str(p, "created_at")),
                score=_extract_like_count(p),
            )
            idx = len(items)
            if pnum > 0:
                post_num_to_idx[pnum] = idx
            items.append(
                {
                    "node": node,
                    "post_number": pnum,
                    "reply_to_post_number": reply_to_pnum,
                    "children": [],
                }
            )

        root_indices: list[int] = []
        for idx, item in enumerate(items):
            parent_pnum = item["reply_to_post_number"]
            if 2 <= parent_pnum < item["post_number"] and parent_pnum in post_num_to_idx:
                pidx = post_num_to_idx[parent_pnum]
                if pidx != idx:
                    items[pidx]["children"].append(idx)
                    continue
            root_indices.append(idx)

        visited = [False] * len(items)

        def _materialize(idx: int) -> CommentNode:
            visited[idx] = True
            node: CommentNode = items[idx]["node"]
            child_nodes = [_materialize(ch) for ch in items[idx]["children"] if not visited[ch]]
            return node.model_copy(update={"replies": child_nodes})

        roots: list[CommentNode] = []
        for ridx in root_indices:
            if not visited[ridx]:
                roots.append(_materialize(ridx))
        for idx in range(len(items)):
            if not visited[idx]:
                roots.append(_materialize(idx))
        return roots

    async def fetch_topic_article(
        self,
        article_id: str,
        *,
        max_initial_posts: int = 60,
    ) -> tuple[TextArticle | None, FetchStatus, dict[str, int]]:
        status = FetchStatus()
        tid = self.extract_topic_id(article_id)
        if tid <= 0:
            status.error_code = "invalid_argument"
            status.error = f"无效的话题 ID: {article_id}"
            return None, status, {}

        auth_res = await self._auth.resolve_cookies(persist_chrome_snapshot=True)
        doc = await self._perform_get_json(f"/t/{tid}.json?track_visit=false", auth_res, status)
        if doc is None:
            return None, status, {}

        title = decode_html_entities(_safe_str(doc, "title").strip()) or strip_html_and_decode(
            _safe_str(doc, "fancy_title")
        )
        if not title:
            status.ok = False
            status.error_code = "service_offline"
            status.error = "Discourse 帖子响应缺少标题字段"
            return None, status, {}

        slug = _safe_str(doc, "slug").strip()
        topic_url = f"{self._base_url}/t/{slug}/{tid}" if slug else f"{self._base_url}/t/{tid}"
        published_at = self.format_timestamp(_safe_str(doc, "created_at"))

        op_username = op_name = ""
        details = doc.get("details")
        if isinstance(details, Mapping) and isinstance(details.get("created_by"), Mapping):
            cb = details["created_by"]
            op_username = _safe_str(cb, "username").strip()
            op_name = _safe_str(cb, "name").strip()

        cat_id = _safe_int(doc, "category_id", 0)
        cat_name = self._subs.category_name_by_id(cat_id)
        tags: list[str] = [cat_name] if cat_name else []
        raw_tags = doc.get("tags")
        if isinstance(raw_tags, list):
            for tg in raw_tags:
                if isinstance(tg, str) and tg.strip():
                    tags.append(tg.strip())
                elif isinstance(tg, Mapping):
                    tname = _safe_str(tg, "name").strip()
                    if tname:
                        tags.append(tname)

        all_posts: list[dict[str, Any]] = []
        loaded_ids: set[int] = set()
        stream_ids: list[int] = []
        ps = doc.get("post_stream")
        if isinstance(ps, Mapping):
            if isinstance(ps.get("posts"), list):
                for p in ps["posts"]:
                    if isinstance(p, dict):
                        all_posts.append(p)
                        pid = _safe_int(p, "id", 0)
                        if pid > 0:
                            loaded_ids.add(pid)
            if isinstance(ps.get("stream"), list):
                for sid in ps["stream"]:
                    if isinstance(sid, int) and not isinstance(sid, bool):
                        stream_ids.append(sid)

        if not op_username:
            for p in all_posts:
                if _safe_int(p, "post_number", 0) == 1:
                    op_username = _safe_str(p, "username").strip()
                    op_name = _safe_str(p, "name").strip()
                    break

        authors = _format_display_author(op_username, op_name)
        posts_count = _safe_int(doc, "posts_count", len(all_posts))
        sub_parts = [authors]
        if cat_name:
            sub_parts.append(cat_name)
        if published_at:
            sub_parts.append(published_at)
        sub_parts.append(f"{posts_count} 楼")
        subtitle = " · ".join(sub_parts)

        safe_max = max(1, min(int(max_initial_posts), 200))
        if safe_max > len(all_posts) and len(stream_ids) > len(all_posts):
            extra_ids: list[int] = []
            for pid in stream_ids:
                if pid in loaded_ids:
                    continue
                extra_ids.append(pid)
                if len(all_posts) + len(extra_ids) >= safe_max:
                    break
            if extra_ids:
                extra_st = FetchStatus()
                all_posts.extend(await self._fetch_posts_by_ids(tid, extra_ids, auth_res, extra_st))

        paragraphs: list[TextParagraph] = []
        for p in all_posts:
            if _safe_int(p, "post_number", 0) == 1:
                cooked = _safe_str(p, "cooked")
                paragraphs = parse_cooked_to_paragraphs(cooked, self._base_url)
                if not paragraphs:
                    fallback = strip_html_and_decode(cooked) or _safe_str(p, "raw").strip()
                    if fallback:
                        paragraphs.append(TextParagraph(id="p1", kind="paragraph", original=fallback))
                break

        comments = self.build_comment_tree(all_posts, op_username, self._base_url)
        if len(stream_ids) > len(all_posts):
            remaining = len(stream_ids) - len(all_posts)
            paragraphs.append(
                TextParagraph(
                    id="p_more_comments",
                    kind="quote",
                    original=(
                        f"提示：本帖共 {len(stream_ids)} 楼，当前已加载前 {len(all_posts)} 楼，"
                        f"剩余 {remaining} 条回复可通过 text.comments.v1 分页按需加载。"
                    ),
                )
            )

        article = TextArticle(
            id=f"topic:{tid}",
            source_id="plugin-shuiyuan",
            source_name="交大水源",
            category="shuiyuan",
            channel_id=f"cat:{cat_id}" if cat_id > 0 else "",
            title=title,
            subtitle=subtitle,
            authors=authors,
            author=authors,
            published_at=published_at,
            url=topic_url,
            tags=tags,
            paragraphs=paragraphs,
            comments=comments,
        )
        meta = {
            "total_posts": len(stream_ids) if stream_ids else len(all_posts),
            "loaded_posts": len(all_posts),
            "has_more_comments": len(stream_ids) > len(all_posts),
        }
        return article, status, meta

    async def fetch_topic_comments_page(
        self,
        article_id: str,
        *,
        offset: int = 0,
        limit: int = 40,
    ) -> tuple[CommentsPageResult, FetchStatus]:
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(int(limit), 100))
        res = CommentsPageResult(offset=safe_offset)

        status = FetchStatus()
        tid = self.extract_topic_id(article_id)
        if tid <= 0:
            status.error_code = "invalid_argument"
            status.error = f"无效的话题 ID: {article_id}"
            return res, status

        auth_res = await self._auth.resolve_cookies(persist_chrome_snapshot=True)
        doc = await self._perform_get_json(f"/t/{tid}.json?track_visit=false", auth_res, status)
        if doc is None:
            return res, status

        op_username = ""
        details = doc.get("details")
        if isinstance(details, Mapping) and isinstance(details.get("created_by"), Mapping):
            op_username = _safe_str(details["created_by"], "username").strip()

        loaded_by_id: dict[int, dict[str, Any]] = {}
        stream_ids: list[int] = []
        ps = doc.get("post_stream")
        if isinstance(ps, Mapping):
            if isinstance(ps.get("posts"), list):
                for p in ps["posts"]:
                    if isinstance(p, dict):
                        pid = _safe_int(p, "id", 0)
                        if pid > 0:
                            loaded_by_id[pid] = p
                        if _safe_int(p, "post_number", 0) == 1 and not op_username:
                            op_username = _safe_str(p, "username").strip()
            if isinstance(ps.get("stream"), list):
                for sid in ps["stream"]:
                    if isinstance(sid, int) and not isinstance(sid, bool):
                        stream_ids.append(sid)

        reply_stream_ids = stream_ids[1:] if stream_ids else []
        res.total_posts = len(reply_stream_ids)
        if safe_offset >= len(reply_stream_ids):
            return res, status

        end = min(len(reply_stream_ids), safe_offset + safe_limit)
        target_ids = reply_stream_ids[safe_offset:end]
        missing_ids = [pid for pid in target_ids if pid not in loaded_by_id]
        if missing_ids:
            for fp in await self._fetch_posts_by_ids(tid, missing_ids, auth_res, status):
                pid = _safe_int(fp, "id", 0)
                if pid > 0:
                    loaded_by_id[pid] = fp

        page_posts = [loaded_by_id[pid] for pid in target_ids if pid in loaded_by_id]
        res.comments = self.build_comment_tree(page_posts, op_username, self._base_url)
        res.loaded_count = len(page_posts)
        res.next_offset = end
        res.has_more = end < len(reply_stream_ids)
        return res, status
