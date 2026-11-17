"""HTTPS-only arXiv, Nature RSS, bioRxiv, PubMed, Crossref, and article client."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

from omnidesk_sdk import PluginContext, TextArticle, TextFeedItem, TextParagraph
from omnidesk_sdk.http import HttpResponse, SafeHttpClient

if __package__:
    from .journals_parsers import (
        FeedEntry,
        JournalParseError,
        parse_arxiv,
        parse_biorxiv,
        parse_crossref,
        parse_html_article,
        parse_pubmed,
        parse_rss,
        strip_markup,
    )
else:
    from journals_parsers import (  # type: ignore[no-redef]
        FeedEntry,
        JournalParseError,
        parse_arxiv,
        parse_biorxiv,
        parse_crossref,
        parse_html_article,
        parse_pubmed,
        parse_rss,
        strip_markup,
    )

SOURCE_LABELS = {
    "arxiv": "arXiv",
    "nature": "Nature",
    "biorxiv": "bioRxiv",
    "medrxiv": "medRxiv",
    "pubmed": "PubMed",
}
_CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "cf_chl_opt",
    "challenges.cloudflare.com",
    "enable javascript and cookies to continue",
    "just a moment...",
)


class JournalError(RuntimeError):
    """Normalized plugin error with a stable error code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 0,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        super().__init__(message)


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _as_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 100) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _as_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.2, min(120.0, parsed))


