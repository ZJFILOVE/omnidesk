"""Atomic 0600 job persistence and fcntl.flock operation_id deduplication."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

if __package__:
    from . import pdf_utils
else:
    import pdf_utils  # type: ignore[import-not-found,no-redef]

JOB_STATES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


class DuplicateOperationError(RuntimeError):
    """Raised when an operation_id has already been submitted."""

    def __init__(self, operation_id: str, existing_job: dict[str, Any]) -> None:
        self.operation_id = operation_id
        self.existing_job = existing_job
        super().__init__(f"Duplicate operation_id {operation_id!r}")


def _is_shared_root(path: Path) -> bool:
    resolved = path.resolve()
    protected = {Path("/"), Path("/tmp"), Path("/var/tmp"), Path.home().resolve()}
    for env_name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            protected.add(Path(raw).expanduser().resolve())
    return resolved in protected


def ensure_private_dir(path: Path) -> Path:
    """Create a directory with 0700 permissions and refuse symlinked directories."""
    if path.is_symlink():
        raise pdf_utils.PathContainmentError(f"Refusing symlinked directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _is_shared_root(path):
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
    return path


def atomic_write_text_0600(target: Path, content: str) -> None:
    """Atomically write UTF-8 text to `target` with strict 0600 permissions."""
    if target.is_symlink():
        raise pdf_utils.PathContainmentError(f"Refusing symlinked target: {target}")
    ensure_private_dir(target.parent)
    tmp_path: Path | None = None
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp_path = Path(tmp.name)
        os.fchmod(tmp.fileno(), 0o600)
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, target)
        os.chmod(target, 0o600)
    except BaseException:
        with contextlib.suppress(Exception):
            if not tmp.closed:
                tmp.close()
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json_0600(target: Path, payload: Mapping[str, Any]) -> None:
    """Atomically serialize JSON to `target` with 0600 file permissions."""
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text_0600(target, serialized)


def safe_read_json(target: Path) -> dict[str, Any]:
    """Read a regular non-symlink JSON object using O_NOFOLLOW."""
    if target.is_symlink():
        raise pdf_utils.PathContainmentError(f"Refusing symlinked JSON file: {target}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"Not a regular file: {target}")
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise PermissionError(f"File {target} is not owned by current user")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            data = json.loads(handle.read())
    finally:
        if fd != -1:
            os.close(fd)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {target}")
    return data


def resolve_jobs_dir(ctx: Any, output_root: Path | None = None) -> Path:
    """Determine isolated jobs storage directory without ever writing into plugin source dir."""
    data_dir = Path(ctx.data_dir).expanduser().resolve()
    plugin_dir = Path(ctx.plugin_dir).expanduser().resolve()
    if data_dir != plugin_dir:
        return data_dir / "jobs"
    cfg_store = getattr(ctx, "_config_store", None)
    cfg_root = getattr(cfg_store, "_root_dir", None)
    if cfg_root is not None:
        return Path(cfg_root).expanduser().resolve() / "plugin-data" / ctx.plugin_id / "jobs"
    if output_root is not None:
        return output_root.resolve() / ".omnidesk-jobs"
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data).expanduser() if xdg_data else (Path.home() / ".local" / "share")
    return base / "omnidesk" / "plugin-data" / ctx.plugin_id / "jobs"


@contextlib.contextmanager
def _advisory_file_lock(lock_path: Path) -> Iterator[None]:
    ensure_private_dir(lock_path.parent)
    if lock_path.is_symlink():
        raise pdf_utils.PathContainmentError(f"Refusing symlinked lock file: {lock_path}")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class JobStore:
    """Thread/process-safe 0600 atomic job store with operation_id deduplication lock."""

    def __init__(self, jobs_dir: Path) -> None:
        self.jobs_dir = jobs_dir
        self._ops_dir = jobs_dir / "ops"
        self._locks_dir = jobs_dir / "locks"
        self.inputs_dir = jobs_dir / "inputs"
        self._async_lock = asyncio.Lock()
        self._thread_lock = threading.RLock()

    def job_path(self, job_id: str) -> Path:
        safe_id = pdf_utils.validate_safe_identifier(job_id, "job_id")
        return self.jobs_dir / f"{safe_id}.json"

    def _op_marker_path(self, operation_id: str) -> Path:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return self._ops_dir / f"{digest}.json"

    def _op_lock_path(self, operation_id: str) -> Path:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return self._locks_dir / f"op_{digest}.lock"

    def _job_lock_path(self, job_id: str) -> Path:
        safe_id = pdf_utils.validate_safe_identifier(job_id, "job_id")
        return self._locks_dir / f"job_{safe_id}.lock"

    async def reserve_and_create(
        self,
        record: Mapping[str, Any],
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create a job and enforce single-flight operation_id uniqueness via file lock."""
        job_id = pdf_utils.validate_safe_identifier(record.get("job_id"), "job_id")
        payload = dict(record)
        async with self._async_lock:
            def _do_reserve() -> dict[str, Any]:
                with self._thread_lock:
                    ensure_private_dir(self.jobs_dir)
                    if operation_id:
                        op_id = pdf_utils.validate_safe_identifier(operation_id, "operation_id")
                        lock_path = self._op_lock_path(op_id)
                        marker_path = self._op_marker_path(op_id)
                        with _advisory_file_lock(lock_path):
                            if marker_path.exists() or marker_path.is_symlink():
                                marker = safe_read_json(marker_path)
                                existing_id = str(marker.get("job_id") or "")
                                existing = self.read_job(existing_id) if existing_id else None
                                raise DuplicateOperationError(op_id, existing or marker)
                            atomic_write_json_0600(self.job_path(job_id), payload)
                            atomic_write_json_0600(
                                marker_path,
                                {
                                    "operation_id": op_id,
                                    "job_id": job_id,
                                    "created_at": payload.get("created_at", time.time()),
                                },
                            )
                            return payload
                    atomic_write_json_0600(self.job_path(job_id), payload)
                    return payload

            return await asyncio.to_thread(_do_reserve)

    def update_job(self, job_id: str, **patch: Any) -> dict[str, Any] | None:
        """Read-modify-write a job file atomically."""
        safe_id = pdf_utils.validate_safe_identifier(job_id, "job_id")
        with self._thread_lock, _advisory_file_lock(self._job_lock_path(safe_id)):
            path = self.job_path(safe_id)
            if not path.is_file() or path.is_symlink():
                return None
            current = safe_read_json(path)
            current.update(patch)
            current["updated_at"] = time.time()
            atomic_write_json_0600(path, current)
            return current

    def read_job(self, job_id: str) -> dict[str, Any] | None:
        """Read a single 0600 job file if present."""
        safe_id = pdf_utils.validate_safe_identifier(job_id, "job_id")
        path = self.job_path(safe_id)
        if path.is_symlink():
            raise pdf_utils.PathContainmentError(f"Refusing symlinked job file: {path}")
        if not path.is_file():
            return None
        return safe_read_json(path)

    def find_by_operation_id(self, operation_id: str) -> dict[str, Any] | None:
        op_id = pdf_utils.validate_safe_identifier(operation_id, "operation_id")
        marker = self._op_marker_path(op_id)
        if not marker.is_file() or marker.is_symlink():
            return None
        info = safe_read_json(marker)
        job_id = str(info.get("job_id") or "")
        return self.read_job(job_id) if job_id else None

    def list_jobs(self, *, status: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        """List persisted jobs sorted newest-first, optionally filtered by status."""
        if not self.jobs_dir.is_dir() or self.jobs_dir.is_symlink():
            return []
        clean_status = status.strip().lower()
        if clean_status and clean_status not in JOB_STATES:
            raise ValueError(f"Unsupported job status filter: {status!r}")
        records: list[dict[str, Any]] = []
        for entry in self.jobs_dir.iterdir():
            if entry.is_symlink() or not entry.is_file() or entry.suffix != ".json" or entry.name.startswith("."):
                continue
            try:
                rec = safe_read_json(entry)
            except (OSError, ValueError, pdf_utils.PathContainmentError):
                continue
            if clean_status and str(rec.get("status", "")).lower() != clean_status:
                continue
            records.append(rec)
        records.sort(key=lambda r: float(r.get("created_at") or 0.0), reverse=True)
        if limit is not None and limit >= 0:
            return records[:limit]
        return records

    def find_latest_succeeded_for_book(self, book_id: str, source_path: Path | None = None) -> dict[str, Any] | None:
        """Return newest succeeded job matching `book_id` or `source_path`."""
        resolved_src = str(source_path.resolve()) if source_path is not None else ""
        for rec in self.list_jobs(status="succeeded"):
            if rec.get("book_id") == book_id or (resolved_src and rec.get("source_path") == resolved_src):
                return rec
        return None
