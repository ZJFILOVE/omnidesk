"""IMDb / TMDB metadata client for charts, search, and full detail lookup."""

from __future__ import annotations

import re
import sys
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
    ) -> None:
        self._http = http
        self._imdb_base = imdb_api_base.strip()
        self._tmdb_base = tmdb_api_base.strip()
        self._mode = (provider_mode or "auto").strip().lower()
        self._imdb_key = imdb_api_key
        self._tmdb_key = tmdb_api_key

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
