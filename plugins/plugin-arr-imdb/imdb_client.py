"""IMDb / TMDB metadata client for charts, search, and full detail lookup."""

from __future__ import annotations

import html as html_lib
import json
import re
import sys
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    from .http_client import (
        SafeHttpClient,
        UpstreamClientError,
        join_url,
        normalize_category,
        validate_http_url,
        validate_media_id,
    )
except ImportError:
    _HERE = str(Path(__file__).resolve().parent)
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from http_client import (
        SafeHttpClient,
        UpstreamClientError,
        join_url,
        normalize_category,
        validate_http_url,
        validate_media_id,
    )

_IMDB_ID_RE = re.compile(r"^tt\d{5,12}\Z")
_TMDB_PREFIX_RE = re.compile(r"^(?:tmdb:)?(?:(movie|tv|series):)?(\d{1,12})\Z")
_NEXT_DATA_RE = re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)
_LD_JSON_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)
_PUBLIC_CHART_PATHS: dict[str, str] = {
    "popular": "https://www.imdb.com/chart/moviemeter/",
    "top_rated": "https://www.imdb.com/chart/top/",
    "upcoming": "https://www.imdb.com/chart/moviemeter/",
    "tv_popular": "https://www.imdb.com/chart/tvmeter/",
    "tv_top_rated": "https://www.imdb.com/chart/toptv/",
}


