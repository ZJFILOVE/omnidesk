"""Managed Chrome/Chromium `--app` subprocess launcher and teardown via PluginContext."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext, SubprocessResult

if __package__:
    from .security import (
        ValidationProblem,
        redact_sensitive,
        validate_cdp_port,
        validate_http_url,
        validate_safe_path,
    )
else:  # pragma: no cover - direct module import fallback
    from security import (
        ValidationProblem,
        redact_sensitive,
        validate_cdp_port,
        validate_http_url,
        validate_safe_path,
    )

_AUTO_CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


@dataclass(slots=True)
class ManagedChromeSession:
    """Tracks one Chrome `--app` subprocess managed through `ctx.exec_subprocess`."""

    session_id: str
    url: str
    profile_dir: Path
    cdp_port: int
    extension_enabled: bool
    argv: list[str]
    cancel_event: asyncio.Event
    finished_event: asyncio.Event
    task: asyncio.Task[Any]
    process: asyncio.subprocess.Process | None = None
    pid: int | None = None
    result: SubprocessResult | None = None
    error: BaseException | None = None
    closed_by_request: bool = False
    started_at: float = field(default_factory=lambda: asyncio.get_running_loop().time())

    @property
    def is_running(self) -> bool:
        if self.finished_event.is_set() or self.task.done():
            return False
        if self.process is not None:
            return self.process.returncode is None
        return True

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "url": self.url,
            "profile_dir": str(self.profile_dir),
            "cdp_port": self.cdp_port,
            "extension_enabled": self.extension_enabled,
            "running": self.is_running,
        }


def resolve_chrome_bin(configured_bin: Any) -> str | None:
    """Resolve Chrome/Chromium executable safely without falling back on explicit missing paths."""
    raw = str(configured_bin or "").strip()
    if not raw or "\x00" in raw:
        return None
    if "/" in raw or "\\" in raw:
        expanded = Path(raw).expanduser()
        if ".." in expanded.parts:
            return None
        if expanded.is_file() and os.access(expanded, os.X_OK):
            return str(expanded)
        return None
    if raw == "auto":
        for candidate in _AUTO_CHROME_CANDIDATES:
            found = shutil.which(candidate)
            if found:
                return found
        return None
    return shutil.which(raw)


def resolve_extension_dir(plugin_dir: Path) -> Path | None:
    """Return validated MV3 extension directory under `plugin_dir/resources/mv3-extension`."""
    try:
        ext_dir = validate_safe_path("resources/mv3-extension", base_dir=plugin_dir, check_symlinks=True)
    except ValidationProblem:
        return None
    manifest = ext_dir / "manifest.json"
    if ext_dir.is_dir() and manifest.is_file() and not manifest.is_symlink():
        return ext_dir
    return None


def build_chrome_args(
    chrome_executable: str,
    *,
    url: str,
    profile_dir: Path,
    extension_dir: Path | None = None,
    cdp_port: int = 0,
) -> list[str]:
    """Build arg-list for Chrome `--app` window (`shell=False`)."""
    args = [
        chrome_executable,
        f"--user-data-dir={profile_dir}",
        f"--app={url}",
        "--class=omnidesk-reader",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if extension_dir is not None:
        args.append(f"--load-extension={extension_dir}")
    if cdp_port > 0:
        args.append(f"--remote-debugging-port={cdp_port}")
        args.append("--remote-debugging-address=127.0.0.1")
    return args


class ChromeSessionManager:
    """Manages Chrome `--app` subprocesses via `PluginContext.exec_subprocess`."""

    def __init__(self) -> None:
        self._sessions: dict[str, ManagedChromeSession] = {}

    def active_sessions(self) -> list[ManagedChromeSession]:
        return [s for s in self._sessions.values() if s.is_running]

    def latest_session(self) -> ManagedChromeSession | None:
        active = self.active_sessions()
        if active:
            return active[-1]
        if self._sessions:
            return list(self._sessions.values())[-1]
        return None

    async def open_app(
        self,
        ctx: PluginContext,
        *,
        raw_url: Any,
        chrome_bin: str,
        profile_dir: Path,
        extension_enabled: bool,
        cdp_port: int,
    ) -> CapabilityResult:
        """Validate URL and launch managed Chrome `--app` window via `ctx.exec_subprocess`."""
        try:
            url = validate_http_url(raw_url)
            valid_port = validate_cdp_port(cdp_port, allow_zero=True)
        except ValidationProblem as exc:
            return CapabilityResult.fail(
                str(exc),
                data={"launched": False, "pid": None, "url": "", "profile_dir": str(profile_dir)},
                error_code=exc.error_code,
            )

        executable = resolve_chrome_bin(chrome_bin)
        if executable is None:
            return CapabilityResult.fail(
                "未找到 Chrome/Chromium 可执行文件，请检查 chrome_bin 配置",
                data={"launched": False, "pid": None, "url": url, "profile_dir": str(profile_dir)},
                error_code="not_configured",
            )

        if profile_dir.is_symlink() or profile_dir.parent.is_symlink():
            return CapabilityResult.fail(
                "受管 Chrome profile 路径不得为符号链接",
                data={"launched": False, "pid": None, "url": url, "profile_dir": str(profile_dir)},
                error_code="path_safety",
            )
        profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        ext_dir = resolve_extension_dir(ctx.plugin_dir) if extension_enabled else None
        argv = build_chrome_args(
            executable,
            url=url,
            profile_dir=profile_dir,
            extension_dir=ext_dir,
            cdp_port=valid_port,
        )

        session_id = uuid.uuid4().hex[:12]
        cancel_event = asyncio.Event()
        finished_event = asyncio.Event()
        known_procs = {id(s.process) for s in self._sessions.values() if s.process is not None}
        before_procs = set(ctx.tracked_resources().get("subprocesses", ()))

        runner_state: dict[str, Any] = {"result": None, "error": None}

        async def _runner() -> None:
            try:
                runner_state["result"] = await ctx.exec_subprocess(
                    argv,
                    cancel_token=cancel_event,
                    grace_sec=2.0,
                )
            except BaseException as exc:
                runner_state["error"] = exc
            finally:
                finished_event.set()

        task = ctx.spawn_task(_runner(), name=f"{ctx.plugin_id}:chrome:{session_id}")
        captured_proc: asyncio.subprocess.Process | None = None
        loop = asyncio.get_running_loop()
        detect_deadline = loop.time() + 2.0
        while loop.time() < detect_deadline:
            current = set(ctx.tracked_resources().get("subprocesses", ())) - before_procs
            for cand in current:
                if id(cand) not in known_procs:
                    captured_proc = cand
                    break
            if captured_proc is not None or finished_event.is_set() or task.done():
                break
            await asyncio.sleep(0.001)

        if captured_proc is not None and not finished_event.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(finished_event.wait(), timeout=0.15)

        if finished_event.is_set():
            err = runner_state["error"]
            res = runner_state["result"]
            if isinstance(err, (FileNotFoundError, PermissionError, OSError)):
                return CapabilityResult.fail(
                    "无法启动 Chrome/Chromium 可执行文件，请检查 chrome_bin 配置",
                    data={"launched": False, "pid": None, "url": url, "profile_dir": str(profile_dir)},
                    error_code="not_configured",
                )
            if err is not None:
                return CapabilityResult.fail(
                    redact_sensitive(f"启动 Chrome 进程失败: {err}"),
                    data={"launched": False, "pid": None, "url": url, "profile_dir": str(profile_dir)},
                    error_code="service_offline",
                )
            if isinstance(res, SubprocessResult) and res.returncode != 0 and not res.cancelled:
                detail = redact_sensitive(res.stderr.strip()[:200])
                msg = f"Chrome 启动后异常退出 (rc={res.returncode})" + (f": {detail}" if detail else "")
                return CapabilityResult.fail(
                    msg,
                    data={
                        "launched": False,
                        "pid": captured_proc.pid if captured_proc is not None else None,
                        "url": url,
                        "profile_dir": str(profile_dir),
                    },
                    error_code="service_offline",
                )

        pid = captured_proc.pid if captured_proc is not None else None
        session = ManagedChromeSession(
            session_id=session_id,
            url=url,
            profile_dir=profile_dir,
            cdp_port=valid_port,
            extension_enabled=ext_dir is not None,
            argv=argv,
            cancel_event=cancel_event,
            finished_event=finished_event,
            task=task,
            process=captured_proc,
            pid=pid,
            result=runner_state["result"],
            error=runner_state["error"],
        )
        self._sessions[session_id] = session
        return CapabilityResult.ok(
            {
                "launched": True,
                "running": session.is_running,
                "pid": pid,
                "session_id": session_id,
                "url": url,
                "profile_dir": str(profile_dir),
                "cdp_port": valid_port,
                "extension_enabled": ext_dir is not None,
                "extension_dir": str(ext_dir) if ext_dir is not None else "",
                "args": list(argv),
            }
        )

    async def _terminate_session(self, session: ManagedChromeSession) -> None:
        session.closed_by_request = True
        session.cancel_event.set()
        if not session.finished_event.is_set():
            try:
                await asyncio.wait_for(session.finished_event.wait(), timeout=3.0)
            except TimeoutError:
                session.task.cancel()
                with contextlib.suppress(BaseException):
                    await session.task

    async def close_sessions(
        self,
        *,
        profile_dir: Path,
        session_id: str | None = None,
        pid: int | None = None,
    ) -> CapabilityResult:
        """Terminate one or all running managed Chrome `--app` sessions."""
        targets: list[ManagedChromeSession] = []
        for sess in self._sessions.values():
            if not sess.is_running:
                continue
            if session_id and sess.session_id != session_id:
                continue
            if pid is not None and sess.pid != pid:
                continue
            targets.append(sess)

        closed_pids: list[int] = []
        closed_ids: list[str] = []
        if targets:
            await asyncio.gather(*(self._terminate_session(s) for s in targets), return_exceptions=True)
            for s in targets:
                closed_ids.append(s.session_id)
                if s.pid is not None:
                    closed_pids.append(s.pid)

        still_running = self.active_sessions()
        return CapabilityResult.ok(
            {
                "closed": bool(targets),
                "closed_count": len(targets),
                "closed_pids": closed_pids,
                "closed_sessions": closed_ids,
                "running": bool(still_running),
                "pid": still_running[-1].pid if still_running else None,
                "profile_dir": str(profile_dir),
            }
        )

    def status(
        self,
        *,
        plugin_dir: Path,
        profile_dir: Path,
        chrome_bin: str,
        extension_enabled: bool,
        cdp_port: int,
    ) -> CapabilityResult:
        """Return current managed Chrome process status without spawning processes."""
        active = self.active_sessions()
        latest = active[-1] if active else self.latest_session()
        resolved_bin = resolve_chrome_bin(chrome_bin)
        ext_dir = resolve_extension_dir(plugin_dir) if extension_enabled else None
        return CapabilityResult.ok(
            {
                "launched": latest is not None,
                "running": bool(active),
                "pid": active[-1].pid if active else (latest.pid if latest else None),
                "url": active[-1].url if active else (latest.url if latest else ""),
                "profile_dir": str(profile_dir),
                "chrome_bin": chrome_bin,
                "chrome_found": resolved_bin is not None,
                "cdp_port": cdp_port,
                "extension_enabled": extension_enabled,
                "extension_ready": ext_dir is not None,
                "active_count": len(active),
                "sessions": [s.summary() for s in active],
            }
        )

    async def shutdown_all(self) -> None:
        """Terminate all managed Chrome sessions during plugin unload."""
        active = self.active_sessions()
        if active:
            await asyncio.gather(*(self._terminate_session(s) for s in active), return_exceptions=True)
        self._sessions.clear()
