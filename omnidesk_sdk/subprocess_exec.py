"""Subprocess execution helper enforcing shell=False and process-group teardown."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import signal
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import SubprocessResult


def is_cancel_requested(cancel_token: Any) -> bool:
    if cancel_token is None:
        return False
    if callable(cancel_token):
        return bool(cancel_token())
    for attr in ("is_cancelled", "is_set", "cancelled"):
        member = getattr(cancel_token, attr, None)
        if member is not None:
            return bool(member() if callable(member) else member)
    return False


async def wait_for_cancel_token(cancel_token: Any) -> None:
    if is_cancel_requested(cancel_token):
        return
    wait_fn = getattr(cancel_token, "wait", None)
    if callable(wait_fn) and inspect.iscoroutinefunction(wait_fn):
        await wait_fn()
        return
    while not is_cancel_requested(cancel_token):
        await asyncio.sleep(0.02)


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _resolve_pgid(proc: asyncio.subprocess.Process) -> int | None:
    recorded = getattr(proc, "_omnidesk_pgid", None)
    if proc.returncode is not None:
        return recorded if isinstance(recorded, int) else None
    try:
        return os.getpgid(proc.pid)
    except ProcessLookupError:
        return None
    except OSError:
        return recorded if isinstance(recorded, int) else None


def signal_process_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    pgid = _resolve_pgid(proc)
    if pgid is None:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill() if sig == signal.SIGKILL else proc.terminate()
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except OSError:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill() if sig == signal.SIGKILL else proc.terminate()


async def terminate_process_group(proc: asyncio.subprocess.Process, *, grace_sec: float = 2.0) -> None:
    pgid = _resolve_pgid(proc)
    if proc.returncode is not None and (pgid is None or not _pgid_alive(pgid)):
        return
    timeout = max(0.05, grace_sec)
    deadline = time.monotonic() + timeout
    signal_process_group(proc, signal.SIGTERM)
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            signal_process_group(proc, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
    if pgid is not None and _pgid_alive(pgid):
        while _pgid_alive(pgid) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if _pgid_alive(pgid):
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(pgid, signal.SIGKILL)
            reap_deadline = time.monotonic() + 0.05
            while _pgid_alive(pgid) and time.monotonic() < reap_deadline:
                await asyncio.sleep(0.01)


async def run_isolated_subprocess(
    args: Sequence[str | os.PathLike[str]],
    *,
    tracked_set: set[asyncio.subprocess.Process],
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    stdin_data: bytes | str | None = None,
    timeout_sec: float | None = None,
    cancel_token: Any = None,
    grace_sec: float = 2.0,
) -> SubprocessResult:
    """Run an arg-list subprocess in its own session with SIGTERM->SIGKILL cleanup."""
    if isinstance(args, (str, bytes, os.PathLike)) or not isinstance(args, Sequence):
        raise TypeError("exec_subprocess args must be a non-empty sequence of strings (shell=False)")
    argv = [os.fspath(part) for part in args]
    if not argv or not argv[0]:
        raise ValueError("exec_subprocess args must contain an executable name")
    if is_cancel_requested(cancel_token):
        return SubprocessResult(returncode=-1, cancelled=True)

    stdin_bytes = stdin_data.encode("utf-8") if isinstance(stdin_data, str) else stdin_data
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=os.fspath(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    pgid = proc.pid
    with contextlib.suppress(OSError):
        pgid = os.getpgid(proc.pid)
    with contextlib.suppress(AttributeError):
        setattr(proc, "_omnidesk_pgid", pgid)
    tracked_set.add(proc)

    comm_task = asyncio.create_task(proc.communicate(input=stdin_bytes))
    cancel_task = asyncio.create_task(wait_for_cancel_token(cancel_token)) if cancel_token is not None else None
    timed_out = False
    cancelled_by_token = False
    try:
        waiters: set[asyncio.Task[Any]] = {comm_task}
        if cancel_task is not None:
            waiters.add(cancel_task)
        done, _ = await asyncio.wait(waiters, timeout=timeout_sec, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            timed_out = True
            await terminate_process_group(proc, grace_sec=grace_sec)
        elif cancel_task is not None and cancel_task in done and not comm_task.done():
            cancelled_by_token = True
            await terminate_process_group(proc, grace_sec=grace_sec)

        if not comm_task.done():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(comm_task), timeout=0.5)

        if comm_task.done() and not comm_task.cancelled():
            try:
                stdout_b, stderr_b = comm_task.result()
            except (BrokenPipeError, ConnectionResetError, OSError):
                if not (timed_out or cancelled_by_token):
                    raise
                stdout_b = b""
                stderr_b = b""
        else:
            comm_task.cancel()
            with contextlib.suppress(BaseException):
                await comm_task
            stdout_b = b""
            stderr_b = b""

        rc = proc.returncode if proc.returncode is not None else -1
        return SubprocessResult(
            returncode=rc,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            stdout_bytes=stdout_b,
            stderr_bytes=stderr_b,
            timed_out=timed_out,
            cancelled=cancelled_by_token,
        )
    except asyncio.CancelledError:
        effective_grace = getattr(proc, "_omnidesk_cleanup_grace", grace_sec)
        await terminate_process_group(proc, grace_sec=effective_grace)
        comm_task.cancel()
        with contextlib.suppress(BaseException):
            await comm_task
        raise
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
            with contextlib.suppress(BaseException):
                await cancel_task
        with contextlib.suppress(AttributeError):
            setattr(proc, "_omnidesk_pgid", None)
        tracked_set.discard(proc)