class ImdbTmdbClient:
    """Client for user-configured IMDb-compatible or TMDB v3 JSON APIs."""

    def __init__(
        self,
        http: SafeHttpClient,
        *,
        imdb_api_base: str = "",
        tmdb_api_base: str = "",
        provider_mode: str = "auto",
        imdb_api_key: str = "",
        tmdb_api_key: str = "",
        public_fallback: bool = False,
        language: str = "zh-CN",
    ) -> None:
        self._http = http
        self._imdb_base = imdb_api_base.strip()
        self._tmdb_base = tmdb_api_base.strip()
        self._mode = (provider_mode or "auto").strip().lower()
        self._imdb_key = imdb_api_key
        self._tmdb_key = tmdb_api_key
        self._public_fallback = bool(public_fallback)
        self._language = (language or "zh-CN").strip()

    @property
    def has_custom_base(self) -> bool:
        return bool(self._imdb_base or self._tmdb_base)

    def _resolve_endpoint(self) -> tuple[str, str, str]:
        if self._mode == "tmdb":
            raw_base = self._tmdb_base or self._imdb_base
            key = self._tmdb_key or self._imdb_key
            mode = "tmdb"
        elif self._mode == "imdb":
            raw_base = self._imdb_base or self._tmdb_base
            key = self._imdb_key or self._tmdb_key
            mode = "imdb"
        elif self._imdb_base:
            raw_base = self._imdb_base
            key = self._imdb_key or self._tmdb_key
            mode = "tmdb" if "themoviedb.org" in raw_base.lower() else "auto"
        else:
            raw_base = self._tmdb_base
            key = self._tmdb_key or self._imdb_key
            mode = "tmdb"
        base = validate_http_url(raw_base, label="imdb_api_base / tmdb_api_base", empty_code="not_configured")
        return base, key, mode

    def _auth_for(self, key: str, mode: str) -> tuple[dict[str, str], dict[str, str]]:
        if not key:
            return {}, {}
        if mode == "tmdb" and "." not in key:
            return {"X-Api-Key": key}, {"api_key": key}
        return {"Authorization": f"Bearer {key}", "X-Api-Key": key}, {}

    async def _get_with_404_fallback(self, base: str, key: str, mode: str, candidates: list[tuple[str, dict[str, Any]]]) -> Any:
        headers, auth_q = self._auth_for(key, mode)
        secrets = [s for s in (self._imdb_key, self._tmdb_key) if s]
        last_err: UpstreamClientError | None = None
        for path, params in candidates:
            url = join_url(base, path, {**params, **auth_q})
            try:
                _, payload = await self._http.request_json("GET", url, headers=headers, secrets=secrets)
                return payload
            except UpstreamClientError as exc:
                if exc.code == "not_found":
                    last_err = exc
                    continue
                raise
        raise last_err or UpstreamClientError("not_found", "未找到对应媒体资源")

    async def fetch_items(self, category: str = "popular", query: str = "", page: int = 1) -> list[dict[str, Any]]:
        norm_cat = normalize_category(category, query)
        clean_query = query.strip()
        if norm_cat == "search" and not clean_query:
            return []
        if not self.has_custom_base and self._public_fallback:
            return await self._fetch_public_items(norm_cat, clean_query, max(1, int(page)))
        base, key, mode = self._resolve_endpoint()
        page_num = max(1, int(page))
        tmdb_map = {
            "popular": ("/movie/popular", {"page": page_num}),
            "top_rated": ("/movie/top_rated", {"page": page_num}),
            "upcoming": ("/movie/upcoming", {"page": page_num}),
            "tv_popular": ("/tv/popular", {"page": page_num}),
            "tv_top_rated": ("/tv/top_rated", {"page": page_num}),
            "search": ("/search/multi", {"query": clean_query, "page": page_num}),
        }
        imdb_candidates = (
            [("/search", {"q": clean_query, "query": clean_query, "page": page_num}), ("/imdb/search", {"q": clean_query, "page": page_num})]
            if norm_cat == "search"
            else [(f"/charts/{norm_cat}", {"page": page_num}), (f"/imdb/{norm_cat}", {"page": page_num})]
        )
        candidates = [tmdb_map[norm_cat]] if mode == "tmdb" else (imdb_candidates if mode == "imdb" else [*imdb_candidates, tmdb_map[norm_cat]])
        payload = await self._get_with_404_fallback(base, key, mode, candidates)
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, Mapping):
            rows = payload.get("results") if isinstance(payload.get("results"), list) else (
                payload.get("items") if isinstance(payload.get("items"), list) else payload.get("titles")
            )
        else:
            rows = None
        if not isinstance(rows, list):
            raise UpstreamClientError("upstream_error", "榜单接口返回格式无效，期望列表")
        return [dict(r) for r in rows if isinstance(r, Mapping)]

    async def fetch_detail(self, item_id: str, media_type: str = "") -> dict[str, Any]:
        safe_id = validate_media_id(item_id, field="item_id")
        if not self.has_custom_base and self._public_fallback:
            return await self._fetch_public_detail(safe_id)
        base, key, mode = self._resolve_endpoint()
        is_imdb = bool(_IMDB_ID_RE.match(safe_id))
        m_tmdb = _TMDB_PREFIX_RE.match(safe_id)
        mtype_hint = (m_tmdb.group(1) if m_tmdb and m_tmdb.group(1) else media_type or "").lower()
        numeric_id = m_tmdb.group(2) if m_tmdb else None
        is_tv = mtype_hint in {"tv", "series"}

        if mode == "imdb":
            return await self._fetch_detail_dict(base, key, mode, [(f"/title/{safe_id}", {}), (f"/titles/{safe_id}", {}), (f"/imdb/title/{safe_id}", {})])

        if mode == "auto" and is_imdb:
            try:
                return await self._fetch_detail_dict(base, key, mode, [(f"/title/{safe_id}", {}), (f"/titles/{safe_id}", {}), (f"/imdb/title/{safe_id}", {})])
            except UpstreamClientError as exc:
                if exc.code != "not_found":
                    raise
                return await self._fetch_tmdb_by_imdb(base, key, safe_id)

        if is_imdb:
            return await self._fetch_tmdb_by_imdb(base, key, safe_id)
        target_id = numeric_id or safe_id
        tmdb_path = f"/tv/{target_id}" if is_tv else f"/movie/{target_id}"
        candidates = [(tmdb_path, {"append_to_response": "credits,external_ids"})]
        if mode == "auto":
            candidates.extend([(f"/title/{safe_id}", {}), (f"/titles/{safe_id}", {})])
        return await self._fetch_detail_dict(base, key, mode, candidates)

    async def _fetch_tmdb_by_imdb(self, base: str, key: str, imdb_id: str) -> dict[str, Any]:
        found = await self._fetch_detail_dict(base, key, "tmdb", [(f"/find/{imdb_id}", {"external_source": "imdb_id"})])
        movies = found.get("movie_results") or []
        tvs = found.get("tv_results") or []
        if movies and isinstance(movies[0], Mapping) and movies[0].get("id"):
            detail = await self._fetch_detail_dict(base, key, "tmdb", [(f"/movie/{movies[0]['id']}", {"append_to_response": "credits,external_ids"})])
            detail.setdefault("imdb_id", imdb_id)
            return detail
        if tvs and isinstance(tvs[0], Mapping) and tvs[0].get("id"):
            detail = await self._fetch_detail_dict(base, key, "tmdb", [(f"/tv/{tvs[0]['id']}", {"append_to_response": "credits,external_ids"})])
            detail.setdefault("imdb_id", imdb_id)
            detail.setdefault("media_type", "tv")
            return detail
        raise UpstreamClientError("not_found", f"未找到 IMDb ID {imdb_id} 对应的影视条目")

    async def _fetch_detail_dict(self, base: str, key: str, mode: str, candidates: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        payload = await self._get_with_404_fallback(base, key, mode, candidates)
        if not isinstance(payload, Mapping) or not payload:
            raise UpstreamClientError("not_found", "未找到对应影视详情")
        return dict(payload)

    def _public_headers(self) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": f"{self._language},zh;q=0.8,en;q=0.6",
        }

    async def _fetch_public_items(self, norm_cat: str, clean_query: str, page: int) -> list[dict[str, Any]]:
        if norm_cat == "search":
            encoded = urllib.parse.quote(clean_query, safe="")
            url = f"https://v3.sg.media-imdb.com/suggestion/x/{encoded}.json"
            _, payload = await self._http.request_json(
                "GET", url, headers=self._public_headers(), allow_proxy_for_wan=True
            )
            rows = payload.get("d") if isinstance(payload, Mapping) else None
            if not isinstance(rows, list):
                return []
            out: list[dict[str, Any]] = []
            for entry in rows:
                if not isinstance(entry, Mapping):
                    continue
                tt_id = str(entry.get("id") or "").strip()
                if not _IMDB_ID_RE.match(tt_id):
                    continue
                q_kind = str(entry.get("q") or "").lower()
                mtype = "tv" if ("tv" in q_kind or "series" in q_kind) else "movie"
                img = entry.get("i") if isinstance(entry.get("i"), Mapping) else {}
                out.append(
                    {
                        "id": tt_id,
                        "imdb_id": tt_id,
                        "title": str(entry.get("l") or tt_id),
                        "year": entry.get("y"),
                        "media_type": mtype,
                        "poster": str(img.get("imageUrl") or ""),
                        "overview": str(entry.get("s") or ""),
                    }
                )
            return out

        chart_url = _PUBLIC_CHART_PATHS.get(norm_cat, _PUBLIC_CHART_PATHS["popular"])
        _, _, body = await self._http.request_raw(
            "GET",
            chart_url,
            headers={**self._public_headers(), "Accept": "text/html,application/xhtml+xml"},
            allow_proxy_for_wan=True,
        )
        rows = self._parse_public_chart_html(body, is_tv=norm_cat.startswith("tv_"))
        if not rows:
            raise UpstreamClientError("upstream_error", "IMDb 公开榜单页面未解析到条目")
        start = (page - 1) * 25
        return rows[start : start + 25]

    @staticmethod
    def _parse_public_chart_html(body: str, *, is_tv: bool) -> list[dict[str, Any]]:
        m_next = _NEXT_DATA_RE.search(body)
        if m_next:
            try:
                nd = json.loads(m_next.group(1))
                edges = (
                    nd.get("props", {})
                    .get("pageProps", {})
                    .get("pageData", {})
                    .get("chartTitles", {})
                    .get("edges", [])
                )
                if isinstance(edges, list) and edges:
                    parsed: list[dict[str, Any]] = []
                    for edge in edges:
                        node = edge.get("node") if isinstance(edge, Mapping) else None
                        if not isinstance(node, Mapping):
                            continue
                        tt_id = str(node.get("id") or "").strip()
                        if not tt_id:
                            continue
                        title_text = ((node.get("titleText") or {}).get("text") or "") if isinstance(node.get("titleText"), Mapping) else ""
                        year = ((node.get("releaseYear") or {}).get("year")) if isinstance(node.get("releaseYear"), Mapping) else None
                        rating = ((node.get("ratingsSummary") or {}).get("aggregateRating")) if isinstance(node.get("ratingsSummary"), Mapping) else None
                        poster = ((node.get("primaryImage") or {}).get("url") or "") if isinstance(node.get("primaryImage"), Mapping) else ""
                        plot = (((node.get("plot") or {}).get("plotText") or {}).get("plainText") or "") if isinstance(node.get("plot"), Mapping) else ""
                        parsed.append(
                            {
                                "id": tt_id,
                                "imdb_id": tt_id,
                                "title": str(title_text or tt_id),
                                "year": year,
                                "rating": rating,
                                "media_type": "tv" if is_tv else "movie",
                                "poster": str(poster),
                                "overview": str(plot),
                            }
                        )
                    if parsed:
                        return parsed
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        m_ld = _LD_JSON_RE.search(body)
        if m_ld:
            try:
                ld = json.loads(m_ld.group(1))
                elements = ld.get("itemListElement") if isinstance(ld, Mapping) else None
                if isinstance(elements, list):
                    parsed = []
                    for entry in elements:
                        item = entry.get("item") if isinstance(entry, Mapping) else None
                        if not isinstance(item, Mapping):
                            continue
                        url_val = str(item.get("url") or "")
                        m_id = re.search(r"(tt\d{5,12})", url_val)
                        if not m_id:
                            continue
                        tt_id = m_id.group(1)
                        rating_val = ((item.get("aggregateRating") or {}).get("ratingValue")) if isinstance(item.get("aggregateRating"), Mapping) else None
                        parsed.append(
                            {
                                "id": tt_id,
                                "imdb_id": tt_id,
                                "title": html_lib.unescape(str(item.get("name") or tt_id)),
                                "rating": rating_val,
                                "media_type": "tv" if is_tv else "movie",
                                "poster": str(item.get("image") or ""),
                                "overview": html_lib.unescape(str(item.get("description") or "")),
                            }
                        )
                    if parsed:
                        return parsed
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        return []

    async def _fetch_public_detail(self, safe_id: str) -> dict[str, Any]:
        if not _IMDB_ID_RE.match(safe_id):
            raise UpstreamClientError("not_found", f"公开 IMDb 详情仅支持 tt 编号，收到: {safe_id}")
        url = f"https://www.imdb.com/title/{safe_id}/"
        _, _, body = await self._http.request_raw(
            "GET",
            url,
            headers={**self._public_headers(), "Accept": "text/html,application/xhtml+xml"},
            allow_proxy_for_wan=True,
        )
        m_ld = _LD_JSON_RE.search(body)
        if not m_ld:
            raise UpstreamClientError("not_found", f"未找到 IMDb 条目详情: {safe_id}")
        try:
            ld = json.loads(m_ld.group(1))
        except json.JSONDecodeError:
            raise UpstreamClientError("upstream_error", "IMDb 详情页 JSON-LD 解析失败") from None
        if not isinstance(ld, Mapping):
            raise UpstreamClientError("not_found", f"未找到 IMDb 条目详情: {safe_id}")
        at_type = str(ld.get("@type") or "").lower()
        is_tv = "series" in at_type or "tv" in at_type
        date_pub = str(ld.get("datePublished") or "")
        year = int(date_pub[:4]) if len(date_pub) >= 4 and date_pub[:4].isdigit() else None
        rating = ((ld.get("aggregateRating") or {}).get("ratingValue")) if isinstance(ld.get("aggregateRating"), Mapping) else None
        return {
            "id": safe_id,
            "imdb_id": safe_id,
            "title": html_lib.unescape(str(ld.get("name") or safe_id)),
            "year": year,
            "rating": rating,
            "media_type": "tv" if is_tv else "movie",
            "poster": str(ld.get("image") or ""),
            "overview": html_lib.unescape(str(ld.get("description") or "")),
            "genres": ld.get("genre") if isinstance(ld.get("genre"), list) else [],
        }
