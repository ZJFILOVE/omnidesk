"""OmniDesk v2 plugin: IMDb/TMDB charts & details with fail-closed Radarr/Sonarr queue management."""

from __future__ import annotations

import base64
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, WorkspaceTabSpec

try:
    from .arr_client import RADARR_SPEC, SONARR_SPEC, ArrClient, QbittorrentClient
    from .http_client import SafeHttpClient, UpstreamClientError, scrub_secrets, validate_http_url, validate_media_id
    from .imdb_client import ImdbTmdbClient
    from .sources import ArrImdbMediaSource
except ImportError:
    _HERE = str(Path(__file__).resolve().parent)
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from arr_client import RADARR_SPEC, SONARR_SPEC, ArrClient, QbittorrentClient
    from http_client import SafeHttpClient, UpstreamClientError, scrub_secrets, validate_http_url, validate_media_id
    from imdb_client import ImdbTmdbClient
    from sources import ArrImdbMediaSource

_SUPPORTED_ACTIONS = (
    "categories",
    "fetch_items",
    "fetch_detail",
    "cover",
    "local_status",
    "radarr_lookup",
    "radarr_add",
    "radarr_queue",
    "sonarr_lookup",
    "sonarr_add",
    "sonarr_queue",
    "download_preview",
    "download_submit",
    "download_status",
)


def _format_bytes(size: float | int) -> str:
    val = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} TB"


