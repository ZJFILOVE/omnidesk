"""Blog discovery, feed aggregation, article extraction, cookies, and Chrome fallback."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omnidesk_sdk import PluginContext
from omnidesk_sdk.auth import filter_cookies, parse_json_cookies, parse_netscape_cookies
from omnidesk_sdk.http import SafeHttpClient

if __package__:
    from .blog_parser import (
        BlogArticle,
        BlogFeedEntry,
        BlogParseError,
        discover_article_links,
        discover_feed_links,
        html_title,
        parse_feed,
        parse_html_article,
    )
    from .blog_store import BlogStoreError, BlogSubscription, normalize_blog_url
else:
    from blog_parser import (  # type: ignore[no-redef]
        BlogArticle,
        BlogFeedEntry,
        BlogParseError,
        discover_article_links,
        discover_feed_links,
        html_title,
        parse_feed,
        parse_html_article,
    )
    from blog_store import BlogStoreError, BlogSubscription, normalize_blog_url  # type: ignore[no-redef]

_CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "cf_chl_opt",
    "challenges.cloudflare.com",
    "enable javascript and cookies to continue",
    "just a moment...",
)
_COMMON_FEED_PATHS = ("/feed", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml", "/rss", "/?feed=rss2")


class BlogError(RuntimeError):
    """Normalized blog client error."""

    def __init__(self, code: str, message: str, *, http_status: int = 0, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        super().__init__(message)


def _as_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.2, min(120.0, parsed))


def _as_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 100) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _cookie_header_from_data(data: Mapping[str, Any], url: str) -> str:
    raw_header = data.get("cookie_header") or data.get("header")
    if isinstance(raw_header, str) and raw_header.strip():
        return raw_header.strip()
    cookies = data.get("cookies")
    if isinstance(cookies, str) and cookies.strip():
        return cookies.strip()
    if isinstance(cookies, Mapping):
        return "; ".join(f"{key}={value}" for key, value in cookies.items() if str(key).strip() and value is not None)
    if isinstance(cookies, list):
        try:
            parsed = parse_json_cookies(json.dumps(cookies))
        except Exception:
            return ""
        matched = filter_cookies(parsed, url)
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in matched)
    return ""


class BlogClient:
    """Fetches feeds/articles for saved blogs with strict HTTPS validation."""

    def __init__(self, ctx: PluginContext | None = None, *, http: Any | None = None) -> None:
        self.ctx = ctx
        self.http = http or SafeHttpClient()
        self.timeout_sec = 20.0
        self.prefer_chrome_fetch = True
        self.max_articles_per_feed = 40
        self.cookie_domain = ""
        self.cookie_header = ""
        self.cookie_file_path = ""

    def set_context(self, ctx: PluginContext | None) -> None:
        self.ctx = ctx

    def configure(self, config: Mapping[str, Any]) -> None:
        self.timeout_sec = _as_float(config.get("request_timeout_sec", 20), 20.0)
        self.prefer_chrome_fetch = bool(config.get("prefer_chrome_fetch", True))
        self.max_articles_per_feed = _as_int(config.get("max_articles_per_feed", 40), 40)
        self.cookie_domain = str(config.get("cookie_domain") or "").strip().lower().lstrip(".")
        self.cookie_file_path = str(config.get("cookie_file_path") or "").strip()
        self.cookie_header = ""
        if self.ctx is not None:
            self.cookie_header = str(self.ctx.get_secret("cookie_header", "") or "").strip()

    async def _resolve_cookies(self, url: str) -> str:
        if self.ctx is not None and self.ctx.capabilities.has("web.cookies.v1"):
            result = await self.ctx.capabilities.call(
                "web.cookies.v1",
                {"action": "get_cookies", "url": url, "include_subdomains": True},
            )
            if result.ok and isinstance(result.data, Mapping):
                header = _cookie_header_from_data(result.data, url)
                if header:
                    return header
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if self.cookie_header and self.cookie_domain and (
            host == self.cookie_domain or host.endswith(f".{self.cookie_domain}")
        ):
            return self.cookie_header
        if self.cookie_file_path:
            path = Path(self.cookie_file_path).expanduser()
            try:
                text = path.read_text(encoding="utf-8")
                cookies = (
                    parse_json_cookies(text)
                    if text.lstrip().startswith(("{", "["))
                    else parse_netscape_cookies(text)
                )
            except (OSError, Exception):
                cookies = []
            matched = filter_cookies(cookies, url)
            if matched:
                return "; ".join(f"{cookie.name}={cookie.value}" for cookie in matched)
        return ""

    async def _request(self, url: str, *, accept: str = "") -> tuple[str, str, str]:
        cookies = await self._resolve_cookies(url)
        headers = {
            "Accept": accept
            or "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if cookies:
            headers["Cookie"] = cookies
        secrets = (cookies,) if cookies else ()
        try:
            response = await self.http.get(
                url,
                headers=headers,
                timeout_sec=self.timeout_sec,
                secrets=secrets,
            )
        except Exception as exc:
            raise BlogError("service_offline", f"博客请求失败: {exc}", retryable=True) from None
        if response.status_code >= 400:
            raise BlogError(
                "upstream_status",
                f"博客返回 HTTP {response.status_code}",
                http_status=response.status_code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        content_type = response.get_header("content-type", "").lower()
        return response.text, content_type, response.url

    async def _chrome_extract(self, url: str) -> str:
        if self.ctx is None or not self.prefer_chrome_fetch or not self.ctx.capabilities.has("web.extract.v1"):
            raise BlogError("challenge_detected", "页面触发 JS/Cloudflare 校验，且 Chrome 抽取不可用")
        result = await self.ctx.capabilities.call(
            "web.extract.v1",
            {
                "action": "extract",
                "url": url,
                "render_js": True,
                "use_cookies": True,
                "timeout_ms": int(self.timeout_sec * 1000),
            },
        )
        if not result.ok or not isinstance(result.data, Mapping):
            raise BlogError(
                str(result.data.get("error_code") or "service_offline")
                if isinstance(result.data, Mapping)
                else "service_offline",
                result.error or "Chrome 页面抽取失败",
            )
        html_text = str(result.data.get("html") or result.data.get("text") or result.data.get("body") or "")
        if not html_text.strip():
            raise BlogError("invalid_response", "Chrome 抽取结果为空")
        return html_text

    @staticmethod
    def _looks_like_feed(text: str, content_type: str) -> bool:
        stripped = (text or "").lstrip()
        if not stripped:
            return False
        if any(token in content_type for token in ("rss", "atom")) and "html" not in content_type:
            return True
        lowered = stripped[:800].lower()
        if lowered.startswith(("<rss", "<feed", "<rdf")) or (
            lowered.startswith("<?xml") and ("<rss" in lowered or "<feed" in lowered or "<rdf" in lowered)
        ):
            return True
        return False

    async def discover(self, url: str) -> BlogSubscription:
        """Discover a blog's feed and title from any blog URL."""
        normalized = normalize_blog_url(url)
        text, content_type, final_url = await self._request(normalized)
        if self._looks_like_feed(text, content_type):
            feed = parse_feed(text, source_url=final_url)
            return BlogSubscription(
                id="",
                url=normalized,
                title=feed.title or urllib.parse.urlsplit(normalized).hostname or normalized,
                feed_url=final_url,
                site_url=feed.site_url or normalized,
            )

        links = discover_feed_links(text, final_url)
        links.extend(
            urllib.parse.urljoin(final_url, path)
            for path in _COMMON_FEED_PATHS
        )
        candidates: list[str] = []
        seen: set[str] = set()
        for candidate in links:
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)

        results = await asyncio.gather(
            *(self._try_feed(candidate) for candidate in candidates[:6]),
            return_exceptions=True,
        )
        for candidate, result in zip(candidates[:6], results):
            if isinstance(result, Exception) or result is None:
                continue
            feed, feed_url = result
            return BlogSubscription(
                id="",
                url=normalized,
                title=feed.title or html_title(text) or urllib.parse.urlsplit(normalized).hostname or normalized,
                feed_url=feed_url,
                site_url=feed.site_url or normalized,
            )
        return BlogSubscription(
            id="",
            url=normalized,
            title=html_title(text) or urllib.parse.urlsplit(normalized).hostname or normalized,
            feed_url="",
            site_url=normalized,
        )

    async def _try_feed(self, url: str) -> tuple[Any, str] | None:
        try:
            text, content_type, final_url = await self._request(url)
            if not self._looks_like_feed(text, content_type):
                return None
            return parse_feed(text, source_url=final_url), final_url
        except Exception:
            return None

    async def fetch_feed(self, sub: BlogSubscription) -> list[BlogFeedEntry]:
        """Fetch one blog's feed, falling back to homepage article links."""
        entries: list[BlogFeedEntry] = []
        error: Exception | None = None
        if not sub.feed_url:
            try:
                discovered = await self.discover(sub.url)
                if discovered.feed_url:
                    sub.feed_url = discovered.feed_url
                    sub.site_url = sub.site_url or discovered.site_url
                    sub.title = sub.title or discovered.title
            except Exception:
                pass
        if sub.feed_url:
            try:
                text, content_type, final_url = await self._request(sub.feed_url)
                if not self._looks_like_feed(text, content_type):
                    raise BlogError("invalid_response", "订阅地址返回的不是 RSS/Atom Feed")
                entries = parse_feed(text, source_url=final_url).entries
            except Exception as exc:
                error = exc
        if not entries:
            try:
                text, _content_type, final_url = await self._request(sub.url)
                links = discover_article_links(text, final_url)
                entries = [
                    BlogFeedEntry(id=link, title=title, url=link)
                    for link, title in links
                ]
            except Exception as exc:
                if error is not None:
                    raise error
                raise exc
        for entry in entries:
            if not entry.url and entry.id:
                entry.url = entry.id
            entry.id = f"{sub.id}::{entry.url or entry.id}"
        return entries[: self.max_articles_per_feed]

    async def fetch_article(self, sub: BlogSubscription, url: str) -> BlogArticle:
        """Fetch and parse a full article, using Chrome when block/SPA pages appear."""
        article_url = normalize_blog_url(url, allow_http=True) if url else sub.url
        html_text = ""
        error: Exception | None = None
        try:
            html_text, _content_type, article_url = await self._request(article_url)
        except BlogError as exc:
            error = exc
        lowered = html_text.casefold()
        if not html_text or any(marker in lowered for marker in _CHALLENGE_MARKERS):
            html_text = await self._chrome_extract(article_url)
        article = parse_html_article(html_text, url=article_url)
        if len(article.paragraphs) < 2 and self.prefer_chrome_fetch:
            try:
                chrome_html = await self._chrome_extract(article_url)
                chrome_article = parse_html_article(chrome_html, url=article_url)
                if len(chrome_article.paragraphs) > len(article.paragraphs):
                    article = chrome_article
            except Exception:
                if not article.paragraphs and error is not None:
                    raise error
        if not article.paragraphs:
            raise BlogError("invalid_response", "未能从页面提取正文")
        article.author = article.author or sub.title
        article.site_name = article.site_name or sub.title
        return article

    @staticmethod
    def match_subscription(
        subscriptions: Sequence[BlogSubscription],
        *,
        identifier: str = "",
        url: str = "",
    ) -> BlogSubscription | None:
        """Resolve a subscription from an id or article URL."""
        target = (identifier or "").strip()
        for sub in subscriptions:
            if sub.id == target:
                return sub
        host = (urllib.parse.urlsplit(url or target).hostname or "").lower()
        if host:
            for sub in subscriptions:
                sub_host = (urllib.parse.urlsplit(sub.url).hostname or "").lower()
                if sub_host == host or host.endswith(f".{sub_host}"):
                    return sub
        return None
