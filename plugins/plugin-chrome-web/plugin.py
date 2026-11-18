"""OmniDesk v2 `plugin-chrome-web` entrypoint providing web.embed/cookies/extract/fetch.v1."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

if __package__:
    from .browser_runtime import browser_operation, close_headless, request_targets, wait_fetch_context
    from .cdp_client import (
        CdpError,
        CdpTransport,
        LoopbackWebSocketCdpTransport,
        extract_eval_value,
        select_extract_target,
        select_same_origin_target,
    )
    from .chrome_process import ChromeSessionManager, resolve_chrome_bin, resolve_extension_dir
    from .cookies_crypto import CookieDecryptor, decryption_scope_summary
    from .cookies_store import handle_cookies_action
    from .fetch_bridge import build_fetch_js, parse_fetch_outcome, validate_fetch_params
    from .html_extract import build_bilingual_segments, extract_from_html
    from .translate_bridge import LoopbackTranslateBridge, bootstrap_extensions_over_cdp, resolve_extension_dirs
    from .security import (
        ValidationProblem,
        origin_string,
        redact_sensitive,
        resolve_profile_dir,
        validate_cdp_port,
        validate_cdp_url,
        validate_http_url,
    )
else:  # pragma: no cover - direct module import fallback
    _PLUGIN_DIR = str(Path(__file__).resolve().parent)
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from browser_runtime import browser_operation, close_headless, request_targets, wait_fetch_context
    from cdp_client import (
        CdpError,
        CdpTransport,
        LoopbackWebSocketCdpTransport,
        extract_eval_value,
        select_extract_target,
        select_same_origin_target,
    )
    from chrome_process import ChromeSessionManager, resolve_chrome_bin, resolve_extension_dir
    from cookies_crypto import CookieDecryptor, decryption_scope_summary
    from cookies_store import handle_cookies_action
    from fetch_bridge import build_fetch_js, parse_fetch_outcome, validate_fetch_params
    from html_extract import build_bilingual_segments, extract_from_html
    from translate_bridge import LoopbackTranslateBridge, bootstrap_extensions_over_cdp, resolve_extension_dirs
    from security import (
        ValidationProblem,
        origin_string,
        redact_sensitive,
        resolve_profile_dir,
        validate_cdp_port,
        validate_cdp_url,
        validate_http_url,
    )


class ChromeWebPlugin(BasePlugin):
    """Managed Chrome `--app` bridge providing `web.embed/cookies/extract/fetch.v1`."""

    def __init__(
        self,
        *,
        cdp_transport: CdpTransport | None = None,
        cookie_decryptor: CookieDecryptor | None = None,
    ) -> None:
        self._sessions = ChromeSessionManager(before_close=lambda session: close_headless(self, session_id=session.session_id))
        self._browser_lock = asyncio.Lock()
        self._cdp_transport: CdpTransport = cdp_transport or LoopbackWebSocketCdpTransport()
        self._cookie_decryptor = cookie_decryptor
        self._translate_bridge: LoopbackTranslateBridge | None = None

    def set_cdp_transport(self, transport: CdpTransport) -> None:
        """Override CDP transport (primarily for offline test harnesses)."""
        self._cdp_transport = transport

    def set_cookie_decryptor(self, decryptor: CookieDecryptor | None) -> None:
        """Override cookie decryptor callback for offline tests."""
        self._cookie_decryptor = decryptor

    async def on_load(self, ctx: PluginContext) -> None:
        """Register web.* capabilities quickly without launching Chrome or probing network."""
        await super().on_load(ctx)
        ctx.register_capability("web.embed.v1", self.handle_embed)
        ctx.register_capability("web.cookies.v1", self.handle_cookies)
        ctx.register_capability("web.extract.v1", self.handle_extract)
        ctx.register_capability("web.fetch.v1", self.handle_fetch)

    async def on_unload(self) -> None:
        """Cleanly terminate all managed Chrome `--app` subprocesses on unload."""
        async with self._browser_lock:
            await self._sessions.shutdown_all()
        if self._translate_bridge is not None:
            await self._translate_bridge.stop()
            self._translate_bridge = None
        self.ctx = None

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight local readiness summary without spawning browsers."""
        ctx = self.ctx
        cfg = ctx.get_config() if ctx is not None else {}
        chrome_bin = str(cfg.get("chrome_bin") or "google-chrome")
        try:
            profile_dir = str(resolve_profile_dir(str(cfg.get("profile_dir") or "")))
        except ValidationProblem:
            profile_dir = ""
        ext_ready = resolve_extension_dir(ctx.plugin_dir) is not None if ctx is not None else False
        return {
            "ok": True,
            "plugin_id": ctx.plugin_id if ctx is not None else "plugin-chrome-web",
            "capabilities": ["web.embed.v1", "web.cookies.v1", "web.extract.v1", "web.fetch.v1"],
            "chrome_bin_found": resolve_chrome_bin(chrome_bin) is not None,
            "profile_dir": profile_dir,
            "extension_ready": ext_ready,
            "active_sessions": len(self._sessions.active_sessions()),
            "cookie_decryption_scope": decryption_scope_summary(),
        }

    def _cfg(self) -> dict[str, Any]:
        return self.ctx.get_config() if self.ctx is not None else {}

    def _profile_dir(self) -> Path:
        return resolve_profile_dir(str(self._cfg().get("profile_dir") or ""))

    def _resolve_cdp_endpoint(self, req: Mapping[str, Any]) -> tuple[str, int]:
        if "cdp_host" in req and req["cdp_host"] is not None:
            if str(req["cdp_host"]).strip() != "127.0.0.1":
                raise ValidationProblem("安全限制: CDP 仅允许连接 127.0.0.1", error_code="permission_denied")
        if req.get("cdp_url"):
            return validate_cdp_url(req["cdp_url"])
        if "cdp_port" in req and req["cdp_port"] is not None:
            return "127.0.0.1", validate_cdp_port(req["cdp_port"], allow_zero=False)
        latest = self._sessions.latest_session()
        if latest is not None and latest.is_running and latest.cdp_port > 0:
            return "127.0.0.1", latest.cdp_port
        cfg_port = validate_cdp_port(self._cfg().get("cdp_port", 9222), allow_zero=True)
        if cfg_port <= 0:
            raise ValidationProblem("尚未配置本地 CDP 端口 (cdp_port)", error_code="not_configured")
        return "127.0.0.1", cfg_port

    def _resolve_timeout_sec(self, req: Mapping[str, Any]) -> float:
        raw = req.get("timeout_ms") if req.get("timeout_ms") is not None else self._cfg().get("request_timeout_ms", 15000)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValidationProblem("timeout_ms 必须为正数")
        ms = float(raw)
        if ms <= 0 or ms > 120_000:
            raise ValidationProblem("timeout_ms 必须在 1..120000 毫秒之间")
        return max(0.05, ms / 1000.0)

    @browser_operation
    async def handle_embed(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `web.embed.v1` actions (`open`, `close`, `status`)."""
        assert self.ctx is not None
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "").strip()
        cfg = self._cfg()
        try:
            profile_dir = self._profile_dir()
        except ValidationProblem as exc:
            return CapabilityResult.fail(str(exc), error_code=exc.error_code)

        chrome_bin = str(cfg.get("chrome_bin") if "chrome_bin" in cfg else "google-chrome")
        ext_enabled = bool(payload["extension_enabled"]) if "extension_enabled" in payload else bool(cfg.get("extension_enabled", True))
        raw_port = payload.get("cdp_port") if payload.get("cdp_port") is not None else cfg.get("cdp_port", 9222)
        try:
            cdp_port = validate_cdp_port(raw_port, allow_zero=True)
        except ValidationProblem as exc:
            return CapabilityResult.fail(str(exc), error_code=exc.error_code)

        if action == "open":
            res = await self._sessions.open_app(
                self.ctx, raw_url=payload.get("url"), chrome_bin=chrome_bin,
                profile_dir=profile_dir, extension_enabled=ext_enabled, cdp_port=cdp_port,
            )
            if res.ok and ext_enabled and cdp_port > 0:
                if self._translate_bridge is None:
                    self._translate_bridge = LoopbackTranslateBridge(self.ctx)
                endpoint, token = await self._translate_bridge.ensure_started()
                self.ctx.spawn_task(
                    bootstrap_extensions_over_cdp(cdp_port, resolve_extension_dirs(self.ctx.plugin_dir), endpoint, token),
                    name=f"{self.ctx.plugin_id}:ext-bootstrap",
                )
            return res
        if action == "close":
            raw_pid = payload.get("pid")
            pid_val: int | None = None
            if raw_pid is not None:
                if isinstance(raw_pid, bool) or not isinstance(raw_pid, int) or raw_pid <= 0:
                    return CapabilityResult.fail("pid 必须为正整数", error_code="invalid_argument")
                pid_val = raw_pid
            sid = str(payload.get("session_id") or "").strip() or None
            return await self._sessions.close_sessions(profile_dir=profile_dir, session_id=sid, pid=pid_val)
        if action == "status":
            return self._sessions.status(
                plugin_dir=self.ctx.plugin_dir,
                profile_dir=profile_dir,
                chrome_bin=chrome_bin,
                extension_enabled=ext_enabled,
                cdp_port=cdp_port,
            )
        return CapabilityResult.fail(
            f"不支持的 web.embed.v1 动作: {action!r}",
            data={"supported_actions": ["open", "close", "status"]},
            error_code="invalid_argument",
        )

    async def handle_cookies(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `web.cookies.v1` actions (`list`, `get`/`get_cookies`, `import`)."""
        assert self.ctx is not None
        payload = dict(req) if req is not None else {}
        cfg = self._cfg()
        try:
            profile_dir = self._profile_dir()
        except ValidationProblem as exc:
            return CapabilityResult.fail(str(exc), error_code=exc.error_code)
        # The live browser owns HttpOnly/session cookies which may not yet be
        # flushed to SQLite (and may be encrypted with an unavailable key).
        read_live = getattr(self._cdp_transport, 'get_cookies', None)
        if payload.get('action') in {'get', 'get_cookies'} and payload.get('url') and read_live:
            try:
                url = validate_http_url(payload['url'])
                sessions = [s for s in self._sessions.active_sessions() if s.profile_dir == profile_dir and s.cdp_port]
                for session in reversed(sessions):
                    targets = await self._cdp_transport.list_targets('127.0.0.1', session.cdp_port, timeout_sec=2)
                    target = next((t for t in targets if t.get('type') == 'page' and t.get('webSocketDebuggerUrl')), None)
                    if target:
                        cookies = await read_live(str(target['webSocketDebuggerUrl']), url, timeout_sec=3)
                        return CapabilityResult.ok({'cookies': cookies, 'count': len(cookies),
                            'cookie_header': '; '.join(f"{c['name']}={c['value']}" for c in cookies),
                            'cookie_source': 'chrome-live'})
            except (CdpError, ValidationProblem):
                pass
        return await asyncio.to_thread(
            handle_cookies_action,
            self.ctx,
            payload,
            profile_dir=profile_dir,
            default_import_target=str(cfg.get("cookie_import_target") or "vault"),
            default_cookie_file_path=str(cfg.get("cookie_file_path") or ""),
            decryptor=self._cookie_decryptor,
        )

    @browser_operation
    async def handle_extract(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `web.extract.v1` rendered DOM extraction exclusively via loopback CDP."""
        assert self.ctx is not None
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or ("extract" if "url" in payload else "")).strip()
        if action != "extract":
            return CapabilityResult.fail(
                f"不支持的 web.extract.v1 动作: {action!r}",
                data={"supported_actions": ["extract"]},
                error_code="invalid_argument",
            )
        try:
            url = validate_http_url(payload.get("url"))
            host, port = self._resolve_cdp_endpoint(payload)
            timeout_sec = self._resolve_timeout_sec(payload)
        except ValidationProblem as exc:
            return CapabilityResult.fail(str(exc), error_code=exc.error_code)

        try:
            targets = await request_targets(self, payload, url, timeout_sec)
            target, needs_nav = select_extract_target(targets, url)
            navigate_url = url if (needs_nav and payload.get("navigate", True) is not False) else None
            deadline = asyncio.get_running_loop().time() + timeout_sec
            html_value = ""
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise CdpError("等待网页正文加载超时", error_code="timeout")
                try:
                    eval_resp = await self._cdp_transport.evaluate(
                        str(target["webSocketDebuggerUrl"]),
                        "document.readyState !== 'loading' && document.documentElement ? document.documentElement.outerHTML : ''",
                        await_promise=False,
                        return_by_value=True,
                        navigate_url=navigate_url,
                        timeout_sec=remaining,
                    )
                    html_value = extract_eval_value(eval_resp)
                except CdpError as exc:
                    # A navigation can destroy the old JS realm before evaluation.
                    if not any(marker in str(exc).lower() for marker in ("context was destroyed", "cannot find context")):
                        raise
                navigate_url = None  # Retry reading, never restart the navigation.
                if isinstance(html_value, str) and html_value.strip():
                    break
                await asyncio.sleep(min(0.15, max(0, deadline - asyncio.get_running_loop().time())))
        except CdpError as exc:
            return CapabilityResult.fail(redact_sensitive(str(exc)), error_code=exc.error_code)

        if not isinstance(html_value, str) or not html_value.strip():
            return CapabilityResult.fail("CDP 未返回已渲染页面 DOM（拒绝使用 urllib 静默降级）", error_code="service_offline")

        target_url = str(target.get("url") or "")
        final_url = target_url if (not needs_nav and target_url.startswith(("http://", "https://"))) else url
        try:
            parsed = extract_from_html(html_value, base_url=final_url, fallback_title=str(target.get("title") or ""))
        except ValueError as exc:
            return CapabilityResult.fail(str(exc), error_code="response_too_large")
        translate_flag = bool(payload.get("translate") or payload.get("bilingual"))
        target_lang = str(payload.get("target_lang") or "zh-CN").strip() or "zh-CN"
        bilingual = await build_bilingual_segments(
            self.ctx, parsed["paragraphs"], title=parsed["title"], translate=translate_flag, target_lang=target_lang
        )
        return CapabilityResult.ok(
            {
                "url": final_url,
                "title": parsed["title"],
                "html": parsed["html"],
                "text": parsed["text"],
                "links": parsed["links"],
                "paragraphs": parsed["paragraphs"],
                "source": "cdp",
                **bilingual,
            }
        )

    @browser_operation
    async def handle_fetch(self, req: Mapping[str, Any]) -> CapabilityResult:
        """Handle `web.fetch.v1` same-origin `fetch()` via CDP `Runtime.evaluate` + `awaitPromise`."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or ("fetch" if "url" in payload else "")).strip()
        if action != "fetch":
            return CapabilityResult.fail(
                f"不支持的 web.fetch.v1 动作: {action!r}",
                data={"supported_actions": ["fetch"]},
                error_code="invalid_argument",
            )
        try:
            url = validate_http_url(payload.get("url"))
            host, port = self._resolve_cdp_endpoint(payload)
            timeout_sec = self._resolve_timeout_sec(payload)
            method, clean_headers, body_str, credentials, max_bytes = validate_fetch_params(payload, self._cfg())
        except ValidationProblem as exc:
            return CapabilityResult.fail(str(exc), error_code=exc.error_code)

        try:
            targets = await request_targets(self, payload, url, timeout_sec)
            navigate_url: str | None = None
            try:
                target = select_same_origin_target(targets, url)
            except CdpError as exc:
                if not payload.get("open_tab_if_missing") or exc.error_code != "permission_denied":
                    raise
                tab_url = str(payload.get("tab_url") or f"{origin_string(url)}/")
                tab_url = validate_http_url(tab_url, field_name="tab_url")
                if origin_string(tab_url) != origin_string(url):
                    raise CdpError(
                        "tab_url 与目标 URL 不同源，拒绝跨域 fetch",
                        error_code="permission_denied",
                    ) from None
                target, needs_nav = select_extract_target(targets, tab_url)
                navigate_url = tab_url if needs_nav else None
            if payload.get("headless") is True:
                timeout_sec = await wait_fetch_context(self, target, url, navigate_url, timeout_sec)
                navigate_url = None
            js_expr = build_fetch_js(
                url=url,
                expected_origin=origin_string(url),
                method=method,
                headers=clean_headers,
                body=body_str,
                credentials=credentials,
                max_bytes=max_bytes,
                timeout_ms=int(timeout_sec * 1000),
            )
            eval_resp = await self._cdp_transport.evaluate(
                str(target["webSocketDebuggerUrl"]),
                js_expr,
                await_promise=True,
                return_by_value=True,
                navigate_url=navigate_url,
                timeout_sec=timeout_sec,
            )
            raw_val = extract_eval_value(eval_resp)
        except CdpError as exc:
            return CapabilityResult.fail(redact_sensitive(str(exc)), error_code=exc.error_code)

        return parse_fetch_outcome(raw_val, expected_url=url, max_bytes=max_bytes)


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning a `ChromeWebPlugin` instance."""
    return ChromeWebPlugin()
