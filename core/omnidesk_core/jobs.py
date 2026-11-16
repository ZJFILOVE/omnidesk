"""Background job lifecycle manager with cancellation tokens and EntityBus progress events."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .bus import EntityBus
from .errors import ErrorCode, InvalidArgumentError, JobCancelledError, JobNotFoundError, JobStateError, OmniDeskError, redact_secrets
from .paths import validate_plugin_id

logger = logging.getLogger(__name__)

JOB_CREATED_TOPIC = "job.created"
JOB_PROGRESS_TOPIC = "job.progress"
JOB_COMPLETED_TOPIC = "job.completed"
JOB_FAILED_TOPIC = "job.failed"
JOB_CANCELLED_TOPIC = "job.cancelled"


class JobStatus(StrEnum):
    """Lifecycle states for a managed background job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})


class CancellationToken:
    """Cooperative cancellation signal shared between JobCenter and workers/subprocesses."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._reason = ""
        self._callbacks: list[Callable[[], Any]] = []
        self._async_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    cancelled = is_cancelled

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "cancelled") -> bool:
        """Mark the token cancelled and notify registered waiters/callbacks."""
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled, self._reason = True, (reason or "cancelled")
            callbacks, waiters = list(self._callbacks), list(self._async_waiters)
            self._callbacks.clear()
            self._async_waiters.clear()
        for loop, evt in waiters:
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(evt.set)
                except RuntimeError:
                    pass
        for cb in callbacks:
            try:
                cb()
            except Exception:
                logger.exception("CancellationToken callback failed")
        return True

    def raise_if_cancelled(self, job_id: str = "") -> None:
        """Raise JobCancelledError if cancellation has been requested."""
        with self._lock:
            if not self._cancelled:
                return
            reason = self._reason
        raise JobCancelledError(f"Job cancelled: {reason}", job_id=job_id)

    def on_cancel(self, callback: Callable[[], Any]) -> None:
        if not callable(callback):
            raise InvalidArgumentError("Cancellation callback must be callable")
        with self._lock:
            if not self._cancelled:
                self._callbacks.append(callback)
                return
        callback()

    async def wait(self) -> None:
        loop, evt = asyncio.get_running_loop(), asyncio.Event()
        pair = (loop, evt)
        with self._lock:
            if self._cancelled:
                return
            self._async_waiters.append(pair)
        try:
            await evt.wait()
        finally:
            with self._lock:
                try:
                    self._async_waiters.remove(pair)
                except ValueError:
                    pass


@dataclass(frozen=True, slots=True)
class JobRecord:
    """Immutable snapshot of a tracked background job."""

    job_id: str
    kind: str
    title: str
    plugin_id: str
    status: JobStatus
    progress: float = 0.0
    stage: str = "pending"
    message: str = ""
    completed_steps: int | None = None
    total_steps: int | None = None
    result: Any = None
    error: str = ""
    error_code: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    progress_topic: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "kind": self.kind, "title": self.title, "plugin_id": self.plugin_id,
            "status": str(self.status), "progress": self.progress, "stage": self.stage, "message": self.message,
            "completed_steps": self.completed_steps, "total_steps": self.total_steps, "result": self.result,
            "error": self.error, "error_code": self.error_code, "metadata": dict(self.metadata),
            "created_at": self.created_at, "updated_at": self.updated_at, "started_at": self.started_at, "finished_at": self.finished_at,
        }


class JobContext:
    """Execution handle passed to background workers launched via `JobCenter.submit`."""

    def __init__(self, center: JobCenter, job_id: str, token: CancellationToken) -> None:
        self._center, self.job_id, self.token = center, job_id, token

    @property
    def is_cancelled(self) -> bool:
        return self.token.is_cancelled

    def raise_if_cancelled(self) -> None:
        self.token.raise_if_cancelled(self.job_id)

    async def update(
        self,
        *,
        progress: float | None = None,
        completed_steps: int | None = None,
        total_steps: int | None = None,
        stage: str | None = None,
        message: str | None = None,
        metadata_patch: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        self.raise_if_cancelled()
        return await self._center.update(
            self.job_id, progress=progress, completed_steps=completed_steps, total_steps=total_steps,
            stage=stage, message=message, metadata_patch=metadata_patch,
        )


def _validate_progress(progress: float | int) -> float:
    if isinstance(progress, bool) or not isinstance(progress, (int, float)):
        raise InvalidArgumentError("progress must be a numeric float between 0.0 and 1.0")
    val = float(progress)
    if not math.isfinite(val) or val < 0.0 or val > 1.0:
        raise InvalidArgumentError(f"progress must be between 0.0 and 1.0, got {progress!r}")
    return val


def _validate_step(value: int, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidArgumentError(f"{name} must be an integer >= {minimum}, got {value!r}")
    return value


def _validate_timeout(timeout: float | int | None) -> float | None:
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise InvalidArgumentError(f"timeout must be a finite number > 0, got {timeout!r}")
    val = float(timeout)
    if not math.isfinite(val) or val <= 0.0:
        raise InvalidArgumentError(f"timeout must be a finite number > 0, got {timeout!r}")
    return val


def _worker_accepts_ctx(worker: Callable[..., Any]) -> bool:
    try:
        sig = inspect.signature(worker)
    except (TypeError, ValueError):
        return True
    pos_kinds = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.VAR_POSITIONAL,
    )
    return any(p.kind in pos_kinds for p in sig.parameters.values())


class JobCenter:
    """Manages job state transitions, cancellation, and EntityBus progress notifications."""

    def __init__(self, *, bus: EntityBus | None = None, secret_provider: Callable[[], Iterable[str]] | None = None) -> None:
        self._bus, self._secret_provider = bus, secret_provider
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def _extra_secrets(self) -> Iterable[str]:
        if self._secret_provider is None:
            return ()
        try:
            return self._secret_provider()
        except Exception:
            return ()

    async def _publish(self, topic: str, record: JobRecord) -> None:
        if self._bus is not None:
            await self._bus.publish(topic, record.to_dict(), source_plugin_id=record.plugin_id)

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def require(self, job_id: str) -> JobRecord:
        if (record := self.get(job_id)) is None:
            raise JobNotFoundError(f"Job not found: {job_id}", job_id=job_id)
        return record

    def _require_non_terminal(self, job_id: str, action: str) -> JobRecord:
        current = self.require(job_id)
        if current.is_terminal:
            raise JobStateError(f"Cannot {action} job {job_id!r} in terminal state {str(current.status)!r}", job_id=job_id)
        return current

    def get_token(self, job_id: str) -> CancellationToken:
        with self._lock:
            if (token := self._tokens.get(job_id)) is None:
                raise JobNotFoundError(f"Job not found: {job_id}", job_id=job_id)
            return token

    def list_jobs(
        self, *, plugin_id: str | None = None, status: JobStatus | str | None = None, kind: str | None = None
    ) -> list[JobRecord]:
        if status is not None:
            try:
                target_status: JobStatus | None = JobStatus(status)
            except ValueError as exc:
                raise InvalidArgumentError(f"Unsupported job status {status!r}") from exc
        else:
            target_status = None
        with self._lock:
            jobs = list(self._jobs.values())
        if plugin_id is not None:
            jobs = [j for j in jobs if j.plugin_id == plugin_id]
        if target_status is not None:
            jobs = [j for j in jobs if j.status == target_status]
        if kind is not None:
            jobs = [j for j in jobs if j.kind == kind]
        return jobs

    async def create(
        self,
        kind: str,
        title: str = "",
        *,
        plugin_id: str = "core",
        job_id: str | None = None,
        total_steps: int | None = None,
        stage: str | None = None,
        message: str = "",
        metadata: Mapping[str, Any] | None = None,
        progress_topic: str | None = None,
        start: bool = False,
    ) -> JobRecord:
        """Create a new job in PENDING (or RUNNING if `start=True`) and emit `job.created`."""
        if not isinstance(kind, str) or not kind.strip():
            raise InvalidArgumentError("Job kind must be a non-empty string")
        pid = validate_plugin_id(plugin_id)
        if job_id is None:
            jid = uuid.uuid4().hex
        elif isinstance(job_id, str):
            jid = job_id.strip()
        else:
            raise InvalidArgumentError("job_id must be a non-empty string")
        if not jid:
            raise InvalidArgumentError("job_id must be a non-empty string")
        steps = _validate_step(total_steps, "total_steps", minimum=1) if total_steps is not None else None
        now = time.time()
        topic = progress_topic.strip() if isinstance(progress_topic, str) and progress_topic.strip() else None
        record = JobRecord(
            job_id=jid, kind=kind.strip(), title=title or kind.strip(), plugin_id=pid,
            status=JobStatus.RUNNING if start else JobStatus.PENDING, progress=0.0,
            stage=stage if stage is not None else ("running" if start else "pending"), message=message,
            completed_steps=0 if steps is not None else None, total_steps=steps,
            metadata=dict(metadata) if metadata else {}, progress_topic=topic,
            created_at=now, updated_at=now, started_at=now if start else None,
        )
        with self._lock:
            if jid in self._jobs:
                raise JobStateError(f"Job already exists: {jid}", job_id=jid)
            self._jobs[jid], self._tokens[jid] = record, CancellationToken()
        await self._publish(JOB_CREATED_TOPIC, record)
        return record

    async def start(self, job_id: str, *, stage: str = "running", message: str = "") -> JobRecord:
        """Transition a PENDING job to RUNNING and emit `job.progress`."""
        now = time.time()
        with self._lock:
            current = self.require(job_id)
            if current.status != JobStatus.PENDING:
                raise JobStateError(f"Cannot start job {job_id!r} in state {str(current.status)!r}", job_id=job_id)
            updated = replace(
                current, status=JobStatus.RUNNING, stage=stage, message=message or current.message, started_at=now, updated_at=now
            )
            self._jobs[job_id] = updated
        await self._emit_progress_topics(updated)
        return updated

    async def _emit_progress_topics(self, record: JobRecord) -> None:
        await self._publish(JOB_PROGRESS_TOPIC, record)
        if record.progress_topic and record.progress_topic != JOB_PROGRESS_TOPIC:
            await self._publish(record.progress_topic, record)

    async def update(
        self,
        job_id: str,
        *,
        progress: float | None = None,
        completed_steps: int | None = None,
        total_steps: int | None = None,
        stage: str | None = None,
        message: str | None = None,
        metadata_patch: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        """Update job progress/stage/message and publish progress events."""
        now = time.time()
        with self._lock:
            current = self.require(job_id)
            if current.status == JobStatus.CANCELLED or self._tokens[job_id].is_cancelled:
                raise JobCancelledError(f"Cannot update cancelled job {job_id!r}", job_id=job_id)
            if current.is_terminal:
                raise JobStateError(f"Cannot update job {job_id!r} in terminal state {str(current.status)!r}", job_id=job_id)
            new_total = _validate_step(total_steps, "total_steps", minimum=1) if total_steps is not None else current.total_steps
            new_completed = _validate_step(completed_steps, "completed_steps", minimum=0) if completed_steps is not None else current.completed_steps
            if new_total is not None and new_completed is None:
                new_completed = 0
            if progress is not None:
                new_progress = _validate_progress(progress)
            elif (completed_steps is not None or total_steps is not None) and new_completed is not None and new_total is not None:
                new_progress = min(1.0, new_completed / new_total)
            else:
                new_progress = current.progress
            merged_meta = {**current.metadata, **metadata_patch} if metadata_patch else dict(current.metadata)
            next_stage = stage if stage is not None else (current.stage if current.status == JobStatus.RUNNING else "running")
            updated = replace(
                current, status=JobStatus.RUNNING, progress=new_progress, stage=next_stage,
                message=message if message is not None else current.message,
                completed_steps=new_completed, total_steps=new_total, metadata=merged_meta,
                started_at=current.started_at or now, updated_at=now,
            )
            self._jobs[job_id] = updated
        await self._emit_progress_topics(updated)
        return updated

    async def complete(self, job_id: str, *, result: Any = None, message: str = "completed", stage: str = "done") -> JobRecord:
        """Mark a job COMPLETED at progress=1.0 and emit `job.completed`."""
        now = time.time()
        with self._lock:
            current = self._require_non_terminal(job_id, "complete")
            updated = replace(
                current, status=JobStatus.COMPLETED, progress=1.0, stage=stage, message=message,
                completed_steps=current.total_steps if current.total_steps is not None else current.completed_steps,
                result=result, started_at=current.started_at or now, updated_at=now, finished_at=now,
            )
            self._jobs[job_id] = updated
        await self._publish(JOB_COMPLETED_TOPIC, updated)
        return updated

    async def fail(
        self, job_id: str, error: str | BaseException, *, error_code: str | ErrorCode | None = None, stage: str = "failed", emit_fault: bool = False
    ) -> JobRecord:
        """Mark a job FAILED with secret-redacted error info and emit `job.failed`."""
        raw_error = error.message if isinstance(error, OmniDeskError) else (error if isinstance(error, str) else str(error) or type(error).__name__)
        safe_error = redact_secrets(raw_error or "job failed", self._extra_secrets())
        resolved_code = str(error_code) if error_code is not None else (
            error.code if isinstance(error, OmniDeskError) else str(ErrorCode.PLUGIN_FAULT)
        )
        now = time.time()
        with self._lock:
            current = self._require_non_terminal(job_id, "fail")
            updated = replace(
                current, status=JobStatus.FAILED, stage=stage, message=safe_error, error=safe_error,
                error_code=resolved_code, started_at=current.started_at or now, updated_at=now, finished_at=now,
            )
            self._jobs[job_id] = updated
        await self._publish(JOB_FAILED_TOPIC, updated)
        if emit_fault and self._bus is not None:
            await self._bus.emit_fault(plugin_id=updated.plugin_id, component=f"job:{updated.kind}", error=safe_error)
        return updated

    async def cancel(self, job_id: str, *, reason: str = "cancelled") -> JobRecord:
        """Cancel a pending/running job, trigger its token/task, and emit `job.cancelled`."""
        safe_reason = redact_secrets(reason or "cancelled", self._extra_secrets())
        now = time.time()
        already_cancelled = False
        token: CancellationToken | None = None
        task: asyncio.Task[Any] | None = None
        with self._lock:
            current = self.require(job_id)
            if current.status == JobStatus.CANCELLED:
                already_cancelled = True
                task = self._tasks.get(job_id)
                updated = current
            elif current.is_terminal:
                raise JobStateError(f"Cannot cancel job {job_id!r} in terminal state {str(current.status)!r}", job_id=job_id)
            else:
                token = self._tokens[job_id]
                task = self._tasks.get(job_id)
                updated = replace(
                    current, status=JobStatus.CANCELLED, stage="cancelled", message=safe_reason,
                    error_code=str(ErrorCode.CANCELLED), updated_at=now, finished_at=now,
                )
                self._jobs[job_id] = updated
        if already_cancelled:
            if task is not None and not task.done() and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
            return updated
        if token is not None:
            token.cancel(safe_reason)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._publish(JOB_CANCELLED_TOPIC, updated)
        return updated

    async def cancel_plugin(self, plugin_id: str, *, reason: str = "plugin unloaded") -> int:
        """Cancel all active (pending/running) jobs owned by `plugin_id`."""
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise InvalidArgumentError("plugin_id must be a non-empty string")
        with self._lock:
            active_ids = [j.job_id for j in self._jobs.values() if j.plugin_id == plugin_id and not j.is_terminal]
        cancelled_count = 0
        for jid in active_ids:
            current = self.get(jid)
            if current is None or current.is_terminal:
                continue
            try:
                await self.cancel(jid, reason=reason)
                cancelled_count += 1
            except JobStateError:
                continue
        return cancelled_count

    async def submit(
        self,
        kind: str,
        worker: Callable[..., Any],
        *,
        title: str = "",
        plugin_id: str = "core",
        job_id: str | None = None,
        total_steps: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        progress_topic: str | None = None,
    ) -> JobRecord:
        """Create and schedule a background job executed by `worker(JobContext)`."""
        if not callable(worker):
            raise InvalidArgumentError("Job worker must be callable")
        record = await self.create(
            kind, title=title, plugin_id=plugin_id, job_id=job_id, total_steps=total_steps,
            stage="running", metadata=metadata, progress_topic=progress_topic, start=True,
        )
        token = self.get_token(record.job_id)
        ctx = JobContext(self, record.job_id, token)
        takes_ctx = _worker_accepts_ctx(worker)

        async def _runner() -> None:
            try:
                raw = worker(ctx) if takes_ctx else worker()
                if inspect.isawaitable(raw):
                    raw = await raw
                if (cur := self.get(record.job_id)) is not None and not cur.is_terminal:
                    await self.complete(record.job_id, result=raw)
            except (asyncio.CancelledError, JobCancelledError) as exc:
                if (cur := self.get(record.job_id)) is not None and not cur.is_terminal:
                    await self.cancel(record.job_id, reason=str(exc) or "cancelled")
            except Exception as exc:
                if (cur := self.get(record.job_id)) is not None and not cur.is_terminal:
                    await self.fail(record.job_id, exc, emit_fault=True)
            finally:
                with self._lock:
                    self._tasks.pop(record.job_id, None)

        with self._lock:
            latest = self._jobs[record.job_id]
            if latest.is_terminal or token.is_cancelled:
                return latest
            task = asyncio.create_task(_runner())
            self._tasks[record.job_id] = task
            task.add_done_callback(lambda _t, jid=record.job_id: self._clear_task(jid, _t))
        return record

    def _clear_task(self, job_id: str, task: asyncio.Task[Any]) -> None:
        with self._lock:
            if self._tasks.get(job_id) is task:
                self._tasks.pop(job_id, None)

    async def wait(self, job_id: str, *, timeout: float | None = None) -> JobRecord:
        """Await settlement of a background job launched by `submit` and return its JobRecord."""
        effective_timeout = _validate_timeout(timeout)
        self.require(job_id)
        with self._lock:
            task = self._tasks.get(job_id)
        if task is not None:
            async with asyncio.timeout(effective_timeout):
                await asyncio.shield(task)
        return self.require(job_id)
