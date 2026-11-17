"""Runtime engine for declarative JSON/Lua feed recipes."""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_sdk import CommentNode, PluginContext, TextArticle, TextFeedItem, TextParagraph

if __package__:
    from .cookie_bridge import CookieBridge
    from .html_css import (
        HtmlDocument,
        extract_readability_html,
        extract_value,
        html_to_paragraphs,
        normalize_ws,
        select_nodes,
    )
    from .http_client import HttpRequest, HttpResponse, HttpTransport, SafeHttpClient
    from .json_path import json_to_int, json_to_str, lookup_json_path, render_template, resolve_json_expr
    from .lua_engine import try_parse_declarative_lua
    from .recipe_spec import BUILTIN_STARTER_RECIPES, RecipeSpec, parse_recipe_dict, recipe_to_dict
    from .security import FeedRecipeError, resolve_url, validate_safe_path
    from .translator import Translator
    from .xml_feed import XmlFeedEntry, parse_xml_feed
else:
    from cookie_bridge import CookieBridge  # type: ignore[no-redef]
    from html_css import (  # type: ignore[no-redef]
        HtmlDocument,
        extract_readability_html,
        extract_value,
        html_to_paragraphs,
        normalize_ws,
        select_nodes,
    )
    from http_client import HttpRequest, HttpResponse, HttpTransport, SafeHttpClient  # type: ignore[no-redef]
    from json_path import (  # type: ignore[no-redef]
        json_to_int,
        json_to_str,
        lookup_json_path,
        render_template,
        resolve_json_expr,
    )
    from lua_engine import try_parse_declarative_lua  # type: ignore[no-redef]
    from recipe_spec import (  # type: ignore[no-redef]
        BUILTIN_STARTER_RECIPES,
        RecipeSpec,
        parse_recipe_dict,
        recipe_to_dict,
    )
    from security import FeedRecipeError, resolve_url, validate_safe_path  # type: ignore[no-redef]
    from translator import Translator  # type: ignore[no-redef]
    from xml_feed import XmlFeedEntry, parse_xml_feed  # type: ignore[no-redef]

_FEED_FIELDS = {
    "id": ("id", "guid", "uuid", "objectID"),
    "title": ("title", "name", "headline"),
    "url": ("url", "link", "href", "permalink"),
    "author": ("author", "creator", "by", "user"),
    "published_at": ("published_at", "published", "date", "created_at", "pubDate"),
    "excerpt": ("excerpt", "summary", "description", "content", "text"),
    "cover_url": ("cover_url", "image", "thumbnail", "cover"),
    "score": ("score", "points", "like_count", "likes"),
    "comment_count": ("comment_count", "comments", "num_comments", "reply_count"),
    "tags": ("tags", "categories", "labels"),
}
_ARTICLE_TEXT_FIELDS = ("text", "content", "body", "plain_text", "html", "description")
_ARTICLE_TITLE_FIELDS = ("title", "headline", "name")
_ARTICLE_AUTHOR_FIELDS = ("author", "creator", "byline", "by")
_ARTICLE_DATE_FIELDS = ("published_at", "published", "date", "created_at", "updated_at")


@dataclass(slots=True)
class FeedPage:
    items: list[TextFeedItem]
    effective_url: str = ""
    cached: bool = False


