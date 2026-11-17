"""Unified Radarr / Sonarr v3 API client with dry-run gate and fail-closed errors."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .http_client import (
        SafeHttpClient,
        UpstreamClientError,
        as_float,
        as_int,
        extract_poster_url,
        join_url,
        scrub_secrets,
        validate_http_url,
        validate_media_id,
        validate_remote_path,
    )
except ImportError:
    _HERE = str(Path(__file__).resolve().parent)
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from http_client import (
        SafeHttpClient,
        UpstreamClientError,
        as_float,
        as_int,
        extract_poster_url,
        join_url,
        scrub_secrets,
        validate_http_url,
        validate_media_id,
        validate_remote_path,
    )


@dataclass(frozen=True, slots=True)
class ArrServiceSpec:
    kind: str
    label: str
    lookup_path: str
    library_path: str
    queue_path: str
    queue_params: dict[str, str]
    id_field: str
    id_prefix: str
    search_option_key: str


RADARR_SPEC = ArrServiceSpec(
    kind="radarr",
    label="Radarr",
    lookup_path="/api/v3/movie/lookup",
    library_path="/api/v3/movie",
    queue_path="/api/v3/queue",
    queue_params={"includeMovie": "true"},
    id_field="tmdbId",
    id_prefix="tmdb:",
    search_option_key="searchForMovie",
)

SONARR_SPEC = ArrServiceSpec(
    kind="sonarr",
    label="Sonarr",
    lookup_path="/api/v3/series/lookup",
    library_path="/api/v3/series",
    queue_path="/api/v3/queue",
    queue_params={"includeSeries": "true", "includeEpisode": "true"},
    id_field="tvdbId",
    id_prefix="tvdb:",
    search_option_key="searchForMissingEpisodes",
)


class ArrClient:
    """Unified Radarr / Sonarr v3 API client with dry-run gate and fail-closed errors."""

    def __init__(self, http: SafeHttpClient, spec: ArrServiceSpec, base_url: str, api_key: str) -> None:
        self._http = http
        self._spec = spec
        self._base = validate_http_url(base_url, label=f"{spec.kind}_url", empty_code="not_configured")
        if not isinstance(api_key, str) or not api_key.strip():
            raise UpstreamClientError("not_configured", f"尚未配置 {spec.label} API Key（SecretVault）")
        self._api_key = api_key.strip()
        self._headers = {"X-Api-Key": self._api_key}

    async def _get(self, path: str, query: Mapping[str, Any] | None = None) -> Any:
        _, payload = await self._http.request_json(
            "GET", join_url(self._base, path, query), headers=self._headers, secrets=(self._api_key,)
        )
        return payload

    async def _post(self, path: str, body: Mapping[str, Any]) -> Any:
        _, payload = await self._http.request_json(
            "POST", join_url(self._base, path), headers=self._headers, json_body=body, secrets=(self._api_key,)
        )
        return payload

    def _resolve_lookup_term(self, req: Mapping[str, Any]) -> str:
        imdb_id = str(req.get("imdb_id") or "").strip()
        raw_ext = req.get("tmdb_id") if self._spec.kind == "radarr" else req.get("tvdb_id")
        term = str(req.get("term") or req.get("query") or req.get("title") or "").strip()
        if imdb_id:
            return f"imdb:{validate_media_id(imdb_id, field='imdb_id')}"
        if raw_ext not in (None, ""):
            return f"{self._spec.id_prefix}{validate_media_id(raw_ext, field=self._spec.id_field)}"
        if not term:
            raise UpstreamClientError("invalid_argument", f"请提供 {self._spec.label} 查询关键词、imdb_id 或 {self._spec.id_field}")
        return term

    async def lookup(self, req: Mapping[str, Any]) -> dict[str, Any]:
        term = self._resolve_lookup_term(req)
        payload = await self._get(self._spec.lookup_path, {"term": term})
        if not isinstance(payload, list):
            raise UpstreamClientError("upstream_error", f"{self._spec.label} lookup 返回格式无效")
        items = [self._normalize_media(item) for item in payload if isinstance(item, Mapping)]
        return {"service": self._spec.kind, "term": term, "results": items, "count": len(items)}

    async def queue(self, req: Mapping[str, Any]) -> dict[str, Any]:
        page = max(1, as_int(req.get("page"), 1) or 1)
        page_size = max(1, min(as_int(req.get("page_size"), 20) or 20, 200))
        payload = await self._get(self._spec.queue_path, {"page": page, "pageSize": page_size, **self._spec.queue_params})
        if isinstance(payload, list):
            raw_records, total = payload, len(payload)
        elif isinstance(payload, Mapping) and isinstance(payload.get("records"), list):
            raw_records = payload["records"]
            total = as_int(payload.get("totalRecords"), len(raw_records)) or len(raw_records)
        else:
            raise UpstreamClientError("upstream_error", f"{self._spec.label} queue 返回格式无效")
        records = [self._normalize_queue_record(rec) for rec in raw_records if isinstance(rec, Mapping)]
        return {"service": self._spec.kind, "records": records, "total_records": total, "page": page, "page_size": page_size}

    async def add(self, req: Mapping[str, Any], *, default_quality_profile: str = "", default_root_folder: str = "") -> dict[str, Any]:
        confirm = req.get("confirm") is True
        should_submit = confirm and (req.get("dry_run") is not True)

        # Validate explicit local inputs upfront before touching any remote endpoint.
        raw_root = str(req.get("root_folder_path") or default_root_folder or "").strip()
        validated_root = validate_remote_path(raw_root, field="root_folder_path") if raw_root else ""
        for id_key in ("imdb_id", "imdbId", "title_slug", "titleSlug"):
            if req.get(id_key) not in (None, ""):
                validate_media_id(req[id_key], field=id_key)

        candidate = await self._resolve_add_candidate(req)
        ext_id = as_int(candidate.get(self._spec.id_field))
        if not ext_id or ext_id <= 0:
            raise UpstreamClientError("invalid_argument", f"缺少有效的 {self._spec.id_field}")

        in_library = bool(candidate.get("id")) or await self._exists_in_library(ext_id, str(candidate.get("imdbId") or ""))
        if should_submit and in_library:
            raise UpstreamClientError("conflict", f"{candidate.get('title') or ext_id} 已存在于 {self._spec.label} 库中", duplicate=True)

        profile_id = await self._resolve_quality_profile_id(
            req.get("quality_profile_id"),
            str(req.get("quality_profile") or default_quality_profile or "").strip(),
            strict=should_submit,
        )
        root_folder = validated_root or await self._fetch_default_root_folder(strict=should_submit)
        body = self._build_add_payload(candidate, ext_id, profile_id, root_folder, req)
        if not should_submit:
            return {
                "service": self._spec.kind, "dry_run": True, "submitted": False, "confirm_required": True,
                "already_in_library": in_library, "payload_preview": body,
            }
        created = await self._post(self._spec.library_path, body)
        if not isinstance(created, Mapping):
            raise UpstreamClientError("upstream_error", f"{self._spec.label} 添加返回格式无效")
        return {"service": self._spec.kind, "dry_run": False, "submitted": True, "item": self._normalize_media(created)}

    async def _resolve_add_candidate(self, req: Mapping[str, Any]) -> dict[str, Any]:
        ext_key = "tmdb_id" if self._spec.kind == "radarr" else "tvdb_id"
        ext_id = as_int(req.get(ext_key) or req.get(self._spec.id_field))
        title = str(req.get("title") or "").strip()
        if ext_id and title:
            return {
                self._spec.id_field: ext_id,
                "title": title,
                "titleSlug": str(req.get("title_slug") or req.get("titleSlug") or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or str(ext_id)),
                "year": as_int(req.get("year")),
                "imdbId": str(req.get("imdb_id") or req.get("imdbId") or "").strip(),
                "seasons": req.get("seasons") if isinstance(req.get("seasons"), list) else [],
            }
        term = self._resolve_lookup_term(req)
        payload = await self._get(self._spec.lookup_path, {"term": term})
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], Mapping):
            raise UpstreamClientError("not_found", f"在 {self._spec.label} 中未找到匹配片源: {term}")
        return dict(payload[0])

    async def _exists_in_library(self, ext_id: int, imdb_id: str) -> bool:
        try:
            library = await self._get(self._spec.library_path)
        except UpstreamClientError as exc:
            if exc.code == "not_found":
                return False
            raise
        if not isinstance(library, list):
            return False
        for item in library:
            if not isinstance(item, Mapping):
                continue
            if as_int(item.get(self._spec.id_field)) == ext_id:
                return True
            if imdb_id and str(item.get("imdbId") or "").strip() == imdb_id:
                return True
        return False

    async def _resolve_quality_profile_id(self, raw_id: Any, raw_name_or_id: str, *, strict: bool) -> int | None:
        explicit = as_int(raw_id)
        if explicit is not None:
            if explicit <= 0:
                raise UpstreamClientError("invalid_argument", "quality_profile_id 必须为正整数")
            return explicit
        if raw_name_or_id.isdigit():
            val = int(raw_name_or_id)
            if val <= 0:
                raise UpstreamClientError("invalid_argument", "quality_profile ID 必须为正整数")
            return val
        try:
            profiles = await self._get("/api/v3/qualityprofile")
        except UpstreamClientError as exc:
            if not strict and exc.code == "not_found":
                return None
            raise
        if not isinstance(profiles, list) or not profiles:
            if strict:
                raise UpstreamClientError("not_configured", f"{self._spec.label} 未配置可用的 quality profile")
            return None
        if raw_name_or_id:
            target = raw_name_or_id.lower()
            for p in profiles:
                if isinstance(p, Mapping) and str(p.get("name") or "").strip().lower() == target and (pid := as_int(p.get("id"))):
                    return pid
            raise UpstreamClientError("not_found", f"未找到名为 {raw_name_or_id!r} 的 Quality Profile")
        first_id = as_int(profiles[0].get("id")) if isinstance(profiles[0], Mapping) else None
        if first_id is None and strict:
            raise UpstreamClientError("not_configured", f"{self._spec.label} Quality Profile ID 无效")
        return first_id

    async def _fetch_default_root_folder(self, *, strict: bool) -> str | None:
        try:
            folders = await self._get("/api/v3/rootfolder")
        except UpstreamClientError as exc:
            if not strict and exc.code == "not_found":
                return None
            raise
        if isinstance(folders, list) and folders and isinstance(folders[0], Mapping):
            path_val = str(folders[0].get("path") or "").strip()
            if path_val:
                return validate_remote_path(path_val, field="root_folder_path")
        if strict:
            raise UpstreamClientError("not_configured", f"{self._spec.label} 未配置 root_folder_path")
        return None

    def _build_add_payload(
        self, candidate: Mapping[str, Any], ext_id: int, profile_id: int | None, root_folder: str | None, req: Mapping[str, Any]
    ) -> dict[str, Any]:
        search_default = bool(req.get("search_for_movie", True)) if self._spec.kind == "radarr" else bool(req.get("search_for_missing_episodes", True))
        payload: dict[str, Any] = {
            "title": str(candidate.get("title") or ""),
            self._spec.id_field: ext_id,
            "qualityProfileId": profile_id,
            "rootFolderPath": root_folder,
            "monitored": bool(req.get("monitored", True)),
            "addOptions": {self._spec.search_option_key: search_default},
        }
        if candidate.get("year") is not None:
            payload["year"] = as_int(candidate.get("year"))
        if candidate.get("titleSlug"):
            payload["titleSlug"] = str(candidate["titleSlug"])
        if candidate.get("imdbId"):
            payload["imdbId"] = str(candidate["imdbId"])
        if self._spec.kind == "sonarr":
            payload["seasonFolder"] = bool(req.get("season_folder", True))
            if isinstance(req.get("seasons"), list):
                payload["seasons"] = [
                    {"seasonNumber": s, "monitored": True} if isinstance(s, int) and not isinstance(s, bool) else dict(s)
                    for s in req["seasons"]
                    if (isinstance(s, int) and not isinstance(s, bool)) or isinstance(s, Mapping)
                ]
            elif isinstance(candidate.get("seasons"), list):
                payload["seasons"] = [dict(s) for s in candidate["seasons"] if isinstance(s, Mapping)]
        return payload

    def _normalize_media(self, item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": as_int(item.get("id")),
            "title": str(item.get("title") or ""),
            "year": as_int(item.get("year")),
            "tmdb_id": as_int(item.get("tmdbId")),
            "tvdb_id": as_int(item.get("tvdbId")),
            "imdb_id": str(item.get("imdbId") or ""),
            "title_slug": str(item.get("titleSlug") or ""),
            "overview": str(item.get("overview") or ""),
            "status": str(item.get("status") or ""),
            "monitored": bool(item.get("monitored", False)),
            "has_file": bool(item.get("hasFile", False)),
            "in_library": bool(as_int(item.get("id"), 0) or 0),
            "quality_profile_id": as_int(item.get("qualityProfileId")),
            "poster_url": extract_poster_url(item),
        }

    def _normalize_queue_record(self, rec: Mapping[str, Any]) -> dict[str, Any]:
        size = as_float(rec.get("size"), 0.0) or 0.0
        sizeleft = as_float(rec.get("sizeleft"), 0.0) or 0.0
        pct = round(((size - sizeleft) / size) * 100.0, 2) if size > 0 and 0.0 <= sizeleft <= size else 0.0
        messages: list[str] = []
        for entry in rec.get("statusMessages") or ():
            if isinstance(entry, str) and entry.strip():
                messages.append(entry.strip())
            elif isinstance(entry, Mapping):
                title = str(entry.get("title") or "").strip()
                sub = [str(m).strip() for m in (entry.get("messages") or ()) if str(m).strip()]
                messages.extend([f"{title}: {m}" if title else m for m in sub] or ([title] if title else []))
        out: dict[str, Any] = {
            "id": as_int(rec.get("id")),
            "title": str(rec.get("title") or ""),
            "status": str(rec.get("status") or ""),
            "tracked_download_status": str(rec.get("trackedDownloadStatus") or ""),
            "tracked_download_state": str(rec.get("trackedDownloadState") or ""),
            "protocol": str(rec.get("protocol") or ""),
            "download_client": str(rec.get("downloadClient") or ""),
            "size": size,
            "sizeleft": sizeleft,
            "progress_pct": pct,
            "timeleft": str(rec.get("timeleft") or ""),
            "estimated_completion_time": str(rec.get("estimatedCompletionTime") or ""),
            "error_message": scrub_secrets(str(rec.get("errorMessage") or "")),
            "status_messages": messages,
        }
        if self._spec.kind == "radarr":
            movie = rec.get("movie") if isinstance(rec.get("movie"), Mapping) else {}
            out["movie_id"] = as_int(rec.get("movieId") or movie.get("id"))
            out["movie_title"] = str(movie.get("title") or "")
        else:
            series = rec.get("series") if isinstance(rec.get("series"), Mapping) else {}
            episode = rec.get("episode") if isinstance(rec.get("episode"), Mapping) else {}
            out["series_id"] = as_int(rec.get("seriesId") or series.get("id"))
            out["episode_id"] = as_int(rec.get("episodeId") or episode.get("id"))
            out["series_title"] = str(series.get("title") or "")
            out["season_number"] = as_int(episode.get("seasonNumber"))
            out["episode_number"] = as_int(episode.get("episodeNumber"))
        return out
