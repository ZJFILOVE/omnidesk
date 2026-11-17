"""OmniDesk v2 plugin: SubHunter (`subtitle.fetch.v1`, `.sync.v1`, `.translate.v1`)."""

from __future__ import annotations

import ssl
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext
from omnidesk_sdk.stores import redact_secrets

try:
    from . import sub_archive, sub_net, sub_providers, sub_sync, sub_translate
except ImportError:
    import sub_archive  # type: ignore[no-redef]
    import sub_net  # type: ignore[no-redef]
    import sub_providers  # type: ignore[no-redef]
    import sub_sync  # type: ignore[no-redef]
    import sub_translate  # type: ignore[no-redef]

_FETCH_ACTIONS = ("status", "probe", "derive_query", "search", "download")
_SYNC_ACTIONS = ("status", "probe", "shift", "sync")
_TRANSLATE_ACTIONS = ("status", "translate")
_PROVIDERS = frozenset({"all", "subhd", "assrt", "custom"})
_OVERWRITE_POLICIES = frozenset({"protect_manual", "skip_existing", "overwrite"})


class SubHunterPlugin(BasePlugin):
    """Provider plugin for subtitle search, secure archive download, and audio sync."""

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context

    async def on_load(self, ctx: PluginContext) -> None:
        """Register subtitle fetch/sync/translate capabilities without blocking."""
        await super().on_load(ctx)
        ctx.register_capability("subtitle.fetch.v1", self.handle_fetch)
        ctx.register_capability("subtitle.sync.v1", self.handle_sync)
        ctx.register_capability("subtitle.translate.v1", self.handle_translate)

    async def on_health_check(self) -> dict[str, Any]:
        """Return non-blocking configuration and binary readiness diagnostics."""
        cfg = self._cfg()
        ff_bin = str(cfg.get("ffsubsync_bin") or "ffsubsync").strip()
        return {
            "ok": True,
            "plugin_id": self.ctx.plugin_id if self.ctx is not None else "plugin-sub-hunter",
            "capabilities": [
                "subtitle.fetch.v1",
                "subtitle.sync.v1",
                "subtitle.translate.v1",
            ],
            "provider": str(cfg.get("provider") or "all"),
            "assrt_token_configured": bool(self._secret("assrt_token")),
            "web_cookies_available": bool(self.ctx and self.ctx.capabilities.has("web.cookies.v1")),
            "llm_translate_available": bool(
                self.ctx and self.ctx.capabilities.has("llm.translate.v1")
            ),
            "translation_concurrency": sub_translate.clamp_concurrency(
                cfg.get("translation_concurrency", sub_translate.DEFAULT_CONCURRENCY)
            ),
            "subtitle_output_mode": sub_translate.normalize_output_mode(
                cfg.get("subtitle_output_mode", sub_translate.DEFAULT_OUTPUT_MODE)
            ),
            "ffsubsync_configured": bool(ff_bin),
            "ffsubsync_found": bool(sub_sync.resolve_ffsubsync_bin(ff_bin)),
        }

    def _cfg(self) -> dict[str, Any]:
        return {} if self.ctx is None else dict(self.ctx.get_config())

    def _secret(self, key: str) -> str:
        if self.ctx is None:
            return ""
        val = self.ctx.get_secret(key)
        return val.strip() if isinstance(val, str) else ""

    def _runtime_data_dir(self) -> Path:
        if self.ctx is None:
            return Path(tempfile.gettempdir()) / "omnidesk" / "plugin-sub-hunter"
        if self.ctx.data_dir != self.ctx.plugin_dir:
            return self.ctx.data_dir
        cfg_root = getattr(getattr(self.ctx, "_config_store", None), "_root_dir", None)
        if isinstance(cfg_root, Path) and cfg_root != self.ctx.plugin_dir:
            return cfg_root / "plugin-data" / self.ctx.plugin_id
        return Path(tempfile.gettempdir()) / "omnidesk" / self.ctx.plugin_id

    def _timeout_sec(self, req: Mapping[str, Any], cfg: Mapping[str, Any]) -> float:
        raw = req.get("timeout_sec")
        if raw is None and req.get("timeout_ms") is not None:
            raw = float(req["timeout_ms"]) / 1000.0
        if raw is None:
            raw = cfg.get("request_timeout_sec", 15)
        try:
            return max(0.05, min(float(raw), 120.0))
        except (TypeError, ValueError):
            return 15.0

    def _translate_timeout_sec(self, req: Mapping[str, Any], cfg: Mapping[str, Any]) -> float:
        raw = req.get("timeout_sec")
        if raw is None and req.get("timeout_ms") is not None:
            raw = float(req["timeout_ms"]) / 1000.0
        if raw is None:
            raw = cfg.get("translation_timeout_sec", 600)
        try:
            return max(10.0, min(float(raw), 1800.0))
        except (TypeError, ValueError):
            return 600.0

    def _overwrite_policy(self, req: Mapping[str, Any], cfg: Mapping[str, Any]) -> str:
        policy = str(req.get("overwrite_policy") or cfg.get("overwrite_policy") or "protect_manual").strip()
        return policy if policy in _OVERWRITE_POLICIES else "protect_manual"

    def _fail(self, exc_code: str, message: str, *, data: Mapping[str, Any] | None = None) -> CapabilityResult:
        safe = redact_secrets(message, sub_net.collect_secrets(self.ctx))
        return CapabilityResult.fail(safe, data=data, error_code=exc_code)

    async def handle_fetch(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Dispatch `subtitle.fetch.v1` actions (`status`, `probe`, `derive_query`, `search`, `download`)."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "").strip()
        if action in {"status", "probe"}:
            return self._fetch_status()
        try:
            if action == "derive_query":
                derived = sub_providers.derive_search_params(payload)
                if not derived["query"]:
                    return self._fail("invalid_argument", "缺少可用于推导检索词的片名、文件名或 IMDb ID")
                return CapabilityResult.ok(derived)
            if action == "search":
                return await self._handle_search(payload)
            if action == "download":
                return await self._handle_download(payload)
        except sub_net.SubNetError as exc:
            return self._fail(exc.error_code, str(exc))
        return self._fail(
            "invalid_argument",
            f"不支持的 subtitle.fetch.v1 动作: {action!r}",
            data={"supported_actions": list(_FETCH_ACTIONS)},
        )

    def _fetch_status(self) -> CapabilityResult:
        cfg = self._cfg()
        ff_bin = str(cfg.get("ffsubsync_bin") or "ffsubsync").strip()
        return CapabilityResult.ok(
            {
                "provider": str(cfg.get("provider") or "all"),
                "assrt_token_configured": bool(self._secret("assrt_token")),
                "cookie_configured": bool(self._secret("cookie_header")),
                "web_cookies_available": bool(self.ctx and self.ctx.capabilities.has("web.cookies.v1")),
                "download_root": str(cfg.get("download_root") or ""),
                "max_download_mb": sub_archive.effective_max_bytes(cfg.get("max_download_mb", 16)) // (1024 * 1024),
                "overwrite_policy": str(cfg.get("overwrite_policy") or "protect_manual"),
                "ffsubsync_configured": bool(ff_bin),
                "ffsubsync_found": bool(sub_sync.resolve_ffsubsync_bin(ff_bin)),
            }
        )

    async def _handle_search(self, req: Mapping[str, Any]) -> CapabilityResult:
        cfg = self._cfg()
        provider = str(req.get("provider") or cfg.get("provider") or "all").strip().lower()
        if provider not in _PROVIDERS:
            return self._fail("invalid_argument", f"未知字幕源 provider: {provider!r}")
        params = sub_providers.derive_search_params(req)
        if not params["query"]:
            return self._fail("invalid_argument", "搜索需要提供 query、title、filename 或 imdb_id")

        timeout = self._timeout_sec(req, cfg)
        max_bytes = sub_archive.effective_max_bytes(cfg.get("max_download_mb", 16))
        if provider == "all":
            targets = ["subhd"]
            if self._secret("assrt_token"):
                targets.append("assrt")
            if str(cfg.get("custom_api_base") or "").strip():
                targets.append("custom")
        else:
            targets = [provider]

        opts = sub_providers.ProviderOpts(timeout=timeout, max_bytes=max_bytes, ssl_context=self._ssl_context)
        results: list[dict[str, Any]] = []
        errors: dict[str, tuple[str, str, int | None]] = {}
        for name in targets:
            try:
                results.extend(
                    await sub_providers.search_provider(
                        self.ctx,
                        name,
                        params,
                        cfg,
                        assrt_token=self._secret("assrt_token"),
                        custom_api_key=self._secret("custom_api_key"),
                        opts=opts,
                    )
                )
            except sub_net.SubNetError as exc:
                errors[name] = (
                    exc.error_code,
                    redact_secrets(str(exc), sub_net.collect_secrets(self.ctx)),
                    exc.http_status,
                )

        if errors and len(errors) == len(targets):
            code, msg, status = next(iter(errors.values()))
            return self._fail(code, msg, data={"results": [], "count": 0, "http_status": status, "degraded": True})

        filtered = sub_providers.filter_and_rank_results(results, params["language"])
        return CapabilityResult.ok(
            {
                "results": filtered,
                "count": len(filtered),
                "query": params["query"],
                "provider": provider,
                "providers_queried": targets,
                "provider_errors": {k: {"error_code": v[0], "message": v[1]} for k, v in errors.items()},
            }
        )

    async def _handle_download(self, req: Mapping[str, Any]) -> CapabilityResult:
        cfg = self._cfg()
        timeout = self._timeout_sec(req, cfg)
        max_bytes = sub_archive.effective_max_bytes(cfg.get("max_download_mb", 16))
        policy = self._overwrite_policy(req, cfg)
        data_dir = self._runtime_data_dir()

        try:
            out_dir = sub_archive.resolve_download_dir(
                req.get("output_dir") if isinstance(req.get("output_dir"), str) else None,
                str(cfg.get("download_root") or ""),
                data_dir / "subtitles",
            )
            raw_url = str(req.get("url") or req.get("download_url") or "").strip()
            headers: dict[str, str] = {}
            allow_lb = True
            if not raw_url:
                sub_id = str(req.get("id") or "").strip()
                provider = str(req.get("provider") or cfg.get("provider") or "").strip().lower()
                if not sub_id or provider not in {"assrt", "subhd", "custom"}:
                    return self._fail("invalid_argument", "下载需要提供 HTTPS url 或明确的 provider + id")
                opts = sub_providers.ProviderOpts(timeout=timeout, max_bytes=max_bytes, ssl_context=self._ssl_context)
                raw_url, headers, allow_lb = await sub_providers.resolve_provider_download(
                    self.ctx,
                    provider,
                    sub_id,
                    cfg,
                    assrt_token=self._secret("assrt_token"),
                    custom_api_key=self._secret("custom_api_key"),
                    opts=opts,
                )

            sub_net.validate_https_url(raw_url, allow_loopback=allow_lb)
            if "Cookie" not in headers:
                cookie = await sub_net.resolve_cookie_header(self.ctx, raw_url)
                if cookie:
                    headers["Cookie"] = cookie

            resp = await sub_net.fetch_https_bytes(
                raw_url,
                headers=headers,
                timeout_sec=timeout,
                max_bytes=max_bytes,
                allow_loopback=allow_lb,
                ssl_context=self._ssl_context,
                secrets=sub_net.collect_secrets(self.ctx),
            )
            hint_name = str(req.get("filename") or resp.filename or "subtitle.srt")
            lang_hint = str(req.get("language") or cfg.get("preferred_langs") or "")
            items = sub_archive.extract_subtitle_payload(
                resp.body, hint_name, max_bytes=max_bytes, charset_hint=resp.charset, lang_hint=lang_hint
            )

            out_dir.mkdir(parents=True, exist_ok=True)
            planned: list[tuple[Path, str, Any]] = []
            skipped: list[str] = []
            for item in items:
                target = out_dir / sub_archive.sanitize_subtitle_filename(item.filename)
                if sub_archive.check_overwrite_allowed(target, data_dir, policy) == "skip":
                    skipped.append(str(target))
                else:
                    planned.append((target, item.text, item))

            written: list[dict[str, Any]] = []
            for target, text, item in planned:
                sub_archive.atomic_write_text(target, text)
                sub_archive.record_managed_subtitle(data_dir, target, text)
                written.append(
                    {
                        "path": str(target),
                        "filename": target.name,
                        "source_encoding": item.encoding,
                        "output_encoding": "utf-8",
                        "bytes": len(text.encode("utf-8")),
                    }
                )
            return CapabilityResult.ok(
                {"files": written, "skipped": skipped, "count": len(written), "output_dir": str(out_dir)}
            )
        except (sub_net.SubNetError, sub_archive.ArchiveSecurityError) as exc:
            return self._fail(exc.error_code, str(exc))

    async def handle_translate(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Dispatch `subtitle.translate.v1` actions (`status`, `translate`)."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "translate").strip()
        cfg = self._cfg()
        concurrency = sub_translate.clamp_concurrency(
            payload.get(
                "translation_concurrency",
                cfg.get("translation_concurrency", sub_translate.DEFAULT_CONCURRENCY),
            )
        )
        output_mode = sub_translate.normalize_output_mode(
            payload.get(
                "output_mode",
                cfg.get("subtitle_output_mode", sub_translate.DEFAULT_OUTPUT_MODE),
            )
        )
        if "output_mode" not in payload and "bilingual" in payload:
            output_mode = "bilingual" if bool(payload["bilingual"]) else "translated"
        data_dir = self._runtime_data_dir()

        if action in {"status", "probe"}:
            return CapabilityResult.ok(
                sub_translate.translation_status(
                    self.ctx, data_dir, concurrency, output_mode
                )
            )
        if action != "translate":
            return self._fail(
                "invalid_argument",
                f"不支持的 subtitle.translate.v1 动作: {action!r}",
                data={"supported_actions": list(_TRANSLATE_ACTIONS)},
            )

        sub_path = str(payload.get("subtitle_path") or "").strip()
        if not sub_path:
            return self._fail("invalid_argument", "translate 需要提供 subtitle_path")
        assert self.ctx is not None
        try:
            result = await sub_translate.translate_subtitle_file(
                self.ctx,
                subtitle_path=sub_path,
                output_path=str(payload.get("output_path") or "").strip() or None,
                target_lang=str(payload.get("target_lang") or "zh-CN").strip() or "zh-CN",
                output_mode=output_mode,
                overwrite_policy=self._overwrite_policy(payload, cfg),
                data_dir=data_dir,
                concurrency=concurrency,
                timeout_sec=self._translate_timeout_sec(payload, cfg),
                use_cache=bool(payload.get("use_cache", True)),
            )
            return CapabilityResult.ok(result)
        except sub_translate.TranslateError as exc:
            return self._fail(exc.error_code, str(exc))

    async def handle_sync(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Dispatch `subtitle.sync.v1` actions (`status`, `probe`, `shift`, `sync`)."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "").strip()
        cfg = self._cfg()
        ff_bin = str(cfg.get("ffsubsync_bin") or "ffsubsync").strip()
        policy = self._overwrite_policy(payload, cfg)

        if action in {"status", "probe"}:
            resolved = sub_sync.resolve_ffsubsync_bin(ff_bin)
            return CapabilityResult.ok(
                {
                    "ffsubsync_configured": bool(ff_bin),
                    "ffsubsync_found": bool(resolved),
                    "ffsubsync_path": resolved or "",
                    "overwrite_policy": policy,
                }
            )

        data_dir = self._runtime_data_dir()
        sub_path = str(payload.get("subtitle_path") or "").strip()
        out_path = str(payload.get("output_path") or "").strip() or None
        in_place = bool(payload.get("in_place", False))

        try:
            if action == "shift":
                if not sub_path:
                    return self._fail("invalid_argument", "shift 需要提供 subtitle_path")
                return CapabilityResult.ok(
                    sub_sync.shift_subtitle_file(
                        subtitle_path=sub_path,
                        output_path=out_path,
                        offset_ms=payload.get("offset_ms", 0),
                        scale=payload.get("scale", 1.0),
                        overwrite_policy=policy,
                        data_dir=data_dir,
                        in_place=in_place,
                    )
                )

            if action == "sync":
                if not sub_path:
                    return self._fail("invalid_argument", "sync 需要提供 subtitle_path")
                mode = str(payload.get("mode") or "auto").strip()
                ref_path = str(payload.get("reference_path") or "").strip()
                if mode == "manual_shift" or (
                    mode == "auto" and not ref_path and ("offset_ms" in payload or "scale" in payload)
                ):
                    return CapabilityResult.ok(
                        sub_sync.shift_subtitle_file(
                            subtitle_path=sub_path,
                            output_path=out_path,
                            offset_ms=payload.get("offset_ms", 0),
                            scale=payload.get("scale", 1.0),
                            overwrite_policy=policy,
                            data_dir=data_dir,
                            in_place=in_place,
                        )
                    )
                if mode not in {"auto", "ffsubsync"}:
                    return self._fail("invalid_argument", f"不支持的同步模式: {mode!r}")
                if not ref_path:
                    return self._fail("invalid_argument", "ffsubsync 同步需要提供 reference_path")
                assert self.ctx is not None
                try:
                    audio_stream = int(payload.get("audio_stream", -1))
                    timeout_sec = max(0.05, float(payload.get("timeout_sec", 120.0)))
                except (TypeError, ValueError) as exc:
                    return self._fail("invalid_argument", f"同步参数无效: {exc}")
                return CapabilityResult.ok(
                    await sub_sync.sync_with_ffsubsync(
                        self.ctx,
                        ffsubsync_bin=ff_bin,
                        subtitle_path=sub_path,
                        reference_path=ref_path,
                        output_path=out_path,
                        overwrite_policy=policy,
                        data_dir=data_dir,
                        audio_stream=audio_stream,
                        max_offset_seconds=payload.get("max_offset_seconds"),
                        timeout_sec=timeout_sec,
                        in_place=in_place,
                    )
                )
        except (sub_sync.SyncError, sub_archive.ArchiveSecurityError) as exc:
            return self._fail(exc.error_code, str(exc))

        return self._fail(
            "invalid_argument",
            f"不支持的 subtitle.sync.v1 动作: {action!r}",
            data={"supported_actions": list(_SYNC_ACTIONS)},
        )


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `SubHunterPlugin` instance."""
    return SubHunterPlugin()
