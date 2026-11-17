"""Serialized browser operations and windowless startup for background requests."""
from __future__ import annotations

import asyncio
import json
import os
import socket
from functools import wraps
from typing import Any

if __package__:
    from .cdp_client import CdpError, extract_eval_value
    from .security import ValidationProblem, origin_string, validate_http_url
else:
    from cdp_client import CdpError, extract_eval_value
    from security import ValidationProblem, origin_string, validate_http_url


def browser_operation(method):
    """Keep profile mode changes and page navigation from racing each other."""
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self._browser_lock:
            return await method(self, *args, **kwargs)
    return wrapped


def profile_in_use(profile) -> bool:
    """Let Chrome recover its own stale Linux lock, but never forward to a live GUI."""
    lock = profile / "SingletonLock"
    if not os.path.lexists(lock):
        return False
    try:
        hostname, raw_pid = os.readlink(lock).rsplit("-", 1)
        if hostname != socket.gethostname() or not raw_pid.isdigit() or int(raw_pid) <= 0:
            return True
        os.kill(int(raw_pid), 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError):
        return True
    return True


async def close_headless(plugin: Any, *, profile_dir=None, session_id=None, pid=None) -> None:
    """Flush cookies through Browser.close before subprocess teardown is needed."""
    close = getattr(plugin._cdp_transport, "close_browser", None)
    if close is None:
        return
    for session in plugin._sessions.active_sessions():
        if not session.headless or (profile_dir is not None and session.profile_dir != profile_dir):
            continue
        if (session_id and session.session_id != session_id) or (pid is not None and session.pid != pid):
            continue
        try:
            targets = await plugin._cdp_transport.list_targets("127.0.0.1", session.cdp_port, timeout_sec=1.0)
            target = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
            if target is None:
                continue
            session.closed_by_request = True
            try:
                await close(str(target["webSocketDebuggerUrl"]), timeout_sec=2.0)
            except CdpError:
                pass  # Chrome may close the socket before acknowledging Browser.close.
            await asyncio.wait_for(session.finished_event.wait(), timeout=2.0)
        except (CdpError, TimeoutError):
            pass  # The existing managed-process cleanup remains the last resort.


async def wait_fetch_context(plugin: Any, target: dict, url: str, navigate_url: str | None, timeout_sec: float) -> float:
    """CDP advertises a new tab before its initial about:blank realm is replaced."""
    deadline = asyncio.get_running_loop().time() + timeout_sec
    expression = f"window.location.origin === {json.dumps(origin_string(url))} && document.readyState !== 'loading'"
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise CdpError("等待后台网页加载超时", error_code="timeout")
        try:
            response = await plugin._cdp_transport.evaluate(
                str(target["webSocketDebuggerUrl"]), expression, navigate_url=navigate_url, timeout_sec=remaining,
            )
            if extract_eval_value(response) is True:
                return max(0.05, deadline - asyncio.get_running_loop().time())
        except CdpError as exc:
            if not any(marker in str(exc).lower() for marker in ("context was destroyed", "cannot find context")):
                raise
        navigate_url = None
        await asyncio.sleep(min(0.1, max(0, deadline - asyncio.get_running_loop().time())))


async def request_targets(plugin: Any, payload: dict, url: str, timeout_sec: float) -> list[dict]:
    try:
        host, port = plugin._resolve_cdp_endpoint(payload)
        profile = plugin._profile_dir()
        tab_url = validate_http_url(str(payload.get("tab_url") or f"{origin_string(url)}/"), field_name="tab_url")
        if origin_string(tab_url) != origin_string(url):
            raise CdpError("tab_url 与目标 URL 不同源", error_code="permission_denied")
    except ValidationProblem as exc:
        raise CdpError(str(exc), error_code=exc.error_code) from None
    headless = payload.get("headless") is True
    sessions = [s for s in plugin._sessions.active_sessions() if s.profile_dir == profile]
    visible = [s for s in sessions if not s.headless]
    if headless and visible:
        # Explicit opt-in: use the verified browser itself, retaining its cookies
        # and browser identity. Never navigate an unrelated visible login page.
        if payload.get("reuse_visible_session") is not True or not any(s.cdp_port == port for s in visible):
            raise CdpError("请完成并关闭 Chrome 登录窗口后重试后台拉取")
        targets = await plugin._cdp_transport.list_targets(host, port, timeout_sec=timeout_sec)
        matching = [t for t in targets if t.get("type") == "page" and
                    (str(t.get("url", "")) == origin_string(url) or
                     str(t.get("url", "")).startswith(origin_string(url) + "/"))]
        if not matching:
            raise CdpError("请在已打开的 Chrome 窗口中完成当前站点验证后重试", error_code="verification_required")
        return matching
    try:
        targets = await plugin._cdp_transport.list_targets(host, port, timeout_sec=timeout_sec)
    except CdpError as exc:
        if exc.error_code != "service_offline" or not (headless or payload.get("open_tab_if_missing")):
            raise
    else:
        if headless and not any(s.headless and s.cdp_port == port for s in sessions):
            raise CdpError("CDP 端口正由其他浏览器占用，请关闭该浏览器后重试后台拉取")
        return targets

    # A session may still be starting. Reuse it instead of spawning twice.
    if not any(s.headless and s.cdp_port == port for s in sessions):
        # Chromium otherwise forwards the URL to an already-running GUI process,
        # even when the new invocation specifies --headless.
        if profile_in_use(profile):
            raise CdpError("Chrome 会话目录正在使用中，请关闭该会话的浏览器后重试后台拉取")
        cfg = plugin._cfg()
        opened = await plugin._sessions.open_app(
            plugin.ctx, raw_url=tab_url,
            chrome_bin=str(cfg.get("chrome_bin", "google-chrome")),
            profile_dir=profile, extension_enabled=False, cdp_port=port, headless=True,
        )
        if not opened.ok:
            raise CdpError(opened.error or "后台浏览器启动失败", error_code=str(opened.data.get("error_code", "service_offline")))
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise CdpError("等待后台浏览器启动超时", error_code="timeout")
        try:
            targets = await plugin._cdp_transport.list_targets(host, port, timeout_sec=min(remaining, 1.0))
            if any(t.get("type") == "page" for t in targets):
                return targets
        except CdpError as exc:
            if exc.error_code not in {"service_offline", "timeout"}:
                raise
        await asyncio.sleep(min(0.15, max(0, deadline - asyncio.get_running_loop().time())))
