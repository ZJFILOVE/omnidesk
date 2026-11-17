"""Domain 2 TextSource implementation for Zhihu hot list, recommend, follow, collections, articles, and comments."""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from omnidesk_sdk import PluginContext, TextArticle, TextChannel, TextFeedItem, TextSource

if __package__:
    from .zhihu_auth import AuthManager
    from .zhihu_errors import ZhihuError, redact_zhihu_secrets, url_origin, validate_segment_id
    from .zhihu_http import ZhihuHttpClient, ZhihuHttpResponse
    from .zhihu_parser import (
        CollectionSummary,
        CommentPageResult,
        ParsedZhihuTarget,
        extract_initial_data,
        parse_answer_api_json,
        parse_article_from_initial_data,
        parse_article_id_or_url,
        parse_collection_items_json,
        parse_collections_list_json,
        parse_comments_json,
        parse_fallback_html_dom,
        parse_hot_list_initial_data,
        parse_hot_list_json,
        parse_question_with_answers_api_json,
        parse_search_json,
        parse_topstory_feed_json,
        parse_topstory_initial_data,
        parse_zhuanlan_api_json,
    )
    from .zhihu_translate import translate_article_if_enabled, translate_comments_if_enabled
else:
    from zhihu_auth import AuthManager  # type: ignore[no-redef]
    from zhihu_errors import ZhihuError, redact_zhihu_secrets, url_origin, validate_segment_id  # type: ignore[no-redef]
    from zhihu_http import ZhihuHttpClient, ZhihuHttpResponse  # type: ignore[no-redef]
    from zhihu_parser import (  # type: ignore[no-redef]
        CollectionSummary,
        CommentPageResult,
        ParsedZhihuTarget,
        extract_initial_data,
        parse_answer_api_json,
        parse_article_from_initial_data,
        parse_article_id_or_url,
        parse_collection_items_json,
        parse_collections_list_json,
        parse_comments_json,
        parse_fallback_html_dom,
        parse_hot_list_initial_data,
        parse_hot_list_json,
        parse_question_with_answers_api_json,
        parse_search_json,
        parse_topstory_feed_json,
        parse_topstory_initial_data,
        parse_zhuanlan_api_json,
    )
    from zhihu_translate import translate_article_if_enabled, translate_comments_if_enabled  # type: ignore[no-redef]

DEFAULT_WEB_BASE_URL = "https://www.zhihu.com"
DEFAULT_ZHUANLAN_BASE_URL = "https://zhuanlan.zhihu.com"
_BUILTIN_CHANNELS = (
    ("hot", "热榜", False),
    ("recommend", "推荐", True),
    ("follow", "关注", True),
    ("collections", "收藏夹", True),
)