class JournalClient:
    """Fetch academic feeds and papers using the hardened SDK HTTP client."""

    def __init__(
        self,
        ctx: PluginContext | None = None,
        *,
        http: Any | None = None,
    ) -> None:
        self.ctx = ctx
        self.http = http or SafeHttpClient()
        self.enabled_sources = ["arxiv", "nature", "biorxiv", "pubmed"]
        self.timeout_sec = 15.0
        self.max_results = 20
        self.arxiv_categories = ["cs.AI", "cs.CL", "cs.LG", "q-bio"]
        self.nature_feeds = ["https://www.nature.com/nature.rss", "https://www.nature.com/natmachintell.rss"]
        self.biorxiv_server = "biorxiv"
        self.biorxiv_days = 7
        self.pubmed_query = "machine learning[Title/Abstract] OR foundation model[Title/Abstract]"
        self.crossref_mailto = ""
        self.ncbi_api_key = ""
        self.prefer_chrome_fetch = True
        self._entry_cache: dict[str, FeedEntry] = {}

    def set_context(self, ctx: PluginContext | None) -> None:
        self.ctx = ctx

    def configure(self, config: Mapping[str, Any]) -> None:
        sources = _split_csv(config.get("enabled_sources", self.enabled_sources))
        allowed = {"arxiv", "nature", "biorxiv", "medrxiv", "pubmed"}
        self.enabled_sources = [item for item in sources if item in allowed] or ["arxiv"]
        self.timeout_sec = _as_float(config.get("request_timeout_sec", 15), 15.0)
        self.max_results = _as_int(config.get("max_results", 20), 20)
        categories = _split_csv(config.get("arxiv_categories", self.arxiv_categories))
        self.arxiv_categories = categories or ["cs.AI"]
        feeds = _split_csv(config.get("nature_feeds", self.nature_feeds))
        self.nature_feeds = [url for url in feeds if url.startswith("https://")] or self.nature_feeds
        server = str(config.get("biorxiv_server") or "biorxiv").strip().lower()
        self.biorxiv_server = server if server in {"biorxiv", "medrxiv"} else "biorxiv"
        self.biorxiv_days = _as_int(config.get("biorxiv_days", 7), 7, minimum=1, maximum=365)
        self.pubmed_query = str(config.get("pubmed_query") or self.pubmed_query).strip()
        self.crossref_mailto = str(config.get("crossref_mailto") or "").strip()
        self.prefer_chrome_fetch = bool(config.get("prefer_chrome_fetch", True))
        if self.ctx is not None:
            self.ncbi_api_key = str(self.ctx.get_secret("ncbi_api_key", "") or "").strip()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/atom+xml, application/rss+xml, application/json, text/xml, text/html;q=0.9",
            "User-Agent": "OmniDesk/2.0 (journals; +https://github.com/ZJFILOVE/omnidesk)",
        }
        return headers

    async def _request(self, url: str, *, params: Mapping[str, Any] | None = None) -> HttpResponse:
        if params:
            encoded = urllib.parse.urlencode({key: value for key, value in params.items() if value not in (None, "")})
            url = f"{url}{'&' if '?' in url else '?'}{encoded}"
        secrets = (self.ncbi_api_key,) if self.ncbi_api_key else ()
        try:
            response = await self.http.get(
                url,
                headers=self._headers(),
                timeout_sec=self.timeout_sec,
                secrets=secrets,
            )
        except Exception as exc:
            raise JournalError("service_offline", f"上游请求失败: {exc}", retryable=True) from None
        if response.status_code >= 400:
            raise JournalError(
                "upstream_status",
                f"上游返回 HTTP {response.status_code}",
                http_status=response.status_code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        return response

    def channel_specs(self) -> list[tuple[str, str, str]]:
        """Return `(channel_id, display_name, source)` tuples for enabled sources."""
        out: list[tuple[str, str, str]] = []
        for source in self.enabled_sources:
            if source == "nature":
                for index, url in enumerate(self.nature_feeds):
                    label = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1] or f"Nature {index + 1}"
                    out.append((f"nature:{index}", label.replace(".rss", ""), "nature"))
            else:
                out.append((source, SOURCE_LABELS.get(source, source), source))
        return out

    async def fetch_feed(
        self,
        channel_id: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[FeedEntry]:
        del force_refresh
        channel = (channel_id or "").strip()
        if not channel:
            channel = "arxiv" if "arxiv" in self.enabled_sources else self.enabled_sources[0]
        source = channel.split(":", 1)[0]
        page = max(1, page)
        if source == "arxiv":
            entries = await self._fetch_arxiv(query, page)
        elif source == "nature":
            entries = await self._fetch_nature(channel)
        elif source in {"biorxiv", "medrxiv"}:
            entries = await self._fetch_biorxiv(channel_source=source)
        elif source == "pubmed":
            entries = await self._fetch_pubmed(query, page)
        else:
            raise JournalError("invalid_argument", f"未知的学术数据源: {source!r}")
        if query and source != "arxiv" and source != "pubmed":
            needle = query.casefold()
            entries = [
                entry
                for entry in entries
                if needle in entry.title.casefold()
                or needle in entry.summary.casefold()
                or needle in entry.author.casefold()
            ]
        for entry in entries:
            self._entry_cache[entry.id] = entry
        return entries

    async def _fetch_arxiv(self, query: str, page: int) -> list[FeedEntry]:
        raw_query = query.strip()
        if not raw_query:
            search_query = " OR ".join(f"cat:{category}" for category in self.arxiv_categories)
        elif ":" in raw_query:
            search_query = raw_query
        else:
            search_query = f"all:{raw_query}"
        response = await self._request(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": search_query,
                "start": (page - 1) * self.max_results,
                "max_results": self.max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
        )
        return parse_arxiv(response.text, max_results=self.max_results).entries

    async def _fetch_nature(self, channel: str) -> list[FeedEntry]:
        suffix = channel.split(":", 1)[1] if ":" in channel else ""
        if suffix.isdigit():
            index = int(suffix)
            urls = [self.nature_feeds[index]] if 0 <= index < len(self.nature_feeds) else []
        elif suffix:
            urls = [suffix]
        else:
            urls = list(self.nature_feeds)
        results = await asyncio.gather(
            *(self._request(url) for url in urls),
            return_exceptions=True,
        )
        entries: list[FeedEntry] = []
        errors: list[Exception] = []
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                errors.append(result)
                continue
            journal = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1].replace(".rss", "") or "Nature"
            entries.extend(parse_rss(result.text, source="nature", journal=journal).entries)
        if not entries and errors:
            raise errors[0]
        return entries[: self.max_results]

    async def _fetch_biorxiv(self, *, channel_source: str = "") -> list[FeedEntry]:
        server = channel_source if channel_source in {"biorxiv", "medrxiv"} else self.biorxiv_server
        end = dt.date.today()
        start = end - dt.timedelta(days=self.biorxiv_days)
        response = await self._request(
            f"https://api.biorxiv.org/details/{server}/{start.isoformat()}/{end.isoformat()}/0"
        )
        return parse_biorxiv(response.text, server=server).entries[: self.max_results]

    async def _fetch_pubmed(self, query: str, page: int) -> list[FeedEntry]:
        term = (query or self.pubmed_query).strip()
        search = await self._request(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={
                "db": "pubmed",
                "term": term,
                "retmode": "json",
                "retmax": self.max_results,
                "retstart": (page - 1) * self.max_results,
                "sort": "pub+date",
                "api_key": self.ncbi_api_key,
            },
        )
        try:
            payload = json.loads(search.text)
            ids = payload.get("esearchresult", {}).get("idlist", [])
        except (json.JSONDecodeError, AttributeError):
            raise JournalError("invalid_response", "PubMed ESearch 响应格式无效") from None
        if not isinstance(ids, list) or not ids:
            return []
        fetch = await self._request(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={
                "db": "pubmed",
                "id": ",".join(str(item) for item in ids),
                "retmode": "xml",
                "api_key": self.ncbi_api_key,
            },
        )
        return parse_pubmed(fetch.text)

    async def fetch_article(self, article_id: str) -> FeedEntry:
        """Resolve a feed article id into a normalized entry, including full text when possible."""
        kind, key = self._split_id(article_id)
        cached = self._entry_cache.get(article_id)
        if kind == "arxiv":
            response = await self._request(
                "https://export.arxiv.org/api/query",
                params={"id_list": key, "max_results": 1},
            )
            entries = parse_arxiv(response.text, max_results=1).entries
            if entries:
                return entries[0]
            raise JournalError("not_found", f"未找到 arXiv 论文 {key!r}")
        if kind == "pubmed":
            response = await self._request(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params={"db": "pubmed", "id": key, "retmode": "xml", "api_key": self.ncbi_api_key},
            )
            entries = parse_pubmed(response.text)
            if entries:
                return entries[0]
            raise JournalError("not_found", f"未找到 PubMed 记录 {key!r}")
        if kind == "doi":
            return await self.lookup_doi(key)
        if kind in {"biorxiv", "medrxiv"}:
            return await self.lookup_doi(key)
        if kind in {"web", "http", "https"}:
            url = key if kind == "web" else f"{kind}:{key}"
            return await self._fetch_web_article(url, cached)
        raise JournalError("invalid_argument", f"无法识别的论文标识: {article_id!r}")

    async def lookup_doi(self, doi: str) -> FeedEntry:
        """Look up a DOI using the Crossref polite API."""
        clean = self._normalize_doi(doi)
        if not clean:
            raise JournalError("invalid_argument", "DOI 不能为空")
        params = {"mailto": self.crossref_mailto} if self.crossref_mailto else None
        response = await self._request(
            f"https://api.crossref.org/works/{urllib.parse.quote(clean, safe='')}",
            params=params,
        )
        try:
            entry = parse_crossref(response.text, identifier=clean)
        except JournalParseError as exc:
            raise JournalError(exc.code, str(exc)) from None
        self._entry_cache[entry.id] = entry
        return entry

    async def _fetch_web_article(self, url: str, cached: FeedEntry | None) -> FeedEntry:
        html_text = ""
        try:
            response = await self._request(url)
            html_text = response.text
        except JournalError as exc:
            if exc.code not in {"upstream_status", "service_offline"}:
                raise
            html_text = ""
        lowered = html_text.casefold()
        if not html_text or any(marker in lowered for marker in _CHALLENGE_MARKERS):
            html_text = await self._chrome_extract(url)
        try:
            entry = parse_html_article(html_text, url=url)
        except Exception as exc:
            raise JournalError("invalid_response", f"网页正文解析失败: {exc}") from None
        if cached is not None:
            entry.id = cached.id
            entry.author = entry.author or cached.author
            entry.published_at = entry.published_at or cached.published_at
            entry.doi = cached.doi
            entry.journal = cached.journal
            entry.tags = cached.tags or entry.tags
            if entry.summary.strip() == entry.title.strip() and cached.summary:
                entry.summary = cached.summary
        return entry

    async def _chrome_extract(self, url: str) -> str:
        if self.ctx is None or not self.prefer_chrome_fetch or not self.ctx.capabilities.has("web.extract.v1"):
            raise JournalError(
                "challenge_detected",
                "目标页面触发 JS/Cloudflare 校验，且 Chrome 抽取不可用",
            )
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
            raise JournalError(
                str(result.data.get("error_code") or "service_offline")
                if isinstance(result.data, Mapping)
                else "service_offline",
                result.error or "Chrome 页面抽取失败",
            )
        text = str(result.data.get("html") or result.data.get("text") or result.data.get("body") or "")
        if not text.strip():
            raise JournalError("invalid_response", "Chrome 抽取结果为空")
        return text

    @staticmethod
    def _normalize_doi(value: str) -> str:
        clean = (value or "").strip()
        if clean.lower().startswith("doi:"):
            clean = clean[4:].strip()
        if "doi.org/" in clean:
            clean = clean.split("doi.org/", 1)[1].strip()
        return clean

    @classmethod
    def _split_id(cls, article_id: str) -> tuple[str, str]:
        value = (article_id or "").strip()
        if value.startswith(("http://", "https://")):
            return "web", value
        if ":" in value:
            kind, key = value.split(":", 1)
            kind = kind.strip().lower()
            key = key.strip()
            if kind in {"arxiv", "pubmed", "doi", "biorxiv", "medrxiv", "web", "http", "https"}:
                return kind, key
        if value.lower().startswith("10.") and "/" in value:
            return "doi", value
        if value.isdigit():
            return "pubmed", value
        return "arxiv", value


