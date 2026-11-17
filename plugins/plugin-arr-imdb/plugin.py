"""OmniDesk v2 plugin: IMDb/TMDB charts & details with fail-closed Radarr/Sonarr queue management."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext, WorkspaceTabSpec

try:
    from .arr_client import RADARR_SPEC, SONARR_SPEC, ArrClient
    from .http_client import SafeHttpClient, UpstreamClientError, scrub_secrets, validate_media_id
    from .imdb_client import ImdbTmdbClient
    from .sources import ArrImdbMediaSource
except ImportError:
    _HERE = str(Path(__file__).resolve().parent)
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from arr_client import RADARR_SPEC, SONARR_SPEC, ArrClient
    from http_client import SafeHttpClient, UpstreamClientError, scrub_secrets, validate_media_id
    from imdb_client import ImdbTmdbClient
    from sources import ArrImdbMediaSource

_SUPPORTED_ACTIONS = (
    "categories",
    "fetch_items",
    "fetch_detail",
    "local_status",
    "radarr_lookup",
    "radarr_add",
    "radarr_queue",
    "sonarr_lookup",
    "sonarr_add",
    "sonarr_queue",
)


class ArrImdbPlugin(BasePlugin):
    """Workspace MediaSource plugin for IMDb/TMDB discovery, local match check, and Radarr/Sonarr control."""

    def __init__(self) -> None:
        self._source = ArrImdbMediaSource(self._build_imdb_client)
        self._last_status: str = "idle"
        self._recent_items: list[dict[str, Any]] = []
        self._last_queue: list[dict[str, Any]] = []

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

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight config readiness flags without making network calls or exposing secrets."""
        cfg = self._read_config()
        plugin_id = self.ctx.plugin_id if self.ctx is not None else "plugin-arr-imdb"
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "source_id": self._source.source_id,
            "imdb_configured": bool(str(cfg.get("imdb_api_base") or "").strip() or str(cfg.get("tmdb_api_base") or "").strip()),
            "radarr_configured": bool(str(cfg.get("radarr_url") or "").strip()) and bool(self._secret("radarr_api_key")),
            "sonarr_configured": bool(str(cfg.get("sonarr_url") or "").strip()) and bool(self._secret("sonarr_api_key")),
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
        keys = ("imdb_api_key", "tmdb_api_key", "radarr_api_key", "sonarr_api_key")
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
            if action == "local_status":
                return await self._action_local_status(payload)
            if action in {"radarr_lookup", "radarr_add", "radarr_queue"}:
                return await self._action_arr("radarr", action.removeprefix("radarr_"), payload)
            if action in {"sonarr_lookup", "sonarr_add", "sonarr_queue"}:
                return await self._action_arr("sonarr", action.removeprefix("sonarr_"), payload)
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

    async def _action_fetch_items(self, req: Mapping[str, Any]) -> CapabilityResult:
        category = str(req.get("category") or "")
        query = str(req.get("query") or "")
        raw_page = req.get("page", 1)
        if isinstance(raw_page, bool) or not isinstance(raw_page, int) or raw_page < 1:
            return self._fail("page 必须为正整数", error_code="invalid_argument")
        items = await self._source.fetch_items(category=category, query=query, page=raw_page)
        dumped = [item.model_dump() for item in items]
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
        detail = await self._source.fetch_detail(str(raw_id), media_type=media_type)
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
        spec = RADARR_SPEC if service == "radarr" else SONARR_SPEC
        client = ArrClient(
            self._http_client(),
            spec,
            str(cfg.get(f"{service}_url") or ""),
            self._secret(f"{service}_api_key"),
        )
        if op == "lookup":
            data = await client.lookup(req)
            self._last_status = f"{service}_lookup:{data['count']}"
            return CapabilityResult.ok(data)
        if op == "queue":
            data = await client.queue(req)
            self._last_queue = list(data["records"])[:20]
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
                default_root_folder=str(cfg.get("root_folder_path") or ""),
            )
            self._last_status = f"{service}_add:{'submitted' if data.get('submitted') else 'dry_run'}"
            return CapabilityResult.ok(data)
        return self._fail(f"未知的 {service} 动作: {op}", error_code="invalid_argument")

    def _build_ui_schema(self) -> dict[str, Any]:
        cfg = self._read_config()
        secrets = self._all_secrets()
        imdb_ready = bool(str(cfg.get("imdb_api_base") or "").strip() or str(cfg.get("tmdb_api_base") or "").strip())
        radarr_ready = bool(str(cfg.get("radarr_url") or "").strip()) and bool(self._secret("radarr_api_key"))
        sonarr_ready = bool(str(cfg.get("sonarr_url") or "").strip()) and bool(self._secret("sonarr_api_key"))
        items = [
            {
                "id": str(entry.get("id") or idx),
                "title": scrub_secrets(str(entry.get("title") or ""), secrets),
                "subtitle": scrub_secrets(str(entry.get("subtitle") or entry.get("status") or ""), secrets),
            }
            for idx, entry in enumerate(self._recent_items or self._last_queue)
        ]
        return {
            "type": "Section",
            "id": "arr-imdb-root",
            "title": "IMDb · Radarr / Sonarr",
            "description": "榜单探索、media.local_match.v1 核验与 Dry-Run 门禁下载队列管理",
            "children": [
                {
                    "type": "Markdown",
                    "id": "arr-imdb-intro",
                    "content": "所有写操作默认 `dry_run=true`，仅在显式传递 `confirm=true` 时才提交 Radarr/Sonarr 下载。",
                },
                {
                    "type": "KeyValue",
                    "id": "arr-imdb-status",
                    "items": [
                        {"key": "imdb_configured", "label": "IMDb/TMDB 已配置", "value": str(imdb_ready).lower()},
                        {"key": "radarr_configured", "label": "Radarr 已配置", "value": str(radarr_ready).lower()},
                        {"key": "sonarr_configured", "label": "Sonarr 已配置", "value": str(sonarr_ready).lower()},
                        {"key": "dry_run_default", "label": "默认 Dry-Run", "value": str(bool(cfg.get("dry_run", True))).lower()},
                        {"key": "last_status", "label": "最近状态", "value": scrub_secrets(self._last_status, secrets)},
                    ],
                },
                {
                    "type": "Form",
                    "id": "arr-imdb-query-form",
                    "fields": [
                        {"name": "category", "label": "榜单分类", "type": "select", "default": "popular", "options": list(self._source.categories())},
                        {"name": "query", "label": "搜索关键词", "type": "string", "default": ""},
                        {"name": "item_id", "label": "IMDb / TMDB ID", "type": "string", "default": ""},
                    ],
                },
                {
                    "type": "ActionRow",
                    "id": "arr-imdb-actions",
                    "actions": [
                        {"id": "fetch_items", "label": "拉取榜单", "variant": "primary"},
                        {"id": "fetch_detail", "label": "查看详情", "variant": "secondary"},
                        {"id": "local_status", "label": "核验 NAS 片源", "variant": "secondary"},
                        {"id": "radarr_queue", "label": "Radarr 队列", "variant": "secondary"},
                        {"id": "sonarr_queue", "label": "Sonarr 队列", "variant": "secondary"},
                    ],
                },
                {
                    "type": "List",
                    "id": "arr-imdb-list",
                    "title": "最近条目 / 队列",
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
