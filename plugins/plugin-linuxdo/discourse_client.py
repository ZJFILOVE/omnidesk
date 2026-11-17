"""Discourse JSON API client with Cloudflare Chrome proxy fallback and nested comment tree builder."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from omnidesk_sdk import CommentNode, PluginContext, TextArticle, TextFeedItem, TextParagraph

if __package__:
    from .auth import AuthManager, AuthResolution
    from .html_parser import (
        decode_html_entities,
        parse_cooked_to_comment_text,
        parse_cooked_to_paragraphs,
        parse_extract_dom_payload,
        strip_html_and_decode,
    )
    from .http_client import (
        MAX_RESPONSE_BYTES,
        HttpClientProtocol,
        HttpRequest,
        SafeHttpsClient,
        detect_challenge,
        detect_login_wall,
    )
    from .security import (
        DEFAULT_BASE_URL,
        LinuxdoError,
        extract_topic_id,
        json_bool,
        json_int,
        json_str,
        json_str_chain,
        redact_text,
        url_origin,
        validate_https_url,
    )
    from .subscriptions import DiscourseCategory, SubscriptionStore
else:
    from auth import AuthManager, AuthResolution  # type: ignore[no-redef]
    from html_parser import (  # type: ignore[no-redef]
        decode_html_entities,
        parse_cooked_to_comment_text,
        parse_cooked_to_paragraphs,
        parse_extract_dom_payload,
        strip_html_and_decode,
    )
    from http_client import (  # type: ignore[no-redef]
        MAX_RESPONSE_BYTES,
        HttpClientProtocol,
        HttpRequest,
        SafeHttpsClient,
        detect_challenge,
        detect_login_wall,
    )
    from security import (  # type: ignore[no-redef]
        DEFAULT_BASE_URL,
        LinuxdoError,
        extract_topic_id,
        json_bool,
        json_int,
        json_str,
        json_str_chain,
        redact_text,
        url_origin,
        validate_https_url,
    )
    from subscriptions import DiscourseCategory, SubscriptionStore  # type: ignore[no-redef]


@dataclass
class FetchStatus:
    """Telemetry and fail-closed status for a Discourse fetch operation."""

    ok: bool = False
    auth_required: bool = False
    session_expired: bool = False
    challenge_detected: bool = False
    fallback_used: bool = False
    http_status: int = 0
    transport: str = "urllib"
    error_code: str = ""
    error: str = ""
    effective_url: str = ""


@dataclass
class CommentsPageResult:
    """Paginated nested comment tree slice returned by `text.comments.v1`."""

    comments: list[CommentNode] = field(default_factory=list)
    total_posts: int = 0
    loaded_count: int = 0
    offset: int = 0
    next_offset: int = 0
    has_more: bool = False
    transport: str = "urllib"


def format_timestamp(iso_utc: str) -> str:
    """Format Discourse ISO-8601 UTC timestamps into local `YYYY-MM-DD HH:MM`."""
    s = (iso_utc or "").strip()
    if len(s) < 16:
        return s
    try:
        normalized = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        if len(s) >= 16 and s[4] == "-" and s[7] == "-" and s[10] in {"T", " "}:
            return f"{s[:10]} {s[11:16]}"
        return s


def _extract_like_count(post: Mapping[str, Any]) -> int:
    if isinstance(post.get("like_count"), int) and not isinstance(post["like_count"], bool):
        return max(0, post["like_count"])
    actions = post.get("actions_summary")
    if isinstance(actions, list):
        for act in actions:
            if isinstance(act, Mapping) and json_int(act, "id", 0) == 2:
                return max(0, json_int(act, "count", 0))
    return 0


def _format_display_author(username: str, name: str) -> str:
    u = (username or "").strip()
    n = (name or "").strip()
    if n and u and n != u:
        return f"{n} (@{u})"
    if u:
        return f"@{u}"
    if n:
        return n
    return "匿名佬友"


def _strip_json_endpoint_extension(url: str) -> str:
    path_part, sep, query_part = url.partition("?")
    if path_part.lower().endswith(".json") and len(path_part) > 5:
        return path_part[:-5] + (sep + query_part if sep else "")
    return ""


def _collect_categories_recursive(
    arr: Any, parent_id: int, out: list[DiscourseCategory]
) -> None:
    if not isinstance(arr, list):
        return
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        cid = json_int(item, "id", 0)
        slug = json_str(item, "slug", "").strip()
        name = decode_html_entities(json_str(item, "name", "").strip())
        desc = strip_html_and_decode(json_str_chain(item, ("description_text", "description")))
        cat = DiscourseCategory(
            id=cid,
            slug=slug,
            name=name or slug,
            description=desc,
            topic_count=json_int(item, "topic_count", 0),
            topics_week=json_int(item, "topics_week", 0),
            parent_id=json_int(item, "parent_category_id", parent_id),
        )
        if cat.id > 0 or cat.slug:
            out.append(cat)
        sub_list = item.get("subcategory_list")
        if isinstance(sub_list, list):
            _collect_categories_recursive(sub_list, cat.id, out)


def build_comment_tree(
    posts: Sequence[Mapping[str, Any]],
    *,
    op_username: str = "",
    base_url: str = DEFAULT_BASE_URL,
) -> list[CommentNode]:
    """Reconstruct multi-level nested `CommentNode` tree from Discourse reply posts."""
    replies: list[Mapping[str, Any]] = []
    seen_ids: set[int] = set()
    for p in posts:
        if not isinstance(p, Mapping):
            continue
        pnum = json_int(p, "post_number", 0)
        pid = json_int(p, "id", 0)
        if pnum <= 1:
            continue
        if pid > 0:
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
        replies.append(p)

    replies.sort(key=lambda p: json_int(p, "post_number", 0))

    items: list[dict[str, Any]] = []
    post_num_to_idx: dict[int, int] = {}

    for p in replies:
        pid = json_int(p, "id", 0)
        pnum = json_int(p, "post_number", 0)
        username = json_str(p, "username", "").strip()
        name = json_str(p, "name", "").strip()
        reply_to = json_int(p, "reply_to_post_number", 0)

        badges: list[str] = []
        if op_username and username == op_username:
            badges.append("OP")
        if json_bool(p, "admin", False):
            badges.append("管理员")
        elif json_bool(p, "moderator", False):
            badges.append("版主")
        if pnum > 0:
            badges.append(f"#{pnum}")

        body = parse_cooked_to_comment_text(json_str(p, "cooked", ""), base_url)
        if not body:
            raw = json_str(p, "raw", "").strip()
            body = raw or "[无文本内容]"
        if reply_to >= 2 and isinstance(p.get("reply_to_user"), Mapping):
            ruser = json_str(p["reply_to_user"], "username", "").strip()
            if ruser and not body.startswith(f"@{ruser}"):
                body = f"回复 @{ruser} (#{reply_to}):\n{body}"

        node = CommentNode(
            id=f"post-{pid}" if pid > 0 else f"floor-{pnum}",
            author=_format_display_author(username, name),
            role_badge=" · ".join(badges),
            score=_extract_like_count(p),
            created_at=format_timestamp(json_str(p, "created_at", "")),
            body=body,
        )
        idx = len(items)
        if pnum > 0:
            post_num_to_idx[pnum] = idx
        items.append({"node": node, "pnum": pnum, "reply_to": reply_to, "children": []})

    roots: list[int] = []
    for idx, it in enumerate(items):
        parent_pnum = it["reply_to"]
        if 2 <= parent_pnum < it["pnum"] and parent_pnum in post_num_to_idx:
            pidx = post_num_to_idx[parent_pnum]
            if pidx != idx:
                items[pidx]["children"].append(idx)
                continue
        roots.append(idx)

    visited = [False] * len(items)

    def _materialize(idx: int) -> CommentNode:
        visited[idx] = True
        cur_node: CommentNode = items[idx]["node"]
        child_nodes = [_materialize(cidx) for cidx in items[idx]["children"] if not visited[cidx]]
        return cur_node.model_copy(update={"replies": child_nodes})

    out: list[CommentNode] = []
    for ridx in roots:
        if not visited[ridx]:
            out.append(_materialize(ridx))
    for i in range(len(items)):
        if not visited[i]:
            out.append(_materialize(i))
    return out


class DiscourseClient:
    """Discourse JSON API client with Cloudflare fallback (`web.fetch.v1` -> `web.extract.v1`)."""

    def __init__(
        self,
        *,
        http_client: HttpClientProtocol | None = None,
        auth: AuthManager,
        subs: SubscriptionStore,
        ctx: PluginContext | None = None,
        allow_loopback_http: bool = False,
        allowed_loopback_origins: set[tuple[str, str, int]] | None = None,
    ) -> None:
        self._http: HttpClientProtocol = http_client or SafeHttpsClient()
        self._auth = auth
        self._subs = subs
        self._ctx = ctx
        self._allow_loopback_http = allow_loopback_http
        self._allowed_loopback_origins = set(allowed_loopback_origins or ())
        self._base_url = DEFAULT_BASE_URL
        self._timeout_sec = 15.0
        self._prefer_chrome_fetch = True
        self._lock = threading.RLock()
        self._last_transport = "urllib"
        self._fallback_count = 0

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    def configure(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_sec: float = 15.0,
        prefer_chrome_fetch: bool = True,
    ) -> None:
        clean = (base_url or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
        validated = validate_https_url(
            clean,
            allow_loopback_http=self._allow_loopback_http,
            allowed_loopback_origins=self._allowed_loopback_origins,
            field_name="base_url",
        )
        with self._lock:
            self._base_url = validated.rstrip("/")
            if self._allow_loopback_http:
                self._allowed_loopback_origins.add(url_origin(self._base_url))
            self._timeout_sec = max(0.05, min(float(timeout_sec), 120.0))
            self._prefer_chrome_fetch = bool(prefer_chrome_fetch)

    @property
    def base_url(self) -> str:
        with self._lock:
            return self._base_url

    @property
    def last_transport(self) -> str:
        with self._lock:
            return self._last_transport

    @property
    def fallback_count(self) -> int:
        with self._lock:
            return self._fallback_count

    def _record_transport(self, transport: str, *, is_fallback: bool) -> None:
        with self._lock:
            self._last_transport = transport
            if is_fallback:
                self._fallback_count += 1

    async def _try_chrome_fetch_json(self, url: str) -> tuple[dict[str, Any] | None, FetchStatus]:
        st = FetchStatus()
        ctx = self._ctx
        if ctx is None or not ctx.capabilities.has("web.fetch.v1"):
            return None, st
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() != "https" and not (
            self._allow_loopback_http and url_origin(url) in self._allowed_loopback_origins
        ):
            return None, st

        res = await ctx.capabilities.call(
            "web.fetch.v1",
            {
                "url": url,
                "method": "GET",
                "headers": {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
                "body": "",
                "credentials": "include",
                "timeout_ms": min(int(self._timeout_sec * 1000), 12000),
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            return None, st

        code = json_int(res.data, "status", 200)
        eff_url = json_str(res.data, "url", url) or url
        body = json_str(res.data, "body", "")
        if not body and isinstance(res.data.get("json"), Mapping):
            body = json.dumps(res.data["json"], ensure_ascii=False)
        st.http_status = code
        st.effective_url = eff_url

        if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
            return None, st
        if detect_challenge(code, {}, body):
            st.challenge_detected = True
            return None, st
        if code == 401 or detect_login_wall(code, eff_url, {}, body):
            st.auth_required = True
            return None, st
        if code < 200 or code >= 300 or not body:
            return None, st

        try:
            doc = json.loads(body)
        except json.JSONDecodeError:
            return None, st
        if not isinstance(doc, dict):
            return None, st
        if doc.get("error_type") in {"not_logged_in", "invalid_access"}:
            st.auth_required = True
            return None, st

        st.ok = True
        st.fallback_used = True
        st.transport = "web.fetch.v1"
        self._record_transport("web.fetch.v1", is_fallback=True)
        return doc, st

    async def _try_chrome_extract_json(self, url: str) -> tuple[dict[str, Any] | None, FetchStatus]:
        st = FetchStatus()
        ctx = self._ctx
        if ctx is None or not ctx.capabilities.has("web.extract.v1"):
            return None, st

        candidates = [url]
        html_url = _strip_json_endpoint_extension(url)
        if html_url and html_url != url:
            candidates.append(html_url)

        for target_url in candidates:
            res = await ctx.capabilities.call(
                "web.extract.v1",
                {
                    "url": target_url,
                    "mode": "auto",
                    "use_cookies": True,
                    "render_js": True,
                    "timeout_ms": min(int(self._timeout_sec * 1000), 10000),
                },
            )
            if not res.ok or not isinstance(res.data, Mapping):
                break
            code = json_int(res.data, "status", 200)
            eff_url = json_str(res.data, "url", target_url) or target_url
            probe = json_str(res.data, "html", "") or json_str(res.data, "text", "")

            if detect_challenge(code, {}, probe):
                st.challenge_detected = True
                break
            if code == 401 or detect_login_wall(code, eff_url, {}, probe):
                st.auth_required = True
                break
            if code >= 400:
                continue

            extracted = parse_extract_dom_payload(res.data, url)
            if isinstance(extracted, dict):
                if extracted.get("error_type") in {"not_logged_in", "invalid_access"}:
                    st.auth_required = True
                    break
                st.ok = True
                st.http_status = code
                st.effective_url = eff_url
                st.fallback_used = True
                st.transport = "web.extract.v1"
                self._record_transport("web.extract.v1", is_fallback=True)
                return extracted, st
        return None, st

    async def _try_chrome_fallback_chain(self, url: str) -> tuple[dict[str, Any] | None, FetchStatus]:
        doc, st_fetch = await self._try_chrome_fetch_json(url)
        if doc is not None:
            await self._auth.resolve_cookies(persist_chrome_snapshot=True)
            return doc, st_fetch
        doc, st_ext = await self._try_chrome_extract_json(url)
        if doc is not None:
            await self._auth.resolve_cookies(persist_chrome_snapshot=True)
            return doc, st_ext
        return None, FetchStatus(
            challenge_detected=st_fetch.challenge_detected or st_ext.challenge_detected,
            auth_required=st_fetch.auth_required or st_ext.auth_required,
        )

    async def perform_get_json(
        self, path_or_url: str, auth_res: AuthResolution
    ) -> tuple[dict[str, Any] | None, FetchStatus]:
        """Execute Discourse GET JSON request with automatic Chrome proxy fallback and fail-closed errors."""
        base = self.base_url
        url = (
            path_or_url
            if path_or_url.startswith(("https://", "http://"))
            else f"{base}/{path_or_url.lstrip('/')}"
        )
        validate_https_url(
            url,
            allow_loopback_http=self._allow_loopback_http,
            allowed_loopback_origins=self._allowed_loopback_origins,
        )
        secrets = self._auth.secrets_for_redaction()
        res = await self._http.request(
            HttpRequest(
                url=url,
                method="GET",
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
                cookie_header=auth_res.cookie_header,
                timeout_sec=self._timeout_sec,
            ),
            secrets=secrets,
            allow_loopback_http=self._allow_loopback_http,
            allowed_loopback_origins=self._allowed_loopback_origins,
        )
        status = FetchStatus(
            http_status=res.status_code,
            effective_url=res.effective_url or url,
            challenge_detected=res.challenge_detected,
            transport=res.transport,
        )

        should_try_chrome = (
            res.status_code in {403, 429, 503}
            or res.challenge_detected
            or (bool(res.error) and self._prefer_chrome_fetch)
        )
        if should_try_chrome:
            fb_doc, fb_st = await self._try_chrome_fallback_chain(url)
            if fb_doc is not None:
                return fb_doc, fb_st
            status.challenge_detected = status.challenge_detected or fb_st.challenge_detected
            status.auth_required = status.auth_required or fb_st.auth_required

        if res.error:
            status.error_code = res.error_code or "service_offline"
            status.error = redact_text(res.error, secrets)
            return None, status

        if res.challenge_detected:
            status.auth_required = True
            status.error_code = "not_configured"
            status.error = (
                f"Linux.do 触发 Cloudflare 验证拦截 (HTTP {res.status_code})，"
                "请通过 web.embed.v1 在 Chrome 完成人机验证"
            )
            return None, status

        if res.sso_redirect_detected or res.status_code in {401, 403}:
            status.auth_required = True
            status.session_expired = auth_res.has_t_cookie or auth_res.has_session_cookie
            status.error_code = "not_configured"
            if res.status_code == 403 and not res.sso_redirect_detected:
                status.challenge_detected = True
                status.error = "Linux.do 返回 HTTP 403（Cloudflare TLS 指纹拦截或会话失效），请通过 Chrome 验证并同步 _t / cf_clearance"
            else:
                status.error = (
                    "Linux.do 会话已过期，请通过 Chrome 重新登录并同步 _t"
                    if status.session_expired
                    else "访问该 Linux.do 内容需要登录账号"
                )
            if status.session_expired and res.sso_redirect_detected:
                self._auth.mark_session_expired(status.error)
            return None, status

        if res.status_code == 429:
            status.error_code = "rate_limited"
            status.error = f"Linux.do 请求触发限流 (HTTP 429)"
            return None, status
        if res.status_code in {404, 410}:
            status.error_code = "not_found"
            status.error = f"请求的 Linux.do 资源不存在 (HTTP {res.status_code})"
            return None, status
        if res.status_code < 200 or res.status_code >= 300:
            status.error_code = "service_offline"
            status.error = f"HTTP {res.status_code} ({url})"
            return None, status

        try:
            doc = json.loads(res.body)
        except json.JSONDecodeError:
            doc = None

        if not isinstance(doc, dict):
            if self._prefer_chrome_fetch:
                fb_doc, fb_st = await self._try_chrome_fallback_chain(url)
                if fb_doc is not None:
                    return fb_doc, fb_st
            status.error_code = "invalid_response"
            status.error = "Linux.do 返回非 JSON 响应或响应格式无效"
            return None, status

        et = json_str(doc, "error_type", "")
        if et in {"not_logged_in", "invalid_access"}:
            status.auth_required = True
            status.session_expired = auth_res.has_t_cookie or auth_res.has_session_cookie
            status.error_code = "not_configured"
            status.error = f"需要登录 Linux.do (Discourse: {et})"
            if status.session_expired:
                self._auth.mark_session_expired(status.error)
            return None, status

        status.ok = True
        self._record_transport(res.transport, is_fallback=False)
        if auth_res.has_t_cookie:
            self._auth.mark_authenticated()
        return doc, status

    async def fetch_categories(self) -> tuple[list[DiscourseCategory], FetchStatus]:
        auth_res = await self._auth.resolve_cookies(persist_chrome_snapshot=True)
        now = time.time()
        doc, status = await self.perform_get_json("/categories.json?include_subcategories=true", auth_res)
        if not status.ok or doc is None:
            self._subs.mark_refresh_failed(now)
            return [], status

        out: list[DiscourseCategory] = []
        cat_list = doc.get("category_list")
        if isinstance(cat_list, Mapping):
            _collect_categories_recursive(cat_list.get("categories"), 0, out)
        if out:
            self._subs.update_discovered(out, now)
        else:
            self._subs.mark_refresh_failed(now)
        return out, status

    def _parse_topic_list_json(self, doc: Mapping[str, Any]) -> list[TextFeedItem]:
        users_by_id: dict[int, str] = {}
        raw_users = doc.get("users")
        if isinstance(raw_users, list):
            for u in raw_users:
                if isinstance(u, Mapping):
                    uid = json_int(u, "id", 0)
                    if uid > 0:
                        users_by_id[uid] = _format_display_author(
                            json_str(u, "username", ""), json_str(u, "name", "")
                        )

        tl = doc.get("topic_list")
        if not isinstance(tl, Mapping) or not isinstance(tl.get("topics"), list):
            return []

        base = self.base_url
        items: list[TextFeedItem] = []
        for t in tl["topics"]:
            if not isinstance(t, Mapping):
                continue
            tid = json_int(t, "id", 0)
            if tid <= 0:
                continue
            raw_title = json_str(t, "title", "").strip()
            title = (
                decode_html_entities(raw_title)
                if raw_title
                else strip_html_and_decode(json_str(t, "fancy_title", ""))
            )
            title = title or f"Linux.do 话题 #{tid}"
            slug = json_str(t, "slug", "").strip()
            url = f"{base}/t/{slug}/{tid}" if slug else f"{base}/t/topic/{tid}"

            author = ""
            posters = t.get("posters")
            if isinstance(posters, list) and posters:
                op_uid = 0
                for p in posters:
                    if not isinstance(p, Mapping):
                        continue
                    desc = json_str(p, "description", "")
                    if "Original Poster" in desc or "原始发帖人" in desc:
                        op_uid = json_int(p, "user_id", 0)
                        break
                if op_uid == 0 and isinstance(posters[0], Mapping):
                    op_uid = json_int(posters[0], "user_id", 0)
                author = users_by_id.get(op_uid, "")

            posts_count = json_int(t, "posts_count", 1)
            comment_count = max(0, posts_count - 1)
            cat_id = json_int(t, "category_id", 0)
            cat_name = self._subs.category_name_by_id(cat_id)
            tags: list[str] = [cat_name] if cat_name else []
            if isinstance(t.get("tags"), list):
                for tg in t["tags"]:
                    if isinstance(tg, str) and tg.strip():
                        tags.append(tg.strip())
                    elif isinstance(tg, Mapping) and (tname := json_str(tg, "name", "").strip()):
                        tags.append(tname)

            excerpt = strip_html_and_decode(json_str(t, "excerpt", ""))
            if not excerpt:
                prefix = f"[{cat_name}] " if cat_name else ""
                auth_part = f"{author} · " if author else ""
                excerpt = f"{prefix}{auth_part}{comment_count} 回复 · {json_int(t, 'views', 0)} 浏览"

            items.append(
                TextFeedItem(
                    id=f"topic:{tid}",
                    source_id="plugin-linuxdo",
                    source_name="Linux.do",
                    category="linuxdo",
                    title=title,
                    excerpt=excerpt,
                    author=author,
                    published_at=format_timestamp(json_str_chain(t, ("last_posted_at", "created_at"))),
                    url=url,
                    tags=tags,
                    comment_count=comment_count,
                    score=json_int(t, "like_count", 0),
                )
            )
        return items

    def _parse_search_json(self, doc: Mapping[str, Any]) -> list[TextFeedItem]:
        post_by_topic: dict[int, Mapping[str, Any]] = {}
        if isinstance(doc.get("posts"), list):
            for p in doc["posts"]:
                if isinstance(p, Mapping):
                    tid = json_int(p, "topic_id", 0)
                    if tid > 0 and tid not in post_by_topic:
                        post_by_topic[tid] = p

        if not isinstance(doc.get("topics"), list):
            return []

        base = self.base_url
        items: list[TextFeedItem] = []
        for t in doc["topics"]:
            if not isinstance(t, Mapping):
                continue
            tid = json_int(t, "id", 0)
            if tid <= 0:
                continue
            title = decode_html_entities(json_str(t, "title", "").strip()) or strip_html_and_decode(
                json_str(t, "fancy_title", "")
            )
            title = title or f"Linux.do 话题 #{tid}"
            slug = json_str(t, "slug", "").strip()
            url = f"{base}/t/{slug}/{tid}" if slug else f"{base}/t/topic/{tid}"
            posts_count = json_int(t, "posts_count", 1)
            comment_count = max(0, posts_count - 1)
            cat_id = json_int(t, "category_id", 0)
            cat_name = self._subs.category_name_by_id(cat_id)
            tags: list[str] = [cat_name] if cat_name else []
            if isinstance(t.get("tags"), list):
                for tg in t["tags"]:
                    if isinstance(tg, str) and tg.strip():
                        tags.append(tg.strip())
                    elif isinstance(tg, Mapping) and (tname := json_str(tg, "name", "").strip()):
                        tags.append(tname)

            author = ""
            score = 0
            excerpt = ""
            if tid in post_by_topic:
                p = post_by_topic[tid]
                author = _format_display_author(json_str(p, "username", ""), json_str(p, "name", ""))
                score = json_int(p, "like_count", 0)
                excerpt = strip_html_and_decode(json_str(p, "blurb", ""))
            if not excerpt:
                prefix = f"[{cat_name}] " if cat_name else ""
                excerpt = f"{prefix}{comment_count} 回复"

            items.append(
                TextFeedItem(
                    id=f"topic:{tid}",
                    source_id="plugin-linuxdo",
                    source_name="Linux.do",
                    category="linuxdo",
                    title=title,
                    excerpt=excerpt,
                    author=author,
                    published_at=format_timestamp(json_str_chain(t, ("last_posted_at", "created_at"))),
                    url=url,
                    tags=tags,
                    comment_count=comment_count,
                    score=score,
                )
            )
        return items

    async def _refresh_categories_if_stale(self, ch: str) -> None:
        if ch != "unread" and self._subs.needs_category_refresh(time.time()):
            await self.fetch_categories()

    async def fetch_topic_list(
        self,
        *,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
    ) -> tuple[list[TextFeedItem], FetchStatus]:
        auth_res = await self._auth.resolve_cookies(persist_chrome_snapshot=True)
        q = (query or "").strip()
        ch = (channel_id or "").strip()
        safe_page = max(1, page)

        if q and ("linux.do/t/" in q or f"{urllib.parse.urlsplit(self.base_url).hostname or ''}/t/" in q):
            if safe_page > 1:
                return [], FetchStatus(ok=True, transport=self.last_transport)
            direct_tid = extract_topic_id(q)
            if direct_tid > 0:
                return [
                    TextFeedItem(
                        id=f"topic:{direct_tid}",
                        source_id="plugin-linuxdo",
                        source_name="Linux.do",
                        category="linuxdo",
                        title=f"打开 Linux.do 帖子 #{direct_tid}",
                        excerpt="点击加载话题正文、代码块与嵌套评论树",
                        url=f"{self.base_url}/t/topic/{direct_tid}",
                    )
                ], FetchStatus(ok=True, transport=self.last_transport)

        if ch.startswith("cat:"):
            cat = self._subs.resolve_category(ch)
            if cat is not None and cat.id <= 0:
                await self._refresh_categories_if_stale(ch)

        if q:
            effective_q = q
            filter_cat_id = 0
            if ch.startswith("cat:"):
                cat = self._subs.resolve_category(ch)
                if cat is not None:
                    if cat.slug:
                        effective_q = f"{q} #{cat.slug}"
                    elif cat.id > 0:
                        filter_cat_id = cat.id
            path = f"/search.json?q={urllib.parse.quote(effective_q, safe='')}"
            if safe_page > 1:
                path += f"&page={safe_page}"
            doc, status = await self.perform_get_json(path, auth_res)
            if not status.ok or doc is None:
                return [], status
            await self._refresh_categories_if_stale(ch)
            items = self._parse_search_json(doc)
            if filter_cat_id > 0 and isinstance(doc.get("topics"), list):
                allowed = {
                    json_int(t, "id", 0)
                    for t in doc["topics"]
                    if isinstance(t, Mapping) and json_int(t, "category_id", 0) == filter_cat_id
                }
                items = [it for it in items if extract_topic_id(it.id) in allowed]
            return items, status

        if ch == "pinned":
            targets = self._subs.list_pinned()
            if not targets:
                await self._refresh_categories_if_stale(ch)
                disc = sorted(self._subs.list_discovered(), key=lambda c: c.topics_week, reverse=True)
                targets = disc[:2]
            if targets:
                merged: list[TextFeedItem] = []
                seen_ids: set[str] = set()
                last_st = FetchStatus(ok=True, transport=self.last_transport)
                for idx, c in enumerate(targets[:3]):
                    if c.id > 0 and c.slug:
                        path = f"/c/{urllib.parse.quote(c.slug, safe='')}/{c.id}.json"
                    elif c.id > 0:
                        path = f"/c/{c.id}.json"
                    else:
                        path = f"/c/{urllib.parse.quote(c.slug, safe='')}.json"
                    if safe_page > 1:
                        path += f"?page={safe_page - 1}"
                    doc, sub_st = await self.perform_get_json(path, auth_res)
                    if sub_st.ok and doc is not None:
                        last_st = sub_st
                        await self._refresh_categories_if_stale(ch)
                        for it in self._parse_topic_list_json(doc):
                            if it.id not in seen_ids:
                                seen_ids.add(it.id)
                                merged.append(it)
                    elif idx == 0:
                        last_st = sub_st
                return merged, last_st

        endpoint = "/latest.json"
        if ch == "top":
            endpoint = "/top.json?period=weekly"
        elif ch == "unread":
            endpoint = "/unread.json"
        elif ch.startswith("cat:"):
            cat = self._subs.resolve_category(ch)
            if cat is not None:
                if cat.id > 0 and cat.slug:
                    endpoint = f"/c/{urllib.parse.quote(cat.slug, safe='')}/{cat.id}.json"
                elif cat.id > 0:
                    endpoint = f"/c/{cat.id}.json"
                elif cat.slug:
                    endpoint = f"/c/{urllib.parse.quote(cat.slug, safe='')}.json"
        if safe_page > 1:
            sep = "&" if "?" in endpoint else "?"
            endpoint += f"{sep}page={safe_page - 1}"

        doc, status = await self.perform_get_json(endpoint, auth_res)
        if not status.ok or doc is None:
            return [], status
        await self._refresh_categories_if_stale(ch)
        return self._parse_topic_list_json(doc), status

    async def _fetch_posts_by_ids(
        self,
        topic_id: int,
        post_ids: Sequence[int],
        auth_res: AuthResolution,
    ) -> tuple[list[Mapping[str, Any]], FetchStatus]:
        out: list[Mapping[str, Any]] = []
        last_st = FetchStatus(ok=True, transport=self.last_transport)
        if topic_id <= 0 or not post_ids:
            return out, last_st

        batch_size = 20
        for i in range(0, len(post_ids), batch_size):
            batch = post_ids[i : i + batch_size]
            qs = "&".join(f"post_ids[]={pid}" for pid in batch)
            doc, batch_st = await self.perform_get_json(f"/t/{topic_id}/posts.json?{qs}", auth_res)
            last_st = batch_st
            if not batch_st.ok or doc is None:
                return out, batch_st
            ps = doc.get("post_stream")
            if isinstance(ps, Mapping) and isinstance(ps.get("posts"), list):
                out.extend(p for p in ps["posts"] if isinstance(p, Mapping))
        return out, last_st

    async def fetch_topic_article(
        self,
        article_id: str,
        *,
        max_initial_posts: int = 60,
    ) -> tuple[TextArticle | None, FetchStatus]:
        tid = extract_topic_id(article_id)
        if tid <= 0:
            return None, FetchStatus(error_code="invalid_argument", error=f"无效的话题 ID: {article_id}")

        auth_res = await self._auth.resolve_cookies(persist_chrome_snapshot=True)
        doc, status = await self.perform_get_json(f"/t/{tid}.json?track_visit=false", auth_res)
        if not status.ok or doc is None:
            return None, status

        raw_title = json_str(doc, "title", "").strip()
        title = (
            decode_html_entities(raw_title)
            if raw_title
            else strip_html_and_decode(json_str(doc, "fancy_title", ""))
        ) or f"Linux.do 话题 #{tid}"
        slug = json_str(doc, "slug", "").strip()
        url = f"{self.base_url}/t/{slug}/{tid}" if slug else f"{self.base_url}/t/topic/{tid}"
        published_at = format_timestamp(json_str(doc, "created_at", ""))

        op_username = ""
        op_name = ""
        details = doc.get("details")
        if isinstance(details, Mapping) and isinstance(details.get("created_by"), Mapping):
            op_username = json_str(details["created_by"], "username", "").strip()
            op_name = json_str(details["created_by"], "name", "").strip()

        cat_id = json_int(doc, "category_id", 0)
        cat_name = self._subs.category_name_by_id(cat_id)
        tags: list[str] = [cat_name] if cat_name else []
        if isinstance(doc.get("tags"), list):
            for tg in doc["tags"]:
                if isinstance(tg, str) and tg.strip():
                    tags.append(tg.strip())
                elif isinstance(tg, Mapping) and (tname := json_str(tg, "name", "").strip()):
                    tags.append(tname)

        all_posts: list[Mapping[str, Any]] = []
        loaded_ids: set[int] = set()
        stream_ids: list[int] = []
        ps = doc.get("post_stream")
        if isinstance(ps, Mapping):
            if isinstance(ps.get("posts"), list):
                for p in ps["posts"]:
                    if isinstance(p, Mapping):
                        all_posts.append(p)
                        pid = json_int(p, "id", 0)
                        if pid > 0:
                            loaded_ids.add(pid)
            if isinstance(ps.get("stream"), list):
                for sid in ps["stream"]:
                    if isinstance(sid, int) and not isinstance(sid, bool) and sid > 0:
                        stream_ids.append(sid)

        if not op_username:
            for p in all_posts:
                if json_int(p, "post_number", 0) == 1:
                    op_username = json_str(p, "username", "").strip()
                    op_name = json_str(p, "name", "").strip()
                    break

        authors = _format_display_author(op_username, op_name)
        posts_count = json_int(doc, "posts_count", len(all_posts))
        sub_parts = [authors]
        if cat_name:
            sub_parts.append(cat_name)
        if published_at:
            sub_parts.append(published_at)
        sub_parts.append(f"{posts_count} 楼")

        if max_initial_posts > len(all_posts) and len(stream_ids) > len(all_posts):
            extra_ids: list[int] = []
            for pid in stream_ids:
                if pid in loaded_ids:
                    continue
                extra_ids.append(pid)
                if len(all_posts) + len(extra_ids) >= max_initial_posts:
                    break
            if extra_ids:
                extra_posts, _ = await self._fetch_posts_by_ids(tid, extra_ids, auth_res)
                all_posts.extend(extra_posts)

        paragraphs: list[TextParagraph] = []
        for p in all_posts:
            if json_int(p, "post_number", 0) == 1:
                cooked = json_str(p, "cooked", "")
                paragraphs = parse_cooked_to_paragraphs(cooked, self.base_url)
                if not paragraphs:
                    fallback = strip_html_and_decode(cooked)
                    if fallback:
                        paragraphs = [TextParagraph(id="p1", kind="paragraph", original=fallback)]
                break

        comments = build_comment_tree(all_posts, op_username=op_username, base_url=self.base_url)
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
            source_id="plugin-linuxdo",
            source_name="Linux.do",
            category="linuxdo",
            title=title,
            subtitle=" · ".join(sub_parts),
            authors=authors,
            published_at=published_at,
            url=url,
            tags=tags,
            paragraphs=paragraphs,
            comments=comments,
            extra={
                "topic_id": tid,
                "total_posts": len(stream_ids) or posts_count,
                "loaded_posts": len(all_posts),
            },
        )
        return article, status

    async def fetch_topic_comments_page(
        self,
        article_id: str,
        *,
        offset: int = 0,
        limit: int = 40,
    ) -> tuple[CommentsPageResult, FetchStatus]:
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, 100))
        res = CommentsPageResult(offset=safe_offset, next_offset=safe_offset)

        tid = extract_topic_id(article_id)
        if tid <= 0:
            return res, FetchStatus(error_code="invalid_argument", error=f"无效的话题 ID: {article_id}")

        auth_res = await self._auth.resolve_cookies(persist_chrome_snapshot=True)
        doc, status = await self.perform_get_json(f"/t/{tid}.json?track_visit=false", auth_res)
        if not status.ok or doc is None:
            return res, status
        res.transport = status.transport

        op_username = ""
        details = doc.get("details")
        if isinstance(details, Mapping) and isinstance(details.get("created_by"), Mapping):
            op_username = json_str(details["created_by"], "username", "").strip()

        loaded_by_id: dict[int, Mapping[str, Any]] = {}
        stream_ids: list[int] = []
        ps = doc.get("post_stream")
        if isinstance(ps, Mapping):
            if isinstance(ps.get("posts"), list):
                for p in ps["posts"]:
                    if isinstance(p, Mapping):
                        pid = json_int(p, "id", 0)
                        if pid > 0:
                            loaded_by_id[pid] = p
                        if json_int(p, "post_number", 0) == 1 and not op_username:
                            op_username = json_str(p, "username", "").strip()
            if isinstance(ps.get("stream"), list):
                for sid in ps["stream"]:
                    if isinstance(sid, int) and not isinstance(sid, bool) and sid > 0:
                        stream_ids.append(sid)

        reply_stream_ids = stream_ids[1:] if len(stream_ids) > 1 else []
        res.total_posts = len(reply_stream_ids)
        if safe_offset >= len(reply_stream_ids):
            return res, status

        end = min(len(reply_stream_ids), safe_offset + safe_limit)
        target_ids = reply_stream_ids[safe_offset:end]
        missing_ids = [pid for pid in target_ids if pid not in loaded_by_id]
        if missing_ids:
            fetched, batch_st = await self._fetch_posts_by_ids(tid, missing_ids, auth_res)
            if not batch_st.ok and not fetched:
                return res, batch_st
            for fp in fetched:
                pid = json_int(fp, "id", 0)
                if pid > 0:
                    loaded_by_id[pid] = fp

        page_posts = [loaded_by_id[pid] for pid in target_ids if pid in loaded_by_id]
        res.comments = build_comment_tree(page_posts, op_username=op_username, base_url=self.base_url)
        res.loaded_count = len(page_posts)
        res.next_offset = end
        res.has_more = end < len(reply_stream_ids)
        return res, status