def entry_to_feed_item(entry: FeedEntry) -> TextFeedItem:
    """Map a normalized entry onto the TextFeedItem contract."""
    return TextFeedItem(
        id=entry.id,
        source_id="plugin-journals",
        source_name=SOURCE_LABELS.get(entry.source, entry.source or "Journals"),
        category="journals",
        channel_id=entry.source,
        title=entry.title,
        excerpt=strip_markup(entry.summary)[:500],
        author=entry.author,
        published_at=entry.published_at,
        url=entry.url,
        doi=entry.doi,
        tags=entry.tags,
        extra={**entry.extra, "journal": entry.journal, "source": entry.source},
    )


def entry_to_article(entry: FeedEntry, *, channel_id: str = "") -> TextArticle:
    """Map a normalized entry onto the TextArticle contract."""
    paragraph_specs = entry.extra.get("paragraphs") if isinstance(entry.extra.get("paragraphs"), list) else None
    paragraphs: list[TextParagraph] = []
    if paragraph_specs:
        for index, item in enumerate(paragraph_specs):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                kind, text = str(item[0]), str(item[1]).strip()
            else:
                kind, text = "paragraph", str(item).strip()
            if text:
                paragraphs.append(TextParagraph(id=f"p{index + 1}", kind=kind, original=text))
    else:
        blocks = [block.strip() for block in re.split(r"\n\s*\n", strip_markup(entry.summary)) if block.strip()]
        for index, block in enumerate(blocks):
            paragraphs.append(
                TextParagraph(
                    id=f"p{index + 1}",
                    kind="paragraph",
                    original=block,
                )
            )
    if entry.doi:
        paragraphs.append(
            TextParagraph(
                id=f"p{len(paragraphs) + 1}",
                kind="paragraph",
                original=f"DOI: {entry.doi}",
            )
        )
    words = len(strip_markup(entry.summary).split())
    if paragraphs:
        words = sum(len(paragraph.original.split()) for paragraph in paragraphs)
    return TextArticle(
        id=entry.id,
        source_id="plugin-journals",
        source_name=SOURCE_LABELS.get(entry.source, entry.source or "Journals"),
        category="journals",
        channel_id=channel_id or entry.source,
        title=entry.title,
        subtitle=entry.journal,
        authors=entry.author,
        author=entry.author,
        published_at=entry.published_at,
        read_time=f"{max(1, round(words / 220))} min",
        url=entry.url,
        doi=entry.doi,
        journal_name=entry.journal,
        tags=entry.tags,
        cover_url=str(entry.extra.get("cover_url") or ""),
        paragraphs=paragraphs,
        extra={**entry.extra, "source": entry.source},
    )