def _format_eta(seconds: int) -> str:
    if seconds <= 0 or seconds >= 8640000:
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class ArrImdbPlugin(BasePlugin):
    """Workspace MediaSource plugin for IMDb/TMDB discovery, local match check, and Radarr/Sonarr control."""

    def __init__(self) -> None:
        self._source = ArrImdbMediaSource(self._build_imdb_client)
        self._last_status: str = "idle"
        self._recent_items: list[dict[str, Any]] = []
        self._last_queue: list[dict[str, Any]] = []
        self._last_download_items: list[dict[str, Any]] = []
        self._last_download_summary: str = "未刷新"

    async def on_load(self, ctx: PluginContext) -> None:
        """Register MediaSource, declarative workspace tab, and media.source.v1 capability non-blockingly."""
        await super().on_load(ctx)
        ctx.register_media_source(self._source)
        ctx.register_workspace_tab(self._build_workspace_tab_spec())
        try:
            ctx.register_capability("media.source.v1", self.handle_capability)
        except Exception as exc:
            if getattr(exc, "code", "") != "conflict" and type(exc).__name__ != "CapabilityConflictError":
                raise

    async def on_unload(self) -> None:
        """Clear transient in-memory state when unloading."""
        self._last_status = "unloaded"
        self._recent_items.clear()
        self._last_queue.clear()
        self._last_download_items.clear()
        self._last_download_summary = "未刷新"

    @staticmethod
    def _has_public_imdb_fallback(cfg: Mapping[str, Any]) -> bool:
        return bool(
            cfg.get("imdb_public_enabled")
            or str(cfg.get("imdb_language") or "").strip()
            or str(cfg.get("radarr_url") or "").strip()
            or str(cfg.get("sonarr_url") or "").strip()
        )

    @staticmethod
    def _default_download_mode(cfg: Mapping[str, Any]) -> str:
        raw = str(cfg.get("download_mode") or "").strip().lower()
        if raw in {"speed", "size"}:
            return raw
        if cfg.get("seeders_first_enabled") is False:
            return "size"
        return "speed"

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight config readiness flags without making network calls or exposing secrets."""
        cfg = self._read_config()
        plugin_id = self.ctx.plugin_id if self.ctx is not None else "plugin-arr-imdb"
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "source_id": self._source.source_id,
            "imdb_configured": bool(
                str(cfg.get("imdb_api_base") or "").strip()
                or str(cfg.get("tmdb_api_base") or "").strip()
                or self._has_public_imdb_fallback(cfg)
            ),
            "radarr_configured": bool(str(cfg.get("radarr_url") or "").strip()) and bool(self._secret("radarr_api_key")),
            "sonarr_configured": bool(str(cfg.get("sonarr_url") or "").strip()) and bool(self._secret("sonarr_api_key")),
            "qbittorrent_configured": bool(str(cfg.get("qbittorrent_url") or "").strip()),
            "dry_run_default": bool(cfg.get("dry_run", True)),
        }

    def _read_config(self) -> dict[str, Any]:
        if self.ctx is None:
            return {"dry_run": True, "request_timeout_sec": 10, "imdb_provider": "auto"}
        return dict(self.ctx.get_config())

    def _secret(self, key: str) -> str:
        if self.ctx is None:
            return ""
        return str(self.ctx.get_secret(key, "") or "").strip()

    def _all_secrets(self) -> list[str]:
        keys = ("imdb_api_key", "tmdb_api_key", "radarr_api_key", "sonarr_api_key", "qbittorrent_password")
        return [val for k in keys if (val := self._secret(k))]

    def _http_client(self) -> SafeHttpClient:
        raw_timeout = self._read_config().get("request_timeout_sec", 10)
        try:
            timeout = float(raw_timeout) if not isinstance(raw_timeout, bool) else 10.0
        except (TypeError, ValueError):
            timeout = 10.0
        return SafeHttpClient(timeout_sec=timeout)

    def _build_imdb_client(self) -> ImdbTmdbClient:
        cfg = self._read_config()
        return ImdbTmdbClient(
            self._http_client(),
            imdb_api_base=str(cfg.get("imdb_api_base") or ""),
            tmdb_api_base=str(cfg.get("tmdb_api_base") or ""),
            provider_mode=str(cfg.get("imdb_provider") or "auto"),
            imdb_api_key=self._secret("imdb_api_key"),
            tmdb_api_key=self._secret("tmdb_api_key"),
            public_fallback=self._has_public_imdb_fallback(cfg),
            language=str(cfg.get("imdb_language") or "zh-CN"),
        )

    def _fail(self, message: str, *, error_code: str, data: Mapping[str, Any] | None = None) -> CapabilityResult:
        safe_msg = scrub_secrets(message, self._all_secrets())
        self._last_status = f"error:{error_code}"
        return CapabilityResult.fail(safe_msg, data=data, error_code=error_code)

    async def handle_capability(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Dispatch `media.source.v1` actions for charts, details, local match, and Radarr/Sonarr."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "").strip()
        try:
            if action == "categories":
                return CapabilityResult.ok({"source_id": self._source.source_id, "categories": self._source.categories()})
            if action == "fetch_items":
                return await self._action_fetch_items(payload)
            if action == "fetch_detail":
                return await self._action_fetch_detail(payload)
            if action == "cover":
                return await self._action_cover(payload)
            if action == "local_status":
                return await self._action_local_status(payload)
            if action in {"radarr_lookup", "radarr_add", "radarr_queue"}:
                return await self._action_arr("radarr", action.removeprefix("radarr_"), payload)
            if action in {"sonarr_lookup", "sonarr_add", "sonarr_queue"}:
                return await self._action_arr("sonarr", action.removeprefix("sonarr_"), payload)
            if action == "download_preview":
                return await self._action_download(payload, force_confirm=False)
            if action == "download_submit":
                return await self._action_download(payload, force_confirm=True)
            if action == "download_status":
                return await self._action_download_status(payload)
            return self._fail(
                f"不支持的动作: {action!r}",
                error_code="invalid_argument",
                data={"supported_actions": list(_SUPPORTED_ACTIONS)},
            )
        except UpstreamClientError as exc:
            extra = {"duplicate": True, "already_exists": True} if exc.duplicate else {}
            if exc.status_code is not None:
                extra["http_status"] = exc.status_code
            return self._fail(exc.message, error_code=exc.code, data=extra)
        except Exception as exc:
            return self._fail(str(exc) or type(exc).__name__, error_code="upstream_error")

    def _make_arr_client(self, service: str) -> ArrClient:
        cfg = self._read_config()
        spec = RADARR_SPEC if service == "radarr" else SONARR_SPEC
        return ArrClient(
            self._http_client(),
            spec,
            str(cfg.get(f"{service}_url") or ""),
            self._secret(f"{service}_api_key"),
        )

    async def _arr_fallback_items(self, category: str, query: str) -> list[dict[str, Any]]:
        cfg = self._read_config()
        prefer_sonarr = category.startswith("tv")
        order = ("sonarr", "radarr") if prefer_sonarr else ("radarr", "sonarr")
        results: list[dict[str, Any]] = []
        for svc in order:
            if not (str(cfg.get(f"{svc}_url") or "").strip() and self._secret(f"{svc}_api_key")):
                continue
            client = self._make_arr_client(svc)
            try:
                if query.strip():
                    data = await client.lookup({"term": query.strip()})
                    rows = data.get("results", [])
                else:
                    rows = await client.library_list(limit=25)
                for row in rows:
                    imdb_id = str(row.get("imdb_id") or "").strip()
                    ext_id = row.get("tmdb_id") if svc == "radarr" else row.get("tvdb_id")
                    item_id = imdb_id or (f"tmdb:{ext_id}" if svc == "radarr" and ext_id else f"tvdb:{ext_id}" if ext_id else str(row.get("id") or ""))
                    year = row.get("year")
                    results.append(
                        {
                            "id": item_id,
                            "title": row.get("title") or item_id,
                            "subtitle": f"{year or ''} · {svc.capitalize()} · {'已有文件' if row.get('has_file') else '待下载'}".strip(" ·"),
                            "source_id": self._source.source_id,
                            "category": category or "popular",
                            "kind": "video",
                            "cover_url": row.get("poster_url") or "",
                            "summary": row.get("overview") or "",
                            "extra": {"imdb_id": imdb_id, "tmdb_id": row.get("tmdb_id"), "tvdb_id": row.get("tvdb_id"), "service": svc, "year": year},
                        }
                    )
            except UpstreamClientError:
                continue
        return results

    async def _action_cover(self, req: Mapping[str, Any]) -> CapabilityResult:
        url = validate_http_url(req.get("url"), label="cover_url", empty_code="invalid_argument")
        _, headers, raw = await self._http_client().request_bytes(
            "GET",
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": "https://www.imdb.com/",
            },
            timeout_sec=12.0,
            allow_proxy_for_wan=True,
        )
        ctype = str(headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            if raw.lstrip().startswith((b"<html", b"<!doctype", b"<HTML", b"<!DOCTYPE")):
                raise UpstreamClientError("upstream_error", "封面返回了 HTML 而非图片")
            ctype = "image/jpeg"
        if not raw:
            raise UpstreamClientError("upstream_error", "封面图片为空")
        encoded = base64.b64encode(raw).decode("ascii")
        return CapabilityResult.ok({"data_url": f"data:{ctype};base64,{encoded}"})

    async def _action_fetch_items(self, req: Mapping[str, Any]) -> CapabilityResult:
        category = str(req.get("category") or "")
        query = str(req.get("query") or "")
        raw_page = req.get("page", 1)
        if isinstance(raw_page, bool) or not isinstance(raw_page, int) or raw_page < 1:
            return self._fail("page 必须为正整数", error_code="invalid_argument")
        cfg = self._read_config()
        has_custom_imdb = bool(str(cfg.get("imdb_api_base") or "").strip() or str(cfg.get("tmdb_api_base") or "").strip())
        try:
            items = await self._source.fetch_items(category=category, query=query, page=raw_page)
            dumped = [item.model_dump() for item in items]
        except UpstreamClientError:
            if has_custom_imdb or not self._has_public_imdb_fallback(cfg):
                raise
            dumped = await self._arr_fallback_items(category, query)
            if not dumped:
                raise
        self._recent_items = dumped[:20]
        self._last_status = f"items:{len(dumped)}"
        return CapabilityResult.ok(
            {"source_id": self._source.source_id, "category": category or ("search" if query.strip() else "popular"), "items": dumped, "count": len(dumped)}
        )

    async def _action_fetch_detail(self, req: Mapping[str, Any]) -> CapabilityResult:
        raw_id = req.get("item_id") or req.get("id") or req.get("imdb_id") or req.get("tmdb_id")
        if raw_id is None or str(raw_id).strip() == "":
            return self._fail("fetch_detail 缺少 item_id", error_code="invalid_argument")
        media_type = str(req.get("media_type") or "").strip()
        cfg = self._read_config()
        has_custom_imdb = bool(str(cfg.get("imdb_api_base") or "").strip() or str(cfg.get("tmdb_api_base") or "").strip())
        try:
            detail = await self._source.fetch_detail(str(raw_id), media_type=media_type)
        except UpstreamClientError:
            if has_custom_imdb or not self._has_public_imdb_fallback(cfg):
                raise
            svc_order = ("sonarr", "radarr") if media_type in {"tv", "series"} else ("radarr", "sonarr")
            detail = {}
            for svc in svc_order:
                if not (str(cfg.get(f"{svc}_url") or "").strip() and self._secret(f"{svc}_api_key")):
                    continue
                try:
                    lookup_req = {"imdb_id": str(raw_id)} if str(raw_id).startswith("tt") else {"term": str(raw_id)}
                    res = await self._make_arr_client(svc).lookup(lookup_req)
                    if res.get("results"):
                        first = res["results"][0]
                        detail = {"item_id": str(raw_id), "service": svc, **first}
                        break
                except UpstreamClientError:
                    continue
            if not detail:
                raise
        self._last_status = f"detail:{detail.get('item_id', '')}"
        return CapabilityResult.ok(detail)

    async def _action_local_status(self, req: Mapping[str, Any]) -> CapabilityResult:
        if self.ctx is None or not self.ctx.capabilities.has("media.local_match.v1"):
            return self._fail(
                "本地片源核验能力 media.local_match.v1 不可用",
                error_code="capability_unavailable",
                data={"available": False, "matched": False, "exists": False, "status": "unknown"},
            )
        imdb_id = str(req.get("imdb_id") or "").strip()
        title = str(req.get("title") or "").strip()
        tmdb_id = req.get("tmdb_id")
        tvdb_id = req.get("tvdb_id")
        if imdb_id:
            imdb_id = validate_media_id(imdb_id, field="imdb_id")
        if not any((imdb_id, title, tmdb_id, tvdb_id)):
            return self._fail("local_status 需要提供 imdb_id、title、tmdb_id 或 tvdb_id", error_code="invalid_argument")

        query: dict[str, Any] = {}
        if imdb_id:
            query["imdb_id"] = imdb_id
        if title:
            query["title"] = title
        if tmdb_id not in (None, ""):
            query["tmdb_id"] = tmdb_id
        if tvdb_id not in (None, ""):
            query["tvdb_id"] = tvdb_id
        if req.get("year") not in (None, "") and not isinstance(req.get("year"), bool):
            query["year"] = req["year"]
        for key in ("season", "episode"):
            if key in req and req[key] is not None:
                val = req[key]
                if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                    return self._fail(f"{key} 必须为非负整数", error_code="invalid_argument")
                query[key] = val
        if "season" in query or "episode" in query:
            query["media_type"] = "series"
        elif req.get("media_type"):
            query["media_type"] = str(req["media_type"]).strip()

        res = await self.ctx.capabilities.call("media.local_match.v1", query)
        if not res.ok:
            code = str(res.data.get("error_code") or "local_match_failed")
            mapped_code = "capability_unavailable" if code == "capability_not_found" else code
            return self._fail(
                res.error or "本地片源核验失败",
                error_code=mapped_code,
                data={"available": False, "matched": False, "exists": False, "status": "unknown"},
            )

        data = dict(res.data)
        matches = next(
            (list(data[k]) for k in ("matches", "candidates", "files") if isinstance(data.get(k), list)),
            [],
        )
        matched: bool | None = None
        for key in ("matched", "exists", "found", "has_local", "in_library"):
            if isinstance(data.get(key), bool):
                matched = data[key]
                break
        if matched is None:
            matched = bool(matches)
        self._last_status = "local:present" if matched else "local:missing"
        return CapabilityResult.ok(
            {
                "available": True,
                "status": "present" if matched else "missing",
                "exists": matched,
                "matched": matched,
                "matches": matches,
                "best_match": data.get("best_match") if data.get("best_match") is not None else (matches[0] if matches else None),
                "confidence": data.get("confidence", 1.0 if matched else 0.0),
                "query": query,
                "provider_plugin_id": res.provider_plugin_id,
            }
        )

    async def _action_arr(self, service: str, op: str, req: Mapping[str, Any]) -> CapabilityResult:
        cfg = self._read_config()
        client = self._make_arr_client(service)
        if op == "lookup":
            data = await client.lookup(req)
            self._last_status = f"{service}_lookup:{data['count']}"
            return CapabilityResult.ok(data)
        if op == "queue":
            data = await client.queue(req)
            self._last_queue = list(data["records"])[:20]
            self._last_download_items = [self._format_arr_queue_item(service, rec) for rec in self._last_queue]
            self._last_download_summary = f"{service.capitalize()} 队列 {len(self._last_queue)} 项"
            self._last_status = f"{service}_queue:{len(data['records'])}"
            return CapabilityResult.ok(data)
        if op == "add":
            if "confirm" in req and not isinstance(req["confirm"], bool):
                return self._fail("confirm 必须为布尔值（仅 confirm=true 时实际提交下载）", error_code="invalid_argument")
            if "dry_run" in req and not isinstance(req["dry_run"], bool):
                return self._fail("dry_run 必须为布尔值", error_code="invalid_argument")
            data = await client.add(
                req,
                default_quality_profile=str(cfg.get("quality_profile") or ""),
                default_root_folder=str(cfg.get(f"{service}_default_root_folder") or cfg.get("root_folder_path") or ""),
            )
            self._last_status = f"{service}_add:{'submitted' if data.get('submitted') else 'dry_run'}"
            return CapabilityResult.ok(data)
        return self._fail(f"未知的 {service} 动作: {op}", error_code="invalid_argument")

    def _resolve_target_service(self, req: Mapping[str, Any]) -> str:
        raw_svc = str(req.get("service") or "auto").strip().lower()
        if raw_svc in {"radarr", "sonarr"}:
            return raw_svc
        category = str(req.get("category") or "").strip().lower()
        item_id = str(req.get("item_id") or "").strip().lower()
        if category.startswith("tv") or item_id.startswith("tvdb:") or "tv" in item_id:
            return "sonarr"
        cfg = self._read_config()
        if str(cfg.get("radarr_url") or "").strip() and self._secret("radarr_api_key"):
            return "radarr"
        return "sonarr"

    async def _action_download(self, req: Mapping[str, Any], *, force_confirm: bool) -> CapabilityResult:
        cfg = self._read_config()
        service = self._resolve_target_service(req)
        mode = str(req.get("download_mode") or self._default_download_mode(cfg)).strip().lower()
        mode = "size" if mode == "size" else "speed"

        lookup_req = dict(req)
        raw_id = str(lookup_req.get("item_id") or lookup_req.get("imdb_id") or "").strip()
        if raw_id.startswith("tt") and not lookup_req.get("imdb_id"):
            lookup_req["imdb_id"] = raw_id
        elif raw_id and not lookup_req.get("term") and not lookup_req.get("query"):
            lookup_req["term"] = raw_id
        if force_confirm:
            lookup_req["confirm"] = True
            lookup_req["dry_run"] = False
        else:
            lookup_req["confirm"] = False
            lookup_req["dry_run"] = True

        default_profile = str(cfg.get(f"{service}_default_profile_id") or "")
        if default_profile in {"", "0"}:
            default_profile = str(cfg.get("quality_profile") or "")
        seeders_profile = str(cfg.get(f"{service}_seeders_first_profiles") or "").strip()
        default_root = str(cfg.get(f"{service}_default_root_folder") or cfg.get("root_folder_path") or "")

        client = self._make_arr_client(service)
        data = await client.download_with_mode(
            lookup_req,
            download_mode=mode,
            default_quality_profile=default_profile,
            seeders_quality_profile=seeders_profile,
            default_root_folder=default_root,
            reject_cam_ts=bool(cfg.get("reject_cam_ts", True)),
        )
        mode_label = "速度优先" if mode == "speed" else "大小/画质优先"
        if data.get("submitted"):
            title = str(((data.get("item") or {}).get("title")) or raw_id or req.get("query") or "")
            grabbed = data.get("grabbed_release")
            if isinstance(grabbed, Mapping) and grabbed.get("title"):
                self._last_status = f"已开始下载（{mode_label}）：{title} · {grabbed.get('seeders', 0)} 做种"
            elif data.get("search_triggered"):
                self._last_status = f"已加入 {service.capitalize()} 并触发搜索（{mode_label}）：{title}"
            else:
                self._last_status = f"已加入 {service.capitalize()}（{mode_label}）：{title}"
            await self._refresh_all_downloads()
        else:
            title = str(data.get("title") or raw_id or req.get("query") or "")
            self._last_status = f"演练预览（{mode_label}）：{title}"
        return CapabilityResult.ok(data)

    @staticmethod
    def _format_arr_queue_item(service: str, rec: Mapping[str, Any]) -> dict[str, Any]:
        pct = rec.get("progress_pct", 0.0)
        size = float(rec.get("size") or 0.0)
        sizeleft = float(rec.get("sizeleft") or 0.0)
        done = max(0.0, size - sizeleft)
        state = str(rec.get("tracked_download_state") or rec.get("status") or "queued")
        eta = str(rec.get("timeleft") or "").strip()
        client_name = str(rec.get("download_client") or service.capitalize())
        parts = [f"[{service.upper()} · {state}] {pct}%"]
        if size > 0:
            parts.append(f"{_format_bytes(done)} / {_format_bytes(size)}")
        if eta:
            parts.append(f"剩余 {eta}")
        parts.append(client_name)
        err = str(rec.get("error_message") or "").strip()
        if err:
            parts.append(err)
        return {
            "id": f"{service}:{rec.get('id') or rec.get('title')}",
            "title": str(rec.get("title") or rec.get("movie_title") or rec.get("series_title") or "未命名任务"),
            "subtitle": " · ".join(parts),
        }

    @staticmethod
    def _format_qb_item(torrent: Mapping[str, Any]) -> dict[str, Any]:
        pct = torrent.get("progress_pct", 0.0)
        dlspeed = int(torrent.get("dlspeed") or 0)
        size = int(torrent.get("size") or 0)
        left = int(torrent.get("amount_left") or 0)
        done = max(0, size - left)
        state = str(torrent.get("state") or "unknown")
        eta = _format_eta(int(torrent.get("eta") or -1))
        seeds = int(torrent.get("num_seeds") or 0)
        parts = [f"[qBittorrent · {state}] {pct}%", f"↓ {_format_bytes(dlspeed)}/s"]
        if size > 0:
            parts.append(f"{_format_bytes(done)} / {_format_bytes(size)}")
        if eta:
            parts.append(f"剩余 {eta}")
        parts.append(f"{seeds} 做种")
        return {
            "id": f"qb:{torrent.get('hash') or torrent.get('name')}",
            "title": str(torrent.get("name") or "未命名种子"),
            "subtitle": " · ".join(parts),
        }

    async def _refresh_all_downloads(self) -> dict[str, Any]:
        cfg = self._read_config()
        radarr_records: list[dict[str, Any]] = []
        sonarr_records: list[dict[str, Any]] = []
        qb_torrents: list[dict[str, Any]] = []
        formatted: list[dict[str, Any]] = []

        if str(cfg.get("radarr_url") or "").strip() and self._secret("radarr_api_key"):
            try:
                res = await self._make_arr_client("radarr").queue({"page_size": 20})
                radarr_records = list(res.get("records") or [])
                formatted.extend(self._format_arr_queue_item("radarr", r) for r in radarr_records)
            except UpstreamClientError:
                pass

        if str(cfg.get("sonarr_url") or "").strip() and self._secret("sonarr_api_key"):
            try:
                res = await self._make_arr_client("sonarr").queue({"page_size": 20})
                sonarr_records = list(res.get("records") or [])
                formatted.extend(self._format_arr_queue_item("sonarr", r) for r in sonarr_records)
            except UpstreamClientError:
                pass

        qb_url = str(cfg.get("qbittorrent_url") or "").strip()
        if qb_url:
            try:
                qb = QbittorrentClient(
                    self._http_client(),
                    qb_url,
                    str(cfg.get("qbittorrent_username") or ""),
                    self._secret("qbittorrent_password"),
                )
                qb_torrents = await qb.fetch_torrents(limit=20)
                formatted.extend(self._format_qb_item(t) for t in qb_torrents)
            except UpstreamClientError:
                pass

        self._last_queue = (radarr_records + sonarr_records)[:20]
        self._last_download_items = formatted[:30]
        self._last_download_summary = f"Radarr {len(radarr_records)} · Sonarr {len(sonarr_records)} · qB {len(qb_torrents)}"
        return {
            "radarr_count": len(radarr_records),
            "sonarr_count": len(sonarr_records),
            "qbittorrent_count": len(qb_torrents),
            "radarr_records": radarr_records,
            "sonarr_records": sonarr_records,
            "qbittorrent_torrents": qb_torrents,
            "items": self._last_download_items,
        }

    async def _action_download_status(self, _req: Mapping[str, Any]) -> CapabilityResult:
        summary = await self._refresh_all_downloads()
        self._last_status = f"downloads:{self._last_download_summary}"
        return CapabilityResult.ok(summary)

    def _build_ui_schema(self) -> dict[str, Any]:
        cfg = self._read_config()
        secrets = self._all_secrets()
        has_custom_imdb = bool(str(cfg.get("imdb_api_base") or "").strip() or str(cfg.get("tmdb_api_base") or "").strip())
        imdb_ready = has_custom_imdb or self._has_public_imdb_fallback(cfg)
        radarr_ready = bool(str(cfg.get("radarr_url") or "").strip()) and bool(self._secret("radarr_api_key"))
        sonarr_ready = bool(str(cfg.get("sonarr_url") or "").strip()) and bool(self._secret("sonarr_api_key"))
        qb_ready = bool(str(cfg.get("qbittorrent_url") or "").strip())
        default_mode = self._default_download_mode(cfg)
        items = [
            {
                "id": str(entry.get("id") or idx),
                "title": scrub_secrets(str(entry.get("title") or ""), secrets),
                "subtitle": scrub_secrets(str(entry.get("subtitle") or entry.get("status") or ""), secrets),
            }
            for idx, entry in enumerate(self._recent_items)
        ]
        downloads = [
            {
                "id": str(entry.get("id") or idx),
                "title": scrub_secrets(str(entry.get("title") or ""), secrets),
                "subtitle": scrub_secrets(str(entry.get("subtitle") or ""), secrets),
            }
            for idx, entry in enumerate(self._last_download_items)
        ]
        return {
            "type": "Section",
            "id": "arr-imdb-root",
            "title": "IMDb · Radarr / Sonarr 下载联动台",
            "description": "榜单探索、NAS 片源核验、速度/大小双模式选种下载与 qBittorrent 实时进度跟踪",
            "children": [
                {
                    "type": "Markdown",
                    "id": "arr-imdb-intro",
                    "content": "选择 **速度优先（做种最多）** 或 **大小/画质优先** 后，可先点「演练预览下载」核对参数，再点「开始下载」提交到 Radarr/Sonarr 并在下方实时查看队列与 qBittorrent 进度。",
                },
                {
                    "type": "KeyValue",
                    "id": "arr-imdb-status",
                    "items": [
                        {"key": "imdb_configured", "label": "IMDb/TMDB 数据源", "value": "自定义 API" if has_custom_imdb else ("公开源/Arr 兜底" if imdb_ready else "未配置")},
                        {"key": "radarr_configured", "label": "Radarr / Sonarr / qB", "value": f"Radarr={str(radarr_ready).lower()} · Sonarr={str(sonarr_ready).lower()} · qB={str(qb_ready).lower()}"},
                        {"key": "download_mode", "label": "默认下载方式", "value": "速度优先（做种最多）" if default_mode == "speed" else "大小/画质优先"},
                        {"key": "download_summary", "label": "当前下载队列", "value": self._last_download_summary},
                        {"key": "last_status", "label": "最近状态", "value": scrub_secrets(self._last_status, secrets)},
                    ],
                },
                {
                    "type": "Form",
                    "id": "arr-imdb-query-form",
                    "fields": [
                        {"name": "category", "label": "榜单分类", "type": "select", "default": "popular", "options": list(self._source.categories())},
                        {"name": "query", "label": "搜索关键词 / 片名", "type": "string", "default": ""},
                        {"name": "item_id", "label": "IMDb / TMDB / TVDB ID", "type": "string", "default": ""},
                        {
                            "name": "service",
                            "label": "下载目标服务",
                            "type": "select",
                            "default": "auto",
                            "options": [
                                {"value": "auto", "label": "自动识别（电影→Radarr / 剧集→Sonarr）"},
                                {"value": "radarr", "label": "Radarr（电影）"},
                                {"value": "sonarr", "label": "Sonarr（剧集）"},
                            ],
                        },
                        {
                            "name": "download_mode",
                            "label": "下载方式（v1 双模式）",
                            "type": "select",
                            "default": default_mode,
                            "options": [
                                {"value": "speed", "label": "速度优先（做种最多）"},
                                {"value": "size", "label": "大小/画质优先（体积最大）"},
                            ],
                        },
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "arr-imdb-actions",
                    "actions": [
                        {"id": "fetch_items", "label": "拉取榜单", "variant": "primary"},
                        {"id": "fetch_detail", "label": "查看详情", "variant": "secondary"},
                        {"id": "local_status", "label": "核验 NAS 片源", "variant": "secondary"},
                        {"id": "download_preview", "label": "演练预览下载", "variant": "secondary"},
                        {"id": "download_submit", "label": "开始下载", "variant": "primary"},
                        {"id": "download_status", "label": "刷新下载状态", "variant": "secondary"},
                        {"id": "radarr_queue", "label": "Radarr 队列", "variant": "secondary"},
                        {"id": "sonarr_queue", "label": "Sonarr 队列", "variant": "secondary"},
                    ],
                },
                {
                    "type": "List",
                    "id": "arr-imdb-downloads",
                    "title": "下载进度与队列状态（是否开始 / 速度 / 大小）",
                    "items": downloads,
                },
                {
                    "type": "List",
                    "id": "arr-imdb-list",
                    "title": "最近条目 / 搜索结果",
                    "items": items,
                },
            ],
        }

    def _build_workspace_tab_spec(self) -> WorkspaceTabSpec:
        return WorkspaceTabSpec(
            tab_id="arr-imdb",
            title="IMDb · Arr",
            domain="media",
            badge="ARR",
            ui_schema=self._build_ui_schema(),
            render=self._build_ui_schema,
            on_action=self.handle_tab_action,
        )

    async def handle_tab_action(self, action_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Handle declarative workspace tab actions and return redacted ui_schema."""
        state_map = dict(state) if state is not None else {}
        if action_id == "clear":
            self._last_status = "idle"
            self._recent_items.clear()
            self._last_queue.clear()
            self._last_download_items.clear()
            self._last_download_summary = "未刷新"
            return {"ok": True, "result": {"cleared": True}, "ui_schema": self._build_ui_schema()}
        res = await self.handle_capability({"action": action_id, **state_map})
        out: dict[str, Any] = {"ok": res.ok, "result": res.data, "ui_schema": self._build_ui_schema()}
        if not res.ok:
            out["error"] = res.error
            out["error_code"] = str(res.data.get("error_code") or "")
        return out


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning an `ArrImdbPlugin` instance."""
    return ArrImdbPlugin()