class ZhihuTextSource(TextSource):
    """OmniDesk Domain 2 TextSource for Zhihu answers, columns, feeds, and comment trees."""

    source_id: str = "plugin-zhihu"
    display_name: str = "知乎"
    category: str = "zhihu"

    def __init__(
        self,
        auth: AuthManager,
        http: ZhihuHttpClient,
        *,
        web_base_url: str = DEFAULT_WEB_BASE_URL,
        zhuanlan_base_url: str = DEFAULT_ZHUANLAN_BASE_URL,
        allow_loopback_http: bool = False,
    ) -> None:
        self._auth = auth
        self._http = http
        self._ctx: PluginContext | None = None
        self._web_base_url = web_base_url.rstrip("/") or DEFAULT_WEB_BASE_URL
        self._zhuanlan_base_url = zhuanlan_base_url.rstrip("/") or DEFAULT_ZHUANLAN_BASE_URL
        self._allow_loopback_http = bool(allow_loopback_http)
        self.last_error: dict[str, Any] | None = None

    def set_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    def _allowed_origins(self) -> set[str]:
        origins: set[str] = set()
        for base in (self._web_base_url, self._zhuanlan_base_url):
            if base.startswith("http://"):
                origins.add(url_origin(base))
        return origins

    def _web_url(self, path_and_query: str) -> str:
        rel = path_and_query if path_and_query.startswith("/") else f"/{path_and_query}"
        return f"{self._web_base_url}{rel}"

    def _article_fetch_url(self, target: ParsedZhihuTarget) -> str:
        raw = target.web_url
        if not raw or not self._allow_loopback_http:
            return raw
        parsed = urllib.parse.urlsplit(raw)
        if parsed.hostname == "zhuanlan.zhihu.com" and self._zhuanlan_base_url.startswith("http://"):
            return f"{self._zhuanlan_base_url}{parsed.path}"
        if parsed.hostname in {"www.zhihu.com", "zhihu.com"} and self._web_base_url.startswith("http://"):
            return f"{self._web_base_url}{parsed.path}"
        return raw

    def _error_from_response(self, resp: ZhihuHttpResponse, fallback_msg: str) -> dict[str, Any]:
        secrets = self._auth.secrets_for_redaction()
        return {
            "error_code": resp.error_code or "service_offline",
            "error": redact_zhihu_secrets(resp.error or fallback_msg, secrets),
            "reason": resp.reason or resp.error_code or "service_offline",
            "http_status": resp.status_code,
            "transport": resp.transport,
            "fallback_used": resp.fallback_used,
            "auth_required": resp.auth_required,
            "challenge_detected": resp.challenge_detected,
            "cloudflare_blocked": resp.cloudflare_blocked,
        }

    def channels(self) -> list[TextChannel]:
        """Return Zhihu channels ordered with any configured `pinned_channels` first."""
        ar = self._auth.resolve_for_url_sync(self._web_url("/"))
        cfg = self._auth.config()
        is_authed = ar.is_authenticated
        auth_badge = "" if is_authed else "需登录"

        defaults = {
            cid: TextChannel(
                id=cid,
                name=name,
                badge=auth_badge if req_auth else "",
                requires_auth=req_auth,
            )
            for cid, name, req_auth in _BUILTIN_CHANNELS
        }

        pinned_tokens = [tok.strip() for tok in cfg.pinned_channels.split(",") if tok.strip()]
        if not pinned_tokens:
            return [defaults[cid] for cid, _, _ in _BUILTIN_CHANNELS]

        ordered: list[TextChannel] = []
        seen: set[str] = set()
        for tok in pinned_tokens:
            if tok.startswith("collection:"):
                rest = tok[11:]
                col_id_raw, _, custom_name = rest.partition(":")
                try:
                    col_id = validate_segment_id(col_id_raw, field="collection_id")
                except ZhihuError:
                    continue
                cid = f"collection:{col_id}"
                if cid in seen:
                    continue
                seen.add(cid)
                ordered.append(
                    TextChannel(
                        id=cid,
                        name=custom_name.strip() or f"收藏夹 #{col_id}",
                        badge="置顶" if is_authed else "需登录",
                        requires_auth=True,
                    )
                )
            elif tok in defaults and tok not in seen:
                seen.add(tok)
                base = defaults[tok]
                badge = "置顶" if (is_authed or not base.requires_auth) else "需登录"
                ordered.append(base.model_copy(update={"badge": badge}))

        for cid, _, _ in _BUILTIN_CHANNELS:
            if cid not in seen:
                ordered.append(defaults[cid])
        return ordered

    @staticmethod
    def _filter_items(items: list[TextFeedItem], query: str) -> list[TextFeedItem]:
        q = (query or "").strip().lower()
        if not q:
            return items
        out: list[TextFeedItem] = []
        for it in items:
            if (
                q in it.title.lower()
                or q in it.excerpt.lower()
                or q in it.author.lower()
                or any(q in tag.lower() for tag in it.tags)
            ):
                out.append(it)
        return out

    async def fetch_feed(
        self,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[TextFeedItem]:
        """Fetch paginated Zhihu feed items; returns `[]` and populates `last_error` on failure."""
        del force_refresh
        self.last_error = None
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            self.last_error = {"error_code": "invalid_argument", "error": "page 必须为 >= 1 的整数"}
            return []

        q = (query or "").strip()
        if q.lower().startswith(("http://", "https://", "//")):
            try:
                parsed = parse_article_id_or_url(
                    q,
                    allow_loopback_http=self._allow_loopback_http,
                    allowed_loopback_origins=self._allowed_origins(),
                )
            except ZhihuError as exc:
                self.last_error = {"error_code": exc.error_code, "error": exc.message}
                return []
            if parsed.kind != "unknown":
                if page > 1:
                    return []
                return [
                    TextFeedItem(
                        id=parsed.canonical_id,
                        source_id=self.source_id,
                        source_name="知乎专栏" if parsed.kind == "article" else "知乎问答",
                        category="zhihu",
                        channel_id=channel_id or "hot",
                        title=f"打开知乎链接: {parsed.web_url}",
                        excerpt="点击立即拉取正文、LaTeX 公式与嵌套评论树",
                        url=parsed.web_url,
                    )
                ]

        cid = (channel_id or "").strip()
        if not cid or cid == "hot":
            return await self._fetch_hot_feed(q, page)
        if cid == "recommend":
            return await self._fetch_recommend_feed(q, page)
        if cid == "follow":
            return await self._fetch_follow_feed(q, page)
        if cid == "collections":
            return await self._fetch_collections_feed("", q, page)
        if cid.startswith("collection:"):
            try:
                col_id = validate_segment_id(cid[11:], field="collection_id")
            except ZhihuError as exc:
                self.last_error = {"error_code": exc.error_code, "error": exc.message}
                return []
            return await self._fetch_collections_feed(col_id, q, page)
        if cid == "search":
            return await self.search_zhihu(q, page)

        self.last_error = {"error_code": "invalid_argument", "error": f"不支持的知乎栏目: {cid!r}"}
        return []

    async def _fetch_hot_feed(self, query: str, page: int) -> list[TextFeedItem]:
        if query:
            search_res = await self.search_zhihu(query, page)
            if search_res or page > 1:
                return search_res
            if self.last_error is not None:
                # Keep hot-list fallback only if search wasn't explicitly blocked? Fail closed on hard transport/auth errors.
                return []

        if page > 1:
            ar = await self._auth.resolve_for_url(self._web_url("/"))
            if ar.is_authenticated:
                return await self._fetch_recommend_feed(query, page - 1)
            return []

        api_resp = await self._http.fetch_with_fallback(
            self._web_url("/api/v3/feed/topstory/hot-lists/total?limit=50&desktop=true"),
            self._auth,
            expect_json=True,
            force_chrome_on_signature=False,
        )
        if api_resp.ok:
            try:
                j = json.loads(api_resp.body)
            except json.JSONDecodeError:
                j = None
            if isinstance(j, dict) and isinstance(j.get("data"), list):
                items = parse_hot_list_json(j)
                if items or not j["data"]:
                    return self._filter_items(items, query)

        if api_resp.error_code == "timeout":
            self.last_error = self._error_from_response(api_resp, "获取知乎热榜超时")
            return []

        html_resp = await self._http.fetch_with_fallback(
            self._web_url("/hot"),
            self._auth,
            expect_json=False,
            force_chrome_on_signature=False,
        )
        if html_resp.ok:
            init_data = extract_initial_data(html_resp.body)
            if init_data is not None:
                items = parse_hot_list_initial_data(init_data)
                return self._filter_items(items, query)
            self.last_error = {
                "error_code": "validation_error",
                "error": "知乎热榜 HTML 缺少有效 js-initialData",
                "reason": "parse_error",
            }
            return []

        self.last_error = self._error_from_response(
            html_resp if not api_resp.ok and html_resp.error_code else api_resp,
            "获取知乎热榜失败",
        )
        return []

    async def _fetch_recommend_feed(self, query: str, page: int) -> list[TextFeedItem]:
        ar = await self._auth.resolve_for_url(self._web_url("/"))
        has_chrome_proxy = self._ctx is not None and (
            self._ctx.capabilities.has("web.fetch.v1") or self._ctx.capabilities.has("web.extract.v1")
        )
        if not ar.is_authenticated and not has_chrome_proxy:
            self.last_error = {
                "error_code": "not_configured",
                "error": "需要登录知乎（z_c0）或启用 Chrome 上下文代理以访问「推荐」栏目",
                "reason": "auth_required",
                "auth_required": True,
            }
            return []

        api_resp = await self._http.fetch_with_fallback(
            self._web_url(
                f"/api/v3/feed/topstory/recommend?action=down&ad_interval=-1&desktop=true&page_number={page}"
            ),
            self._auth,
            expect_json=True,
            force_chrome_on_signature=True,
        )
        if api_resp.ok:
            try:
                j = json.loads(api_resp.body)
            except json.JSONDecodeError:
                j = None
            if isinstance(j, dict) and isinstance(j.get("data"), list):
                items = parse_topstory_feed_json(j, "推荐", channel_id="recommend")
                return self._filter_items(items, query)

        if page <= 1 and api_resp.error_code != "timeout":
            home_resp = await self._http.fetch_with_fallback(
                self._web_url("/"),
                self._auth,
                expect_json=False,
                force_chrome_on_signature=False,
            )
            if home_resp.ok:
                init_data = extract_initial_data(home_resp.body)
                if init_data is not None:
                    items = parse_topstory_initial_data(init_data, "推荐", channel_id="recommend")
                    if items:
                        return self._filter_items(items, query)

        self.last_error = self._error_from_response(api_resp, "获取知乎推荐流失败")
        return []

    async def _fetch_follow_feed(self, query: str, page: int) -> list[TextFeedItem]:
        ar = await self._auth.resolve_for_url(self._web_url("/"))
        if not ar.is_authenticated:
            self.last_error = {
                "error_code": "not_configured",
                "error": "需要登录知乎（z_c0）以访问「关注」栏目",
                "reason": "auth_required",
                "auth_required": True,
            }
            return []

        offset_suffix = f"&offset={(page - 1) * 20}" if page > 1 else ""
        resp = await self._http.fetch_with_fallback(
            self._web_url(f"/api/v3/moments?limit=20&desktop=true{offset_suffix}"),
            self._auth,
            expect_json=True,
            force_chrome_on_signature=True,
        )
        if resp.ok:
            try:
                j = json.loads(resp.body)
            except json.JSONDecodeError:
                j = None
            if isinstance(j, dict) and isinstance(j.get("data"), list):
                items = parse_topstory_feed_json(j, "关注", channel_id="follow")
                return self._filter_items(items, query)

        if resp.error_code == "timeout":
            self.last_error = self._error_from_response(resp, "获取知乎关注动态超时")
            return []

        fallback = await self._http.fetch_with_fallback(
            self._web_url(f"/api/v3/feed/topstory/follow?limit=20&desktop=true{offset_suffix}"),
            self._auth,
            expect_json=True,
            force_chrome_on_signature=True,
        )
        if fallback.ok:
            try:
                j = json.loads(fallback.body)
            except json.JSONDecodeError:
                j = None
            if isinstance(j, dict) and isinstance(j.get("data"), list):
                items = parse_topstory_feed_json(j, "关注", channel_id="follow")
                return self._filter_items(items, query)

        self.last_error = self._error_from_response(
            fallback if fallback.error_code else resp, "获取知乎关注动态失败"
        )
        return []

    async def _fetch_collections_feed(
        self, collection_filter: str, query: str, page: int
    ) -> list[TextFeedItem]:
        ar = await self._auth.resolve_for_url(self._web_url("/"))
        if not ar.is_authenticated:
            self.last_error = {
                "error_code": "not_configured",
                "error": "需要登录知乎（z_c0）以访问「收藏夹」栏目",
                "reason": "auth_required",
                "auth_required": True,
            }
            return []

        cols: list[CollectionSummary] = []
        if collection_filter:
            cols.append(CollectionSummary(id=collection_filter, title=f"收藏夹 #{collection_filter}"))
        else:
            list_resp = await self._http.fetch_with_fallback(
                self._web_url("/api/v4/people/self/collections?limit=20&offset=0"),
                self._auth,
                expect_json=True,
                force_chrome_on_signature=False,
            )
            if list_resp.ok:
                try:
                    j = json.loads(list_resp.body)
                    if isinstance(j, dict) and isinstance(j.get("data"), list):
                        cols = parse_collections_list_json(j)
                        if not j["data"]:
                            return []
                except json.JSONDecodeError:
                    pass

            if not cols:
                me_resp = await self._http.fetch_with_fallback(
                    self._web_url("/api/v4/me?include=url_token"),
                    self._auth,
                    expect_json=True,
                    force_chrome_on_signature=False,
                )
                if me_resp.ok:
                    try:
                        mj = json.loads(me_resp.body)
                    except json.JSONDecodeError:
                        mj = {}
                    token = str(mj.get("url_token") or "").strip() if isinstance(mj, dict) else ""
                    if token:
                        user_cols = await self._http.fetch_with_fallback(
                            self._web_url(
                                f"/api/v4/people/{urllib.parse.quote(token, safe='')}/collections?limit=20&offset=0"
                            ),
                            self._auth,
                            expect_json=True,
                            force_chrome_on_signature=False,
                        )
                        if user_cols.ok:
                            try:
                                cj = json.loads(user_cols.body)
                                if isinstance(cj, dict) and isinstance(cj.get("data"), list):
                                    cols = parse_collections_list_json(cj)
                                    if not cj["data"]:
                                        return []
                            except json.JSONDecodeError:
                                pass
                if not cols:
                    self.last_error = self._error_from_response(
                        list_resp if not list_resp.ok else me_resp, "获取知乎收藏夹列表失败"
                    )
                    return []

        offset = (page - 1) * 20
        items: list[TextFeedItem] = []
        any_ok = False
        last_failed_resp: ZhihuHttpResponse | None = None
        for col in cols[:5]:
            safe_cid = urllib.parse.quote(col.id, safe="")
            items_resp = await self._http.fetch_with_fallback(
                self._web_url(f"/api/v4/collections/{safe_cid}/items?offset={offset}&limit=20"),
                self._auth,
                expect_json=True,
                force_chrome_on_signature=False,
            )
            if items_resp.ok:
                try:
                    j = json.loads(items_resp.body)
                except json.JSONDecodeError:
                    j = None
                if isinstance(j, dict) and isinstance(j.get("data"), list):
                    any_ok = True
                    items.extend(
                        parse_collection_items_json(
                            j, col.title, channel_id=f"collection:{col.id}" if collection_filter else "collections"
                        )
                    )
            else:
                last_failed_resp = items_resp

        if not any_ok and last_failed_resp is not None:
            self.last_error = self._error_from_response(last_failed_resp, "获取知乎收藏夹条目失败")
            return []
        return self._filter_items(items, query)

    async def search_zhihu(self, query: str, page: int = 1) -> list[TextFeedItem]:
        """Search Zhihu content via `/api/v4/search_v3`."""
        self.last_error = None
        q = (query or "").strip()
        if not q:
            return []
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            self.last_error = {"error_code": "invalid_argument", "error": "page 必须为 >= 1 的整数"}
            return []

        offset = (page - 1) * 20
        encoded_q = urllib.parse.quote(q, safe="")
        url = self._web_url(
            f"/api/v4/search_v3?gk_version=gz-gaokao&t=general&q={encoded_q}&correction=1&offset={offset}&limit=20"
        )
        resp = await self._http.fetch_with_fallback(
            url,
            self._auth,
            expect_json=True,
            force_chrome_on_signature=True,
        )
        if not resp.ok:
            self.last_error = self._error_from_response(resp, "知乎搜索失败")
            return []
        try:
            j = json.loads(resp.body)
        except json.JSONDecodeError:
            self.last_error = {
                "error_code": "validation_error",
                "error": "知乎搜索返回无效 JSON",
                "reason": "parse_error",
            }
            return []
        if not isinstance(j, dict) or not isinstance(j.get("data"), list):
            self.last_error = {
                "error_code": "validation_error",
                "error": "知乎搜索 JSON 缺少 data 数组",
                "reason": "parse_error",
            }
            return []
        return parse_search_json(j)

    async def fetch_article(
        self,
        article_id: str,
        *,
        auto_translate: bool | None = None,
        target_lang: str = "zh-CN",
    ) -> TextArticle:
        """Fetch and parse a Zhihu answer, column article, or question fail-closed."""
        self.last_error = None
        if not isinstance(article_id, str) or not article_id.strip():
            raise ZhihuError("invalid_argument", "article_id, id, or url is required")

        target = parse_article_id_or_url(
            article_id,
            allow_loopback_http=self._allow_loopback_http,
            allowed_loopback_origins=self._allowed_origins(),
        )
        if target.kind == "unknown" or not target.web_url:
            raise ZhihuError("invalid_argument", f"无效的知乎文章或问答标识: {article_id!r}")

        article: TextArticle | None = None
        last_failed_resp: ZhihuHttpResponse | None = None
        saw_malformed_html = False

        html_resp = await self._http.fetch_with_fallback(
            self._article_fetch_url(target),
            self._auth,
            expect_json=False,
            force_chrome_on_signature=False,
        )
        if html_resp.ok:
            init_data = extract_initial_data(html_resp.body)
            if init_data is not None:
                article = parse_article_from_initial_data(init_data, target)
            if article is None or not article.paragraphs:
                dom_article = parse_fallback_html_dom(html_resp.body, target)
                if dom_article is not None and dom_article.paragraphs:
                    article = dom_article
                elif article is None:
                    saw_malformed_html = True
        else:
            last_failed_resp = html_resp

        if (article is None or not article.paragraphs) and (last_failed_resp is None or last_failed_resp.error_code != "timeout"):
            api_article, api_failed_resp, api_parse_err = await self._fetch_article_via_api(target)
            if api_article is not None and (api_article.paragraphs or target.kind == "question"):
                article = api_article
            else:
                if api_failed_resp is not None and (last_failed_resp is None or html_resp.ok):
                    last_failed_resp = api_failed_resp
                if api_parse_err:
                    saw_malformed_html = True

        if article is None or (not article.paragraphs and target.kind != "question"):
            if last_failed_resp is not None and not last_failed_resp.ok:
                err_info = self._error_from_response(last_failed_resp, f"加载知乎文章失败: {article_id}")
                self.last_error = err_info
                raise ZhihuError(
                    err_info["error_code"],
                    err_info["error"],
                    status_code=last_failed_resp.status_code or None,
                    details=err_info,
                )
            msg = "知乎页面解析失败：未找到有效正文结构或 js-initialData" if saw_malformed_html else f"未能加载知乎内容: {article_id}"
            self.last_error = {"error_code": "validation_error", "error": msg, "reason": "parse_error"}
            raise ZhihuError("validation_error", msg, details={"reason": "parse_error"})

        if not article.comments:
            try:
                c_page = await self.fetch_comments(
                    target.canonical_id,
                    offset=0,
                    limit=20,
                    order_by="score",
                    root_comment_id="",
                    author_hint=article.authors,
                )
                if c_page.comments:
                    article = article.model_copy(update={"comments": c_page.comments})
            except ZhihuError:
                pass

        want_tr = self._auth.config().auto_translate if auto_translate is None else bool(auto_translate)
        if want_tr:
            article, _ = await translate_article_if_enabled(
                self._ctx,
                article,
                auto_translate=True,
                target_lang=target_lang,
            )
            if article.comments:
                tr_comments = await translate_comments_if_enabled(
                    self._ctx,
                    article.comments,
                    auto_translate=True,
                    title=article.title,
                    target_lang=target_lang,
                )
                article = article.model_copy(update={"comments": tr_comments})
        return article

    async def _fetch_article_via_api(
        self, target: ParsedZhihuTarget
    ) -> tuple[TextArticle | None, ZhihuHttpResponse | None, bool]:
        if target.kind == "article" and target.article_id:
            resp = await self._http.fetch_with_fallback(
                self._web_url(f"/api/v4/articles/{target.article_id}"),
                self._auth,
                expect_json=True,
            )
            if not resp.ok:
                return None, resp, False
            try:
                j = json.loads(resp.body)
            except json.JSONDecodeError:
                return None, None, True
            art = parse_zhuanlan_api_json(j)
            return art, None, art is None

        if target.kind == "answer" and target.answer_id:
            resp = await self._http.fetch_with_fallback(
                self._web_url(
                    f"/api/v4/answers/{target.answer_id}"
                    "?include=content,voteup_count,comment_count,created_time,updated_time,author,question"
                ),
                self._auth,
                expect_json=True,
            )
            if not resp.ok:
                return None, resp, False
            try:
                j = json.loads(resp.body)
            except json.JSONDecodeError:
                return None, None, True
            art = parse_answer_api_json(j, target.question_id)
            return art, None, art is None

        if target.kind == "question" and target.question_id:
            q_resp = await self._http.fetch_with_fallback(
                self._web_url(
                    f"/api/v4/questions/{target.question_id}?include=detail,answer_count,follower_count,topics"
                ),
                self._auth,
                expect_json=True,
            )
            if not q_resp.ok:
                return None, q_resp, False
            try:
                qj = json.loads(q_resp.body)
            except json.JSONDecodeError:
                return None, None, True
            a_resp = await self._http.fetch_with_fallback(
                self._web_url(
                    f"/api/v4/questions/{target.question_id}/answers"
                    "?include=content,voteup_count,comment_count,author,created_time&limit=5&sort_by=default"
                ),
                self._auth,
                expect_json=True,
            )
            aj: Any = {}
            if a_resp.ok:
                try:
                    aj = json.loads(a_resp.body)
                except json.JSONDecodeError:
                    aj = {}
            art = parse_question_with_answers_api_json(qj, aj)
            return art, None, art is None

        return None, None, False

    async def fetch_comments(
        self,
        article_id: str = "",
        offset: int = 0,
        limit: int = 20,
        order_by: str = "default",
        root_comment_id: str = "",
        author_hint: str = "",
    ) -> CommentPageResult:
        """Fetch paginated root or child comments (`comment_v5` -> `v4`) fail-closed."""
        if offset < 0:
            raise ZhihuError("invalid_argument", "offset 必须 >= 0")
        if limit < 1:
            raise ZhihuError("invalid_argument", "limit 必须 >= 1")
        eff_limit = min(limit, 50)

        if root_comment_id:
            safe_cid = validate_segment_id(root_comment_id, field="root_comment_id")
            q_cid = urllib.parse.quote(safe_cid, safe="")
            offset_q = f"&offset={offset}" if offset > 0 else ""
            v5_child = self._web_url(
                f"/api/v4/comment_v5/comment/{q_cid}/child_comment?limit={eff_limit}{offset_q}"
            )
            resp = await self._http.fetch_with_fallback(v5_child, self._auth, expect_json=True)
            if not resp.ok and resp.error_code != "timeout":
                v4_child = self._web_url(
                    f"/api/v4/comments/{q_cid}/child_comments?limit={eff_limit}&offset={offset}"
                )
                v4_resp = await self._http.fetch_with_fallback(v4_child, self._auth, expect_json=True)
                if v4_resp.ok:
                    resp = v4_resp
            if not resp.ok:
                err_info = self._error_from_response(resp, "获取楼中楼评论失败")
                raise ZhihuError(
                    err_info["error_code"],
                    err_info["error"],
                    status_code=resp.status_code or None,
                    details=err_info,
                )
            try:
                j = json.loads(resp.body)
            except json.JSONDecodeError as exc:
                raise ZhihuError("validation_error", f"评论 JSON 解析失败: {exc}", details={"reason": "parse_error"}) from None
            if not isinstance(j, dict):
                raise ZhihuError("validation_error", "评论 JSON 结构无效", details={"reason": "parse_error"})
            return parse_comments_json(j, author_hint)

        target = parse_article_id_or_url(
            article_id,
            allow_loopback_http=self._allow_loopback_http,
            allowed_loopback_origins=self._allowed_origins(),
        )
        if target.kind == "article":
            rtype, rid = "articles", target.article_id
        elif target.kind == "question":
            rtype, rid = "questions", target.question_id
        elif target.kind == "answer":
            rtype, rid = "answers", target.answer_id
        else:
            raise ZhihuError("invalid_argument", f"无效的评论目标标识: {article_id!r}")
        if not rid:
            raise ZhihuError("invalid_argument", "缺少有效的评论目标 ID")

        v5_order = "ts" if order_by == "ts" else "score"
        offset_q = f"&offset={offset}" if offset > 0 else ""
        v5_url = self._web_url(
            f"/api/v4/comment_v5/{rtype}/{rid}/root_comment?order={v5_order}&limit={eff_limit}{offset_q}"
        )
        resp = await self._http.fetch_with_fallback(v5_url, self._auth, expect_json=True)
        if not resp.ok and resp.error_code != "timeout":
            v4_order = "reverse" if order_by == "ts" else "normal"
            v4_url = self._web_url(
                f"/api/v4/{rtype}/{rid}/root_comments?order={v4_order}&limit={eff_limit}&offset={offset}"
            )
            v4_resp = await self._http.fetch_with_fallback(v4_url, self._auth, expect_json=True)
            if v4_resp.ok:
                resp = v4_resp

        if not resp.ok:
            err_info = self._error_from_response(resp, "获取知乎评论失败")
            raise ZhihuError(
                err_info["error_code"],
                err_info["error"],
                status_code=resp.status_code or None,
                details=err_info,
            )

        try:
            j = json.loads(resp.body)
        except json.JSONDecodeError as exc:
            raise ZhihuError("validation_error", f"评论 JSON 解析失败: {exc}", details={"reason": "parse_error"}) from None
        if not isinstance(j, dict):
            raise ZhihuError("validation_error", "评论 JSON 结构无效", details={"reason": "parse_error"})

        page = parse_comments_json(j, author_hint)
        raw_data = j.get("data")
        if isinstance(raw_data, list):
            expanded = 0
            new_comments = list(page.comments)
            for idx, (node, raw_c) in enumerate(zip(new_comments, raw_data)):
                if expanded >= 3:
                    break
                if not isinstance(raw_c, dict):
                    continue
                child_count = raw_c.get("child_comment_count", 0)
                if isinstance(child_count, int) and child_count > 0 and not node.replies and node.id:
                    try:
                        child_page = await self.fetch_comments(
                            article_id,
                            offset=0,
                            limit=10,
                            order_by=order_by,
                            root_comment_id=node.id,
                            author_hint=author_hint,
                        )
                        if child_page.comments:
                            new_comments[idx] = node.model_copy(update={"replies": child_page.comments})
                            expanded += 1
                    except ZhihuError:
                        pass
            page.comments = new_comments
        return page
