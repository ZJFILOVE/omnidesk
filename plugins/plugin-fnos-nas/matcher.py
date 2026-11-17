"""Local and Radarr/Sonarr v3 high-bitrate media matching (`media.local_match.v1`)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from omnidesk_sdk import CapabilityResult

if __package__:
    from .lan_http import LanHttpClient, LanHttpError, validate_lan_url
    from .library import LibrarySnapshot
    from .safety import MountValidationError, resolve_contained_path
else:
    from lan_http import LanHttpClient, LanHttpError, validate_lan_url
    from library import LibrarySnapshot
    from safety import MountValidationError, resolve_contained_path

_NOISE_RE = re.compile(
    r"(?:\b(?:2160p|4k|1080p|720p|remux|bluray|blu-ray|web-dl|webrip|dovi|hdr10\+?|hdr|hevc|x265|x264|dts-hd|dts|aac|flac|imax|extended|proper|repack)\b|\bS\d{1,2}E\d{1,3}\b|\b\d{1,2}x\d{1,3}\b)",
    re.IGNORECASE,
)
_WORD_OR_CJK_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")


def _quality_score(quality_label: str, size_bytes: int) -> tuple[int, int]:
    q = quality_label.lower()
    score = 0
    if "remux" in q:
        score += 40
    if "2160p" in q or "4k" in q:
        score += 30
    elif "1080p" in q:
        score += 15
    if "dovi" in q or "hdr" in q:
        score += 10
    if "hevc" in q or "x265" in q:
        score += 5
    return score, int(size_bytes)


def _normalize_tokens(title: str) -> list[str]:
    cleaned = re.sub(r"[\[(](?:tmdb|imdb|tvdb)[^\]\)]*[\])]", " ", title, flags=re.IGNORECASE)
    cleaned = re.sub(r"\{(?:tmdb|imdb|tvdb)-[^}]*\}", " ", cleaned, flags=re.IGNORECASE)
    cleaned = _NOISE_RE.sub(" ", cleaned)
    tokens = _WORD_OR_CJK_RE.findall(cleaned.lower())
    if len(tokens) > 1 and re.fullmatch(r"(?:19\d{2}|20\d{2})", tokens[-1]):
        tokens = tokens[:-1]
    return tokens


def _strip_leading_article(tokens: list[str]) -> list[str]:
    return tokens[1:] if len(tokens) > 1 and tokens[0] in {"the", "a", "an"} else tokens


def _coerce_int(val: Any) -> int | None:
    if val is None or isinstance(val, bool) or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _score_candidate(req: Mapping[str, Any], cand: Mapping[str, Any]) -> tuple[float, str]:
    req_season, req_episode = _coerce_int(req.get("season")), _coerce_int(req.get("episode"))
    cand_season, cand_episode = _coerce_int(cand.get("season")), _coerce_int(cand.get("episode"))
    if req_season is not None and cand_season != req_season:
        return 0.0, ""
    if req_episode is not None and cand_episode != req_episode:
        return 0.0, ""

    for req_k, cand_k, score_val, label in (
        ("imdb_id", "imdb_id", 1.0, "imdb_id"),
        ("tmdb_id", "tmdb_id", 0.98, "tmdb_id"),
        ("tvdb_id", "tvdb_id", 0.98, "tvdb_id"),
    ):
        rv = str(req.get(req_k) or "").strip().lower()
        cv = str(cand.get(cand_k) or "").strip().lower()
        if rv and cv and rv != cv:
            return 0.0, ""
        if rv and cv == rv:
            return score_val, label

    req_year, cand_year = _coerce_int(req.get("year")), _coerce_int(cand.get("year"))
    if req_year is not None and cand_year is not None and abs(req_year - cand_year) > 1:
        return 0.0, ""

    req_titles = [str(req.get(k) or "").strip() for k in ("title", "original_title") if str(req.get(k) or "").strip()]
    cand_titles = [str(cand.get(k) or "").strip() for k in ("title", "original_title", "series_title") if str(cand.get(k) or "").strip()]
    if not req_titles or not cand_titles:
        return 0.0, ""

    best, reason = 0.0, ""
    for rt in req_titles:
        r_toks = _normalize_tokens(rt)
        r_core = _strip_leading_article(r_toks)
        if not r_toks:
            continue
        r_str = " ".join(r_toks)
        for ct in cand_titles:
            c_toks = _normalize_tokens(ct)
            c_core = _strip_leading_article(c_toks)
            if not c_toks:
                continue
            c_str = " ".join(c_toks)
            if r_str == c_str:
                score, rsn = 0.92, "exact_title"
            elif r_core and r_core == c_core:
                score, rsn = 0.89, "normalized_title"
            elif min(len(r_toks), len(c_toks)) >= 2:
                ratio = SequenceMatcher(None, r_str, c_str).ratio()
                union = set(r_toks) | set(c_toks)
                jaccard = len(set(r_toks) & set(c_toks)) / len(union) if union else 0.0
                if ratio >= 0.86 and jaccard >= 0.80:
                    score, rsn = round(0.80 + (ratio - 0.86) * 0.6, 4), "high_similarity_title"
                else:
                    score, rsn = 0.0, ""
            else:
                score, rsn = 0.0, ""

            if score > 0.0:
                if req_year is not None and cand_year == req_year:
                    score = min(0.97, score + 0.04)
                if req_season is not None and req_episode is not None:
                    score = min(0.97, score + 0.05)
                if score > best:
                    best, reason = score, rsn
    return (best, reason) if best >= 0.80 else (0.0, "")


def _rewrite_arr_path(remote_path: str, arr_remote_prefix: str, mount_root: Path | None) -> str:
    cleaned = remote_path.strip()
    if not cleaned or ".." in Path(cleaned).parts or "\x00" in cleaned:
        return ""
    if arr_remote_prefix and mount_root is not None:
        prefix = arr_remote_prefix.rstrip("/")
        if cleaned == prefix or cleaned.startswith(f"{prefix}/"):
            rel = cleaned[len(prefix) :].lstrip("/")
            if rel:
                try:
                    return str(resolve_contained_path(mount_root, rel, allow_symlinks=True, reject_hidden=False))
                except MountValidationError:
                    return ""
    return cleaned


async def _query_arr_candidates(
    req: Mapping[str, Any],
    cfg: Mapping[str, Any],
    *,
    radarr_key: str | None,
    sonarr_key: str | None,
    mount_root: Path | None,
    http_client: LanHttpClient,
    timeout_sec: float,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    arr_prefix = str(cfg.get("arr_remote_prefix") or "").strip()
    media_type = str(req.get("media_type") or req.get("kind") or "").strip().lower()
    has_ep = _coerce_int(req.get("season")) is not None or _coerce_int(req.get("episode")) is not None
    secrets = tuple(k for k in (radarr_key, sonarr_key) if k)

    radarr_raw = str(cfg.get("radarr_url") or "").strip()
    if radarr_raw and not has_ep and media_type not in {"tv", "series"}:
        radarr_url = validate_lan_url(radarr_raw, label="Radarr")
        headers = {"X-Api-Key": radarr_key} if radarr_key else {}
        movies = await http_client.request_json(
            "GET", f"{radarr_url}/api/v3/movie", headers=headers, timeout_sec=timeout_sec, secrets=secrets, service_label="Radarr"
        )
        for mv in movies if isinstance(movies, list) else ():
            if not isinstance(mv, Mapping) or not mv.get("hasFile"):
                continue
            mfile = mv.get("movieFile") if isinstance(mv.get("movieFile"), Mapping) else {}
            mapped = _rewrite_arr_path(str(mfile.get("path") or ""), arr_prefix, mount_root)
            if not mapped:
                continue
            cand_meta = {
                "title": str(mv.get("title") or ""), "original_title": str(mv.get("originalTitle") or ""),
                "year": mv.get("year"), "imdb_id": str(mv.get("imdbId") or "").lower(),
                "tmdb_id": str(mv.get("tmdbId") or "") if mv.get("tmdbId") else "",
            }
            score, reason = _score_candidate(req, cand_meta)
            if score >= 0.80:
                q_name = str(((mfile.get("quality") or {}).get("quality") or {}).get("name") or "")
                candidates.append({
                    "item_id": f"radarr:{mv.get('id', '')}", "title": cand_meta["title"], "path": mapped,
                    "play_url": Path(mapped).as_uri() if Path(mapped).is_absolute() else "",
                    "size_bytes": int(mfile.get("size") or 0), "quality_label": q_name,
                    "season": None, "episode": None, "imdb_id": cand_meta["imdb_id"], "tmdb_id": cand_meta["tmdb_id"],
                    "confidence": round(score, 4), "match_source": "radarr", "match_reason": reason,
                })

    sonarr_raw = str(cfg.get("sonarr_url") or "").strip()
    if sonarr_raw and media_type != "movie":
        sonarr_url = validate_lan_url(sonarr_raw, label="Sonarr")
        headers = {"X-Api-Key": sonarr_key} if sonarr_key else {}
        series_list = await http_client.request_json(
            "GET", f"{sonarr_url}/api/v3/series", headers=headers, timeout_sec=timeout_sec, secrets=secrets, service_label="Sonarr"
        )
        for sr in series_list if isinstance(series_list, list) else ():
            if not isinstance(sr, Mapping) or sr.get("id") is None:
                continue
            series_probe = {
                "title": str(sr.get("title") or ""), "imdb_id": str(sr.get("imdbId") or "").lower(),
                "tmdb_id": str(sr.get("tmdbId") or "") if sr.get("tmdbId") else "",
                "tvdb_id": str(sr.get("tvdbId") or "") if sr.get("tvdbId") else "", "year": sr.get("year"),
            }
            sr_req = {k: v for k, v in req.items() if k not in {"season", "episode"}}
            sr_score, sr_reason = _score_candidate(sr_req, series_probe)
            if sr_score < 0.80:
                continue
            sid = sr["id"]
            episodes = await http_client.request_json(
                "GET", f"{sonarr_url}/api/v3/episode", headers=headers, query={"seriesId": sid}, timeout_sec=timeout_sec, secrets=secrets, service_label="Sonarr"
            )
            ep_files = await http_client.request_json(
                "GET", f"{sonarr_url}/api/v3/episodefile", headers=headers, query={"seriesId": sid}, timeout_sec=timeout_sec, secrets=secrets, service_label="Sonarr"
            )
            file_map = {int(f["id"]): f for f in (ep_files or []) if isinstance(f, Mapping) and f.get("id") is not None}
            for ep in episodes or ():
                if not isinstance(ep, Mapping) or not ep.get("hasFile"):
                    continue
                ef = file_map.get(int(ep.get("episodeFileId") or 0))
                if not ef:
                    continue
                mapped = _rewrite_arr_path(str(ef.get("path") or ""), arr_prefix, mount_root)
                if not mapped:
                    continue
                score, reason = _score_candidate(req, {**series_probe, "season": ep.get("seasonNumber"), "episode": ep.get("episodeNumber")})
                if score >= 0.80:
                    q_name = str(((ef.get("quality") or {}).get("quality") or {}).get("name") or "")
                    s_num, e_num = _coerce_int(ep.get("seasonNumber")), _coerce_int(ep.get("episodeNumber"))
                    ep_tag = f"S{s_num:02d}E{e_num:02d}" if s_num is not None and e_num is not None else ""
                    candidates.append({
                        "item_id": f"sonarr:{sid}:{ep.get('id', '')}",
                        "title": f"{series_probe['title']} · {ep_tag}".strip(" ·"), "path": mapped,
                        "play_url": Path(mapped).as_uri() if Path(mapped).is_absolute() else "",
                        "size_bytes": int(ef.get("size") or 0), "quality_label": q_name,
                        "season": s_num, "episode": e_num, "imdb_id": series_probe["imdb_id"], "tmdb_id": "",
                        "confidence": round(score, 4), "match_source": "sonarr", "match_reason": reason or sr_reason,
                    })
    return candidates


async def run_local_match(
    req: Mapping[str, Any],
    snapshot: LibrarySnapshot,
    cfg: Mapping[str, Any],
    *,
    radarr_key: str | None,
    sonarr_key: str | None,
    http_client: LanHttpClient,
    timeout_sec: float,
) -> CapabilityResult:
    """Match title/imdb_id/tmdb_id/season/episode against local NAS and optional Radarr/Sonarr."""
    if not any(str(req.get(k) or "").strip() for k in ("title", "original_title", "imdb_id", "tmdb_id", "tvdb_id")):
        return CapabilityResult.fail("缺少匹配参数（title / imdb_id / tmdb_id）", error_code="invalid_argument")

    has_mount = bool(str(cfg.get("mount_root") or "").strip())
    has_arr = bool(str(cfg.get("radarr_url") or "").strip() or str(cfg.get("sonarr_url") or "").strip())
    if not has_mount and not has_arr:
        return CapabilityResult.fail(
            "尚未配置本地挂载目录 (mount_root) 或 Radarr/Sonarr 服务",
            data={"matched": False, "best_match": None, "candidates": [], "confidence": 0.0},
            error_code="not_configured",
        )
    if has_mount and snapshot.error_code and not has_arr:
        return CapabilityResult.fail(
            snapshot.error,
            data={"matched": False, "best_match": None, "candidates": [], "confidence": 0.0},
            error_code=snapshot.error_code,
        )

    candidates: list[dict[str, Any]] = []
    for rec in snapshot.item_records:
        if rec.get("category") not in {"电影", "剧集"}:
            continue
        score, reason = _score_candidate(req, {**rec, "series_title": rec["title"].split(" · ", 1)[0]})
        if score >= 0.80:
            candidates.append({
                "item_id": rec["id"], "title": rec["title"], "path": rec["path"], "play_url": rec["play_url"],
                "size_bytes": rec["size_bytes"], "quality_label": rec["quality_label"],
                "season": rec.get("season"), "episode": rec.get("episode"),
                "imdb_id": rec.get("imdb_id", ""), "tmdb_id": rec.get("tmdb_id", ""),
                "confidence": round(score, 4), "match_source": "local_mount", "match_reason": reason,
            })

    arr_warning = ""
    if has_arr and req.get("include_arr", True) is not False:
        try:
            candidates.extend(
                await _query_arr_candidates(
                    req, cfg, radarr_key=radarr_key, sonarr_key=sonarr_key,
                    mount_root=snapshot.mount_root, http_client=http_client, timeout_sec=timeout_sec,
                )
            )
        except LanHttpError as exc:
            if not candidates or exc.error_code == "permission_denied":
                return CapabilityResult.fail(
                    str(exc),
                    data={"matched": False, "best_match": None, "candidates": [], "confidence": 0.0, "http_status": exc.http_status},
                    error_code=exc.error_code,
                )
            arr_warning = str(exc)

    candidates.sort(
        key=lambda c: (float(c["confidence"]), *_quality_score(str(c["quality_label"]), int(c["size_bytes"]))),
        reverse=True,
    )
    return CapabilityResult.ok({
        "matched": bool(candidates), "best_match": candidates[0] if candidates else None,
        "candidates": candidates, "confidence": float(candidates[0]["confidence"]) if candidates else 0.0,
        "arr_warning": arr_warning,
    })