class RecipeEngine:
    """Load and execute recipe descriptors without executing arbitrary shell commands."""

    def __init__(
        self,
        ctx: PluginContext | None = None,
        *,
        transport: HttpTransport | None = None,
        allow_loopback_http: bool = False,
        builtin_dir: Path | None = None,
    ) -> None:
        self.ctx = ctx
        self.allow_loopback_http = bool(allow_loopback_http)
        self.http: HttpTransport = transport or SafeHttpClient()
        self.cookies = CookieBridge(ctx)
        self.translator = Translator(ctx)
        self.builtin_dir = builtin_dir or (Path(__file__).resolve().parent / "resources" / "recipes")
        self.recipes: dict[str, RecipeSpec] = {}
        self.cache: dict[tuple[str, str, int], tuple[float, FeedPage]] = {}
        self.cookie_mode = "auto"
        self.cookie_file_path = ""
        self.vault_cookie_header = ""
        self.timeout_sec = 15.0
        self.js_fallback = True
        self.auto_translate = True
        self.target_lang = "zh-CN"
        self.cache_ttl_sec = 300
        self._loaded_dir = ""

    def set_context(self, ctx: PluginContext | None) -> None:
        self.ctx = ctx
        self.cookies.set_context(ctx)
        self.translator.set_context(ctx)

    def configure(
        self,
        *,
        cookie_mode: str = "auto",
        cookie_file_path: str = "",
        vault_cookie_header: str = "",
        timeout_sec: float = 15.0,
        js_fallback: bool = True,
        auto_translate: bool = True,
        target_lang: str = "zh-CN",
        cache_ttl_sec: int = 300,
        cache_dir: Path | None = None,
    ) -> None:
        self.cookie_mode = (cookie_mode or "auto").strip().lower() or "auto"
        self.cookie_file_path = (cookie_file_path or "").strip()
        self.vault_cookie_header = (vault_cookie_header or "").strip()
        self.timeout_sec = max(0.1, min(float(timeout_sec), 120.0))
        self.js_fallback = bool(js_fallback)
        self.auto_translate = bool(auto_translate)
        self.target_lang = (target_lang or "zh-CN").strip() or "zh-CN"
        self.cache_ttl_sec = max(0, int(cache_ttl_sec))
        self.cookies.configure(self.cookie_mode, self.cookie_file_path, self.vault_cookie_header)
        self.translator.configure(cache_dir, self.auto_translate, self.target_lang)

    def _load_file(self, path: Path, *, is_builtin: bool) -> RecipeSpec | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            if path.suffix.lower() == ".json":
                spec = parse_recipe_dict(
                    json.loads(raw),
                    source_path=str(path),
                    raw_source=raw,
                    allow_loopback_http=self.allow_loopback_http,
                )
            elif path.suffix.lower() == ".lua":
                spec = try_parse_declarative_lua(
                    raw,
                    source_path=str(path),
                    allow_loopback_http=self.allow_loopback_http,
                )
            else:
                return None
        except (OSError, json.JSONDecodeError, FeedRecipeError):
            return None
        if spec is not None:
            spec.is_builtin = is_builtin
        return spec

    def load_recipes(self, custom_dir: str | Path | None = None, *, force: bool = False) -> list[RecipeSpec]:
        target_dir = str(Path(custom_dir).expanduser()) if custom_dir else ""
        if not force and target_dir == self._loaded_dir and self.recipes:
            return list(self.recipes.values())
        self.recipes.clear()
        self.cache.clear()
        self._loaded_dir = target_dir
        for path in sorted(self.builtin_dir.glob("*")):
            if path.suffix.lower() not in {".json", ".lua"}:
                continue
            spec = self._load_file(path, is_builtin=True)
            if spec is not None and spec.enabled:
                self.recipes[spec.id] = spec
        if target_dir:
            try:
                root = validate_safe_path(target_dir, check_symlinks=True)
            except FeedRecipeError:
                root = Path()
            if root.is_dir():
                for path in sorted(root.glob("*")):
                    if path.suffix.lower() not in {".json", ".lua"}:
                        continue
                    spec = self._load_file(path, is_builtin=False)
                    if spec is not None and spec.enabled:
                        self.recipes[spec.id] = spec
        return list(self.recipes.values())

    def recipe(self, recipe_id: str = "") -> RecipeSpec:
        rid = (recipe_id or "").strip()
        if not rid:
            if not self.recipes:
                raise FeedRecipeError("not_found", "没有可用的 feed 配方")
            return next(iter(self.recipes.values()))
        spec = self.recipes.get(rid)
        if spec is None:
            raise FeedRecipeError("not_found", f"未找到配方 {rid!r}")
        return spec

    async def _chrome_extract(self, url: str) -> str:
        if self.ctx is None or not self.ctx.capabilities.has("web.extract.v1"):
            raise FeedRecipeError("cloudflare_challenge", "页面触发 JS/Cloudflare 校验，且 web.extract.v1 不可用")
        res = await self.ctx.capabilities.call(
            "web.extract.v1",
            {
                "action": "extract",
                "url": url,
                "mode": "auto",
                "render_js": True,
                "use_cookies": True,
                "timeout_ms": int(self.timeout_sec * 1000),
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            raise FeedRecipeError(
                str(res.data.get("error_code") or "service_offline") if isinstance(res.data, Mapping) else "service_offline",
                res.error or "Chrome 页面抽取失败",
            )
        text = str(res.data.get("html") or res.data.get("text") or res.data.get("body") or "")
        if not text:
            raise FeedRecipeError("invalid_response", "Chrome 抽取结果为空")
        return text

    async def _request(self, url: str, spec: RecipeSpec, extra_headers: Mapping[str, str] | None = None) -> HttpResponse:
        cookie = ""
        if spec.use_chrome_cookies:
            resolution = await self.cookies.resolve_for_url(
                url,
                spec.id,
                use_chrome_cookies=True,
                override_domain=spec.cookie_domain,
            )
            cookie = resolution.header
        headers = {"Accept": "*/*", "User-Agent": "OmniDesk-FeedRecipe/0.2", **dict(extra_headers or {})}
        response = await self.http.request(
            HttpRequest(
                url=url,
                method=spec.feed.method if spec.feed.method in {"GET", "POST"} else "GET",
                headers=headers,
                cookie_header=cookie,
                timeout_sec=self.timeout_sec,
            ),
            extra_secrets=tuple(item for item in (cookie,) if item),
            allow_loopback_http=self.allow_loopback_http,
        )
        if response.error:
            raise FeedRecipeError(
                response.error_code or "service_offline",
                response.error,
                http_status=response.status_code,
            )
        if response.challenge_detected and self.js_fallback:
            extracted = await self._chrome_extract(url)
            return HttpResponse(
                status_code=200,
                effective_url=url,
                headers={"content-type": "text/html"},
                body=extracted,
            )
        return response

    @staticmethod
    def _template_url(template: str, variables: Mapping[str, Any]) -> str:
        return render_template(template, variables).strip()

    @staticmethod
    def _tags_value(value: Any, fallback: Sequence[str]) -> list[str]:
        if isinstance(value, list):
            tags = [str(item).strip() for item in value if str(item).strip()]
            if tags:
                return tags
        if isinstance(value, str) and value.strip():
            return [part.strip() for part in value.split(",") if part.strip()]
        return list(fallback)

    @staticmethod
    def _make_feed_item(
        *,
        spec: RecipeSpec,
        item_id: str,
        title: str,
        url: str,
        author: str = "",
        published_at: str = "",
        excerpt: str = "",
        cover_url: str = "",
        score: int = 0,
        comment_count: int = 0,
        tags: Sequence[str] = (),
        article_url: str = "",
    ) -> TextFeedItem:
        return TextFeedItem(
            id=item_id or url or title,
            source_id="plugin-feed-recipe",
            source_name=spec.name,
            category=spec.category,
            channel_id=spec.id,
            title=title or url or item_id or spec.name,
            excerpt=excerpt,
            author=author,
            published_at=published_at,
            url=url,
            cover_url=cover_url,
            tags=list(tags),
            score=max(0, int(score)),
            comment_count=max(0, int(comment_count)),
            extra={"recipe_id": spec.id, "article_url": article_url or url},
        )

    def _json_feed(self, payload: str, spec: RecipeSpec, base_url: str) -> list[TextFeedItem]:
        try:
            doc = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FeedRecipeError("parse_error", f"配方 JSON 响应无效: {exc}") from None
        raw_items = lookup_json_path(doc, spec.feed.items_path) if spec.feed.items_path else doc
        if not isinstance(raw_items, list):
            raise FeedRecipeError("parse_error", f"配方 items_path 未返回数组: {spec.feed.items_path}")
        fields = spec.feed.fields
        out: list[TextFeedItem] = []
        for raw_item in raw_items[: spec.feed.max_items]:
            if not isinstance(raw_item, Mapping):
                continue
            def field(name: str) -> Any:
                expr = fields.get(name, "")
                if expr:
                    return resolve_json_expr(raw_item, expr, doc)
                for alt in _FEED_FIELDS.get(name, ()):
                    hit = lookup_json_path(raw_item, alt)
                    if hit not in (None, ""):
                        return hit
                return None

            title = json_to_str(field("title"))
            url = json_to_str(field("url"))
            item_id = json_to_str(field("id")) or url or title
            if not title and not url:
                continue
            resolved_url = resolve_url(base_url, url) if url else ""
            out.append(
                self._make_feed_item(
                    spec=spec,
                    item_id=item_id,
                    title=title or url,
                    url=resolved_url,
                    author=json_to_str(field("author")),
                    published_at=json_to_str(field("published_at")),
                    excerpt=json_to_str(field("excerpt")),
                    cover_url=resolve_url(base_url, json_to_str(field("cover_url"))),
                    score=json_to_int(field("score")),
                    comment_count=json_to_int(field("comment_count")),
                    tags=self._tags_value(field("tags"), spec.tags),
                    article_url=resolved_url,
                )
            )
        return out

    def _xml_feed(self, payload: str, spec: RecipeSpec, base_url: str) -> list[TextFeedItem]:
        entries = parse_xml_feed(payload, limit=spec.feed.max_items)
        return [
            self._make_feed_item(
                spec=spec,
                item_id=entry.id or entry.url or entry.title,
                title=entry.title,
                url=resolve_url(base_url, entry.url),
                author=entry.author,
                published_at=entry.published_at,
                excerpt=entry.summary,
                tags=entry.categories or spec.tags,
                article_url=resolve_url(base_url, entry.url),
            )
            for entry in entries
        ]

    def _html_feed(self, payload: str, spec: RecipeSpec, base_url: str) -> list[TextFeedItem]:
        doc = HtmlDocument(payload)
        nodes = select_nodes(doc.root, spec.feed.item_selector)
        out: list[TextFeedItem] = []
        for node in nodes[: spec.feed.max_items]:
            def value(name: str) -> str:
                expr = spec.feed.fields.get(name, "")
                if expr:
                    return extract_value(node, expr).strip()
                for alt in _FEED_FIELDS.get(name, ()):
                    found = extract_value(node, alt)
                    if found.strip():
                        return found.strip()
                return ""

            title = value("title")
            url = resolve_url(base_url, value("url"))
            if not title and not url:
                continue
            out.append(
                self._make_feed_item(
                    spec=spec,
                    item_id=value("id") or url or title,
                    title=title or url,
                    url=url,
                    author=value("author"),
                    published_at=value("published_at"),
                    excerpt=value("excerpt"),
                    cover_url=resolve_url(base_url, value("cover_url")),
                    score=int(value("score") or 0),
                    comment_count=int(value("comment_count") or 0),
                    tags=spec.tags,
                    article_url=url,
                )
            )
        return out

    def parse_feed(self, payload: str, spec: RecipeSpec, base_url: str) -> list[TextFeedItem]:
        if spec.format == "json":
            return self._json_feed(payload, spec, base_url)
        if spec.format == "xml":
            return self._xml_feed(payload, spec, base_url)
        return self._html_feed(payload, spec, base_url)

    async def fetch_feed(
        self,
        recipe_id: str,
        *,
        query: str = "",
        page: int = 1,
        force_refresh: bool = False,
    ) -> FeedPage:
        spec = self.recipe(recipe_id)
        if page < 1:
            return FeedPage([])
        variables = {"query": query or "", "page": page}
        url = self._template_url(spec.feed.url, variables)
        cache_key = (spec.id, query, page)
        now = time.monotonic()
        ttl = spec.cache_ttl_sec if spec.cache_ttl_sec >= 0 else self.cache_ttl_sec
        cached = self.cache.get(cache_key)
        if not force_refresh and ttl > 0 and cached is not None and cached[0] > now:
            return FeedPage(items=[item.model_copy(deep=True) for item in cached[1].items], cached=True)
        response = await self._request(url, spec, spec.feed.headers)
        items = self.parse_feed(response.body, spec, response.effective_url or url)
        if self.auto_translate and items:
            for item in items:
                if item.title and not item.title_zh:
                    translated = await self.translator.translate_text(item.title, self.target_lang)
                    if translated:
                        item.title_zh = translated
                        item.bilingual_cached = True
        page_result = FeedPage(items=items, effective_url=response.effective_url or url)
        if ttl > 0:
            self.cache[cache_key] = (now + ttl, FeedPage(items=[item.model_copy(deep=True) for item in items]))
        return page_result

    @staticmethod
    def _json_comments(
        raw: Any,
        spec: RecipeSpec,
        *,
        depth: int = 0,
    ) -> list[CommentNode]:
        if depth > 12 or not isinstance(raw, list):
            return []
        cs = spec.article.comments
        out: list[CommentNode] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            body = json_to_str(
                lookup_json_path(item, cs.body) if cs.body else item.get("body")
            )
            if not body:
                for key in ("body", "text", "content", "comment"):
                    body = json_to_str(item.get(key))
                    if body:
                        break
            if not body:
                continue
            replies_raw = lookup_json_path(item, cs.replies) if cs.replies else item.get("replies")
            out.append(
                CommentNode(
                    id=json_to_str(lookup_json_path(item, cs.id) if cs.id else item.get("id")),
                    author=json_to_str(lookup_json_path(item, cs.author) if cs.author else item.get("author")),
                    role_badge=json_to_str(lookup_json_path(item, cs.role_badge)) if cs.role_badge else "",
                    body=body,
                    created_at=json_to_str(
                        lookup_json_path(item, cs.created_at) if cs.created_at else item.get("created_at")
                    ),
                    score=json_to_int(lookup_json_path(item, cs.score)) if cs.score else 0,
                    replies=RecipeEngine._json_comments(replies_raw, spec, depth=depth + 1),
                )
            )
        return out

    def _json_article(self, payload: str, spec: RecipeSpec, url: str) -> TextArticle:
        try:
            doc = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FeedRecipeError("parse_error", f"文章 JSON 响应无效: {exc}") from None
        content = lookup_json_path(doc, spec.article.content_path) if spec.article.content_path else doc
        title = ""
        author = ""
        published = ""
        if isinstance(content, Mapping):
            title = next((json_to_str(content.get(k)) for k in _ARTICLE_TITLE_FIELDS if content.get(k)), "")
            author = next((json_to_str(content.get(k)) for k in _ARTICLE_AUTHOR_FIELDS if content.get(k)), "")
            published = next((json_to_str(content.get(k)) for k in _ARTICLE_DATE_FIELDS if content.get(k)), "")
            body_value = next((content.get(k) for k in _ARTICLE_TEXT_FIELDS if content.get(k)), "")
        else:
            body_value = content
        if not title:
            title = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1] or "Untitled"
        paragraphs: list[TextParagraph] = []
        if isinstance(body_value, list):
            for idx, item in enumerate(body_value):
                if isinstance(item, Mapping):
                    text = next((json_to_str(item.get(k)) for k in _ARTICLE_TEXT_FIELDS if item.get(k)), "")
                    kind = str(item.get("kind") or item.get("type") or "paragraph")
                    if kind not in {"heading", "paragraph", "quote", "code", "formula", "image"}:
                        kind = "paragraph"
                else:
                    text = json_to_str(item)
                    kind = "paragraph"
                image = str(item.get("image_url") or item.get("imageUrl") or "") if isinstance(item, Mapping) else ""
                if text or (kind == "image" and image):
                    paragraphs.append(TextParagraph(id=f"p{idx + 1}", kind=kind, original=text,
                        image_url=image, alt=str(item.get("alt") or "") if isinstance(item, Mapping) else "",
                        caption=str(item.get("caption") or "") if isinstance(item, Mapping) else ""))
        elif isinstance(body_value, str) and body_value.strip():
            if "<" in body_value and ">" in body_value:
                paragraphs = html_to_paragraphs(body_value, url)
            else:
                paragraphs = [
                    TextParagraph(id=f"p{idx + 1}", kind="paragraph", original=part.strip())
                    for idx, part in enumerate(body_value.split("\n\n"))
                    if part.strip()
                ]
        raw_comments = (
            lookup_json_path(doc, spec.article.comments.path) if spec.article.comments.path else None
        )
        comments = self._json_comments(raw_comments, spec)
        return TextArticle(
            id=url,
            source_id="plugin-feed-recipe",
            source_name=spec.name,
            category=spec.category,
            channel_id=spec.id,
            title=title,
            authors=author,
            author=author,
            published_at=published,
            url=url,
            tags=list(spec.article.tags or spec.tags),
            paragraphs=paragraphs,
            comments=comments,
            extra={"recipe_id": spec.id},
        )

    def _html_article(self, payload: str, spec: RecipeSpec, url: str) -> TextArticle:
        doc = HtmlDocument(payload)
        title = ""
        if spec.article.title_selector:
            title = extract_value(doc.root, spec.article.title_selector).strip()
        if not title:
            for selector in ("h1", "article h1", "meta[property=og:title]"):
                title = extract_value(doc.root, selector).strip()
                if title:
                    break
        if not title:
            title = doc.title().strip() or urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1] or "Untitled"
        author = extract_value(doc.root, spec.article.author_selector).strip() if spec.article.author_selector else ""
        published = (
            extract_value(doc.root, spec.article.published_selector).strip()
            if spec.article.published_selector
            else ""
        )
        body_html = ""
        if spec.article.body_selector:
            body_html = extract_value(doc.root, f"{spec.article.body_selector}@html").strip()
        if not body_html:
            body_html = extract_readability_html(payload, spec.article.body_selector)
        paragraphs = html_to_paragraphs(body_html, url)
        cs = spec.article.comments
        comments: list[CommentNode] = []
        if cs.item_selector:
            for idx, node in enumerate(select_nodes(doc.root, cs.item_selector)):
                body = extract_value(node, cs.body).strip() if cs.body else ""
                if not body:
                    continue
                comments.append(
                    CommentNode(
                        id=extract_value(node, cs.id).strip() if cs.id else f"comment-{idx + 1}",
                        author=extract_value(node, cs.author).strip() if cs.author else "",
                        role_badge=extract_value(node, cs.role_badge).strip() if cs.role_badge else "",
                        body=body,
                        created_at=extract_value(node, cs.created_at).strip() if cs.created_at else "",
                        score=json_to_int(extract_value(node, cs.score)) if cs.score else 0,
                    )
                )
        return TextArticle(
            id=url,
            source_id="plugin-feed-recipe",
            source_name=spec.name,
            category=spec.category,
            channel_id=spec.id,
            title=title,
            authors=author,
            author=author,
            published_at=published,
            url=url,
            tags=list(spec.article.tags or spec.tags),
            paragraphs=paragraphs,
            comments=comments,
            extra={"recipe_id": spec.id},
        )

    def parse_article(self, payload: str, spec: RecipeSpec, url: str) -> TextArticle:
        if spec.format == "json":
            return self._json_article(payload, spec, url)
        return self._html_article(payload, spec, url)

    async def fetch_article(self, recipe_id: str, article_ref: str) -> TextArticle:
        spec = self.recipe(recipe_id)
        ref = (article_ref or "").strip()
        if not ref:
            raise FeedRecipeError("invalid_argument", "article_id is required")
        if ref.startswith(("http://", "https://")):
            url = ref
        else:
            url = self._template_url(spec.article.url, {"id": ref, "url": ref, "recipe_id": spec.id})
        response = await self._request(url, spec)
        article = self.parse_article(response.body, spec, response.effective_url or url)
        if self.auto_translate:
            await self.translator.translate_article(article, self.target_lang)
        return article

    def recipe_dicts(self) -> list[dict[str, Any]]:
        return [recipe_to_dict(spec) for spec in self.recipes.values()]

