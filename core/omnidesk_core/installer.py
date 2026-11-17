"""Transactional plugin installer, upstream lockfile, snapshot backup, and rollback manager."""

from __future__ import annotations

import asyncio
import concurrent.futures
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import atomic_write_json
from .errors import (
    ErrorCode,
    InvalidArgumentError,
    OmniDeskError,
    PathSafetyError,
    PluginFaultError,
    PluginManifestError,
    PluginNotFoundError,
    ValidationError,
    redact_secrets,
)
from .manifest import PluginManifest, load_manifest
from .paths import (
    OmnideskPaths,
    ensure_safe_dir,
    safe_child_path,
    validate_plugin_id,
    validate_safe_tree,
    validate_snapshot_id,
)
from .plugins import PluginManager, PluginVerifyResult, _run_verify_cmd
from .vault import SecretVault

MAX_SNAPSHOTS = 5
DEFAULT_LOCK_TIMEOUT = 30.0
_IGNORE_NAMES = frozenset({"__pycache__", ".pytest_cache", ".git"})
_GIT_SCHEMES = ("http://", "https://", "ssh://", "git://", "file://")
_DEFAULT_VERIFY_CMDS = frozenset({"", "uv run pytest -q", "pytest -q", "pytest"})
_URL_CREDS_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]+)@")


def _scrub_url_creds(text: str) -> str:
    return _URL_CREDS_RE.sub(r"\1[REDACTED]@", text)


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    """Metadata describing one retained rollback snapshot."""

    snapshot_id: str
    plugin_id: str
    version: str
    created_at: str
    content_hash: str
    path: Path
    lock_entry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "plugin_id": self.plugin_id,
            "version": self.version,
            "created_at": self.created_at,
            "content_hash": self.content_hash,
            "path": str(self.path),
            "lock_entry": dict(self.lock_entry),
        }


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Result payload returned on successful install, update, or rollback."""

    ok: bool
    action: str
    plugin_id: str
    version: str
    plugin_dir: Path
    source_type: str
    url: str
    ref: str
    commit: str
    content_hash: str
    snapshot_id: str = ""
    snapshots: list[str] = field(default_factory=list)
    verify: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "plugin_id": self.plugin_id,
            "version": self.version,
            "plugin_dir": str(self.plugin_dir),
            "source_type": self.source_type,
            "url": self.url,
            "ref": self.ref,
            "commit": self.commit,
            "content_hash": self.content_hash,
            "snapshot_id": self.snapshot_id,
            "snapshots": list(self.snapshots),
            "verify": dict(self.verify),
        }


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _run_coro(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def compute_tree_hash(plugin_dir: Path) -> str:
    """Compute a deterministic SHA-256 hash over a verified non-symlink plugin tree."""
    root = validate_safe_tree(plugin_dir, ignore_names=_IGNORE_NAMES)
    digest = hashlib.sha256()
    entries: list[tuple[str, bool, Path]] = []

    def _collect(cur: Path) -> None:
        with os.scandir(cur) as it:
            for entry in sorted(it, key=lambda e: e.name):
                if entry.name in _IGNORE_NAMES:
                    continue
                p = Path(entry.path)
                rel = p.relative_to(root).as_posix()
                is_dir = entry.is_dir(follow_symlinks=False)
                entries.append((rel, is_dir, p))
                if is_dir:
                    _collect(p)

    _collect(root)
    for rel, is_dir, p in sorted(entries, key=lambda item: item[0]):
        if is_dir:
            digest.update(b"D\x00" + rel.encode("utf-8") + b"\x00")
            continue
        size = p.stat().st_size
        digest.update(b"F\x00" + rel.encode("utf-8") + b"\x00" + str(size).encode("ascii") + b"\x00")
        with p.open("rb") as fh:
            while chunk := fh.read(65536):
                digest.update(chunk)
    return digest.hexdigest()


def _strip_caches(root: Path) -> None:
    for name in ("__pycache__", ".pytest_cache"):
        for candidate in root.rglob(name):
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate, ignore_errors=True)


def _copy_clean_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(*_IGNORE_NAMES))
    validate_safe_tree(dst)


def _move_or_copy_clean(src: Path, dst: Path) -> None:
    validate_safe_tree(src)
    try:
        os.rename(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_clean_tree(src, dst)


def _read_json_obj(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise PathSafetyError(f"Refusing symlinked metadata file: {path}")
    if not path.exists():
        return {}
    if not path.is_file():
        raise PathSafetyError(f"Metadata path is not a regular file: {path}")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Corrupted JSON metadata at {path}: {exc}", cause=exc) from exc
    if not isinstance(parsed, dict):
        raise ValidationError(f"Metadata file {path} must contain a JSON object")
    return parsed


@contextmanager
def _acquire_file_lock(lock_path: Path, timeout: float) -> Iterator[None]:
    if lock_path.is_symlink():
        raise PathSafetyError(f"Refusing symlinked lock file: {lock_path}")
    ensure_safe_dir(lock_path.parent)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathSafetyError(f"Refusing symlinked lock file: {lock_path}") from exc
        raise OmniDeskError(f"Failed opening installer lock {lock_path}: {exc}", code=ErrorCode.PERMISSION_DENIED) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PathSafetyError(f"Lock path is not a regular file: {lock_path}")
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise OmniDeskError(f"Lock acquisition failed on {lock_path}: {exc}") from exc
                now = time.monotonic()
                if now >= deadline:
                    raise OmniDeskError(
                        f"Installer lock busy ({lock_path}); another operation is in progress",
                        code=ErrorCode.CONFLICT,
                        details={"lock_path": str(lock_path)},
                    )
                time.sleep(min(0.02, max(0.002, deadline - now)))
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _run_git(args: list[str], *, cwd: Path | None = None, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "http:https:ssh:git:file",
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _classify_source(source: str | Path, ref: str | None) -> tuple[str, str]:
    raw = str(source).strip()
    if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise InvalidArgumentError("Plugin source must be a valid non-empty path or git URL")
    if raw.startswith("-"):
        raise InvalidArgumentError(f"Unsafe plugin source starting with '-': {raw!r}")
    if raw.lower().startswith("ext::"):
        raise PathSafetyError("Unsupported git remote helper protocol 'ext::'")
    if raw.startswith(_GIT_SCHEMES) or raw.startswith("git@"):
        return "git", raw

    expanded = Path(raw).expanduser()
    if expanded.is_symlink():
        raise PathSafetyError(f"Refusing symlinked plugin source: {expanded}")
    if expanded.exists():
        if not expanded.is_dir():
            raise InvalidArgumentError(f"Plugin source path is not a directory: {expanded}")
        is_bare_git = (expanded / "HEAD").is_file() and (expanded / "objects").is_dir() and not (expanded / "plugin.json").exists()
        if is_bare_git:
            return "git", str(expanded.resolve())
        if ref is not None:
            if (expanded / ".git").exists():
                return "git", str(expanded.resolve())
            raise InvalidArgumentError("--ref is only supported for git repository sources")
        return "path", str(expanded.resolve())

    if raw.endswith(".git") or raw.startswith("github.com/"):
        url = f"https://{raw}" if raw.startswith("github.com/") else raw
        return "git", url
    raise PluginNotFoundError(f"Plugin source path does not exist: {raw}")


def _validate_git_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    cleaned = ref.strip()
    if not cleaned or cleaned.startswith("-") or "\x00" in cleaned or any(ch.isspace() for ch in cleaned):
        raise InvalidArgumentError(f"Invalid git ref {ref!r}")
    return cleaned


def _stage_source(source_type: str, target: str, stage_dir: Path, ref: str | None) -> tuple[Path, str, str]:
    pkg_dir = stage_dir / "package"
    clean_ref = _validate_git_ref(ref)
    if source_type == "path":
        src = Path(target)
        if src.is_symlink():
            raise PathSafetyError(f"Refusing symlinked plugin source: {src}")
        validate_safe_tree(src, ignore_names=_IGNORE_NAMES)
        _copy_clean_tree(src, pkg_dir)
        commit, resolved_ref = "", clean_ref or ""
        if (src / ".git").exists() and not (src / ".git").is_symlink():
            res = _run_git(["-C", str(src), "rev-parse", "HEAD"], timeout=10.0)
            if res.returncode == 0:
                commit = res.stdout.strip()
            br = _run_git(["-C", str(src), "rev-parse", "--abbrev-ref", "HEAD"], timeout=10.0)
            if br.returncode == 0 and br.stdout.strip():
                resolved_ref = resolved_ref or br.stdout.strip()
        return pkg_dir, resolved_ref, commit

    clone_args = ["clone", "-c", "submodule.recurse=false", "--depth", "1"]
    if clean_ref:
        clone_args.extend(["--branch", clean_ref])
    clone_args.extend(["--", target, str(pkg_dir)])
    res = _run_git(clone_args)
    if res.returncode != 0 and clean_ref:
        shutil.rmtree(pkg_dir, ignore_errors=True)
        res = _run_git(["clone", "-c", "submodule.recurse=false", "--", target, str(pkg_dir)])
        if res.returncode == 0:
            chk = _run_git(["-C", str(pkg_dir), "checkout", "--detach", clean_ref])
            if chk.returncode != 0:
                err = _scrub_url_creds((chk.stderr or chk.stdout or "").strip())
                raise OmniDeskError(f"git checkout {clean_ref!r} failed: {err[-400:]}", code=ErrorCode.NOT_FOUND)
    if res.returncode != 0:
        err = _scrub_url_creds((res.stderr or res.stdout or "").strip())
        low = err.lower()
        code = ErrorCode.NOT_FOUND if any(k in low for k in ("not found", "does not exist", "could not find", "unknown revision")) else ErrorCode.VALIDATION_ERROR
        raise OmniDeskError(f"git clone failed for {_scrub_url_creds(target)}: {err[-400:]}", code=code)

    sha_res = _run_git(["-C", str(pkg_dir), "rev-parse", "HEAD"], timeout=10.0)
    commit = sha_res.stdout.strip() if sha_res.returncode == 0 else ""
    resolved_ref = clean_ref or ""
    if not resolved_ref:
        br_res = _run_git(["-C", str(pkg_dir), "rev-parse", "--abbrev-ref", "HEAD"], timeout=10.0)
        if br_res.returncode == 0 and br_res.stdout.strip():
            resolved_ref = br_res.stdout.strip()
    git_dir = pkg_dir / ".git"
    if git_dir.is_symlink():
        raise PathSafetyError("Symlinked .git directory rejected in cloned plugin")
    if git_dir.exists():
        shutil.rmtree(git_dir)
    validate_safe_tree(pkg_dir)
    return pkg_dir, resolved_ref, commit


def _verify_entry_exists(manifest: PluginManifest, pkg_dir: Path) -> None:
    parts = manifest.entry_module.split(".")
    if ".." in parts or any(not p for p in parts):
        raise PathSafetyError(f"Unsafe entrypoint module {manifest.entry_module!r}")
    candidates = (
        safe_child_path(pkg_dir, *parts[:-1], f"{parts[-1]}.py"),
        safe_child_path(pkg_dir, *parts, "__init__.py"),
    )
    for cand in candidates:
        if cand.is_symlink():
            raise PathSafetyError(f"Refusing symlinked entrypoint: {cand}")
        if cand.is_file():
            return
    raise PluginManifestError(
        f"Entrypoint module {manifest.entry_module!r} not found in {pkg_dir}",
        plugin_id=manifest.id,
        code=ErrorCode.VALIDATION_ERROR,
    )


class PluginInstaller:
    """Manages safe staged install, update, 5-snapshot rollback, and upstreams.lock.json."""

    def __init__(
        self,
        paths: OmnideskPaths | None = None,
        *,
        plugins_dir: Path | str | None = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self.paths = paths or OmnideskPaths.from_env()
        self.plugins_dir = Path(plugins_dir).expanduser() if plugins_dir is not None else self.paths.plugins_dir
        self.lock_timeout = float(lock_timeout)
        self._vault = SecretVault(paths=self.paths)

    @property
    def lock_path(self) -> Path:
        return self.paths.install_lock_path

    def _redact(self, text: str) -> str:
        cleaned = _scrub_url_creds(text)
        try:
            return self._vault.redact(cleaned)
        except Exception:
            return redact_secrets(cleaned)

    def _save_diagnostic(self, action: str, plugin_id: str, stage: str, error: str, extra: Mapping[str, Any] | None = None) -> Path:
        ensure_safe_dir(self.paths.state_dir)
        ensure_safe_dir(self.paths.diagnostics_dir)
        name = f"{action}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}.json"
        diag_path = self.paths.diagnostics_dir / name
        payload: dict[str, Any] = {
            "ok": False,
            "action": action,
            "plugin_id": plugin_id,
            "stage": stage,
            "error": self._redact(error),
            "timestamp": _utc_iso(),
        }
        if extra:
            payload["details"] = json.loads(self._redact(json.dumps(dict(extra), ensure_ascii=False, default=str)))
        atomic_write_json(diag_path, payload, file_mode=0o600)
        return diag_path

    def _fail(self, exc: OmniDeskError, *, action: str, plugin_id: str, stage: str, extra: Mapping[str, Any] | None = None) -> OmniDeskError:
        safe_msg = self._redact(exc.message)
        diag_extra = {**exc.details, **dict(extra or {})}
        diag_path_str = ""
        try:
            diag_path_str = str(self._save_diagnostic(action, plugin_id, stage, safe_msg, diag_extra))
        except Exception:
            pass
        exc.message = safe_msg
        exc.args = (safe_msg,)
        exc.details = {**diag_extra, "stage": stage, **({"diagnostic_path": diag_path_str} if diag_path_str else {})}
        if plugin_id:
            exc.details.setdefault("plugin_id", plugin_id)
        return exc

    def read_upstreams_lock(self) -> dict[str, Any]:
        raw = _read_json_obj(self.paths.upstreams_lock_path)
        locks = raw.get("locks")
        if locks is None:
            return {"schema_version": 2, "locks": {}}
        if not isinstance(locks, dict):
            raise ValidationError("upstreams.lock.json 'locks' must be a JSON object")
        return {"schema_version": int(raw.get("schema_version", 2)), "locks": dict(locks)}

    def _write_upstreams_lock(self, data: Mapping[str, Any]) -> None:
        if self.paths.upstreams_lock_path.is_symlink():
            raise PathSafetyError(f"Refusing symlinked lockfile: {self.paths.upstreams_lock_path}")
        ensure_safe_dir(self.paths.data_dir)
        atomic_write_json(self.paths.upstreams_lock_path, data, file_mode=0o600)

    def _check_duplicate_id(self, plugin_id: str) -> None:
        direct = self.plugins_dir / plugin_id
        if direct.is_symlink():
            raise PathSafetyError(f"Refusing symlinked plugin directory: {direct}")
        if direct.exists():
            raise OmniDeskError(
                f"Plugin {plugin_id!r} is already installed at {direct}",
                code=ErrorCode.CONFLICT,
                details={"plugin_id": plugin_id, "existing_dir": str(direct)},
            )
        if not self.plugins_dir.is_dir():
            return
        for entry in sorted(self.plugins_dir.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                continue
            mf = entry / "plugin.json"
            if mf.is_file() and not mf.is_symlink():
                try:
                    existing_id = json.loads(mf.read_text(encoding="utf-8")).get("id")
                except (OSError, json.JSONDecodeError):
                    continue
                if existing_id == plugin_id:
                    raise OmniDeskError(
                        f"Duplicate plugin ID {plugin_id!r} already exists in {entry}",
                        code=ErrorCode.CONFLICT,
                        details={"plugin_id": plugin_id, "existing_dir": str(entry)},
                    )

    def _validate_manifest_staged(
        self,
        pkg_dir: Path,
        *,
        expected_id: str | None,
    ) -> PluginManifest:
        validate_safe_tree(pkg_dir)
        manifest = load_manifest(pkg_dir)
        if expected_id is not None and manifest.id != expected_id:
            raise PluginManifestError(
                f"Plugin ID mismatch: expected {expected_id!r}, got {manifest.id!r}",
                plugin_id=manifest.id,
                code=ErrorCode.VALIDATION_ERROR,
            )
        _verify_entry_exists(manifest, pkg_dir)
        return manifest

    def _verify_staged(
        self,
        pkg_dir: Path,
        *,
        manifest: PluginManifest | None = None,
        expected_id: str | None = None,
        run_tests: bool = True,
        require_tests: bool = False,
    ) -> tuple[PluginManifest, PluginVerifyResult]:
        if manifest is None:
            manifest = self._validate_manifest_staged(pkg_dir, expected_id=expected_id)
        mgr = PluginManager(plugins_dir=pkg_dir.parent, paths=self.paths)
        res: PluginVerifyResult = _run_coro(mgr.verify_plugin(pkg_dir, run_tests=run_tests, require_tests=require_tests))
        if res.ok and run_tests and res.tests_skipped and manifest.verify_cmd.strip() not in _DEFAULT_VERIFY_CMDS:
            ok_cmd, err_cmd = _run_coro(_run_verify_cmd(pkg_dir, manifest.verify_cmd, xdg_root=pkg_dir.parent / "test-xdg"))
            if ok_cmd:
                res = PluginVerifyResult(
                    True, res.plugin_id, res.plugin_dir, res.manifest_ok, res.lifecycle_ok, True, False, res.health, ()
                )
            else:
                res = PluginVerifyResult(
                    False, res.plugin_id, res.plugin_dir, res.manifest_ok, res.lifecycle_ok, False, False, res.health, (self._redact(err_cmd),)
                )
        _strip_caches(pkg_dir)
        if not res.ok:
            msg = "; ".join(res.errors) or f"Verification failed for {manifest.id}"
            if not res.manifest_ok:
                raise PluginManifestError(msg, plugin_id=manifest.id, code=ErrorCode.VALIDATION_ERROR, details={"verify": res.to_dict()})
            if not res.lifecycle_ok:
                raise PluginFaultError(msg, plugin_id=manifest.id, code=ErrorCode.PLUGIN_FAULT, details={"verify": res.to_dict()})
            raise ValidationError(msg, code=ErrorCode.VALIDATION_ERROR, details={"verify": res.to_dict()})
        return manifest, res

    def _snapshots_meta_path(self, plugin_id: str) -> Path:
        return self.paths.plugin_backup_dir(plugin_id) / "snapshots.json"

    def _read_snapshots_index(self, plugin_id: str) -> list[dict[str, Any]]:
        bdir = self.paths.plugin_backup_dir(plugin_id)
        if bdir.is_symlink():
            raise PathSafetyError(f"Refusing symlinked backup directory: {bdir}")
        meta_path = self._snapshots_meta_path(plugin_id)
        if meta_path.exists():
            raw = _read_json_obj(meta_path)
            items = raw.get("snapshots", [])
            if isinstance(items, list):
                return [dict(item) for item in items if isinstance(item, dict) and isinstance(item.get("snapshot_id"), str)]
        if not bdir.is_dir():
            return []
        fallback: list[dict[str, Any]] = []
        for entry in sorted(bdir.iterdir(), key=lambda p: p.stat().st_mtime if not p.is_symlink() else 0.0):
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                continue
            fallback.append({"snapshot_id": validate_snapshot_id(entry.name), "plugin_id": plugin_id, "version": "", "created_at": "", "content_hash": "", "lock_entry": {}})
        return fallback

    def _write_snapshots_index(self, plugin_id: str, items: list[dict[str, Any]]) -> None:
        bdir = self.paths.plugin_backup_dir(plugin_id)
        ensure_safe_dir(bdir)
        meta_path = self._snapshots_meta_path(plugin_id)
        if meta_path.is_symlink():
            raise PathSafetyError(f"Refusing symlinked snapshots index: {meta_path}")
        atomic_write_json(meta_path, {"schema_version": 1, "snapshots": items}, file_mode=0o600)

    def list_snapshots(self, plugin_id: str) -> list[SnapshotInfo]:
        pid = validate_plugin_id(plugin_id)
        bdir = self.paths.plugin_backup_dir(pid)
        out: list[SnapshotInfo] = []
        for item in self._read_snapshots_index(pid):
            sid = item.get("snapshot_id", "")
            try:
                sdir = safe_child_path(bdir, validate_snapshot_id(sid))
            except PathSafetyError:
                continue
            if sdir.is_dir() and not sdir.is_symlink():
                out.append(SnapshotInfo(sid, pid, str(item.get("version", "")), str(item.get("created_at", "")), str(item.get("content_hash", "")), sdir, dict(item.get("lock_entry") or {})))
        return out

    def _ensure_install_dirs(self) -> None:
        if self.plugins_dir == self.paths.plugins_dir:
            ensure_safe_dir(self.paths.data_dir)
        ensure_safe_dir(self.plugins_dir)
        ensure_safe_dir(self.paths.staging_dir)

    def _ensure_backup_dirs(self, plugin_id: str) -> Path:
        ensure_safe_dir(self.paths.data_dir)
        ensure_safe_dir(self.paths.backups_dir)
        return ensure_safe_dir(self.paths.plugin_backup_dir(plugin_id))

    def _make_snapshot(self, plugin_id: str, final_dir: Path, old_lock: Mapping[str, Any]) -> dict[str, Any]:
        validate_safe_tree(final_dir, ignore_names=_IGNORE_NAMES)
        old_version = ""
        try:
            old_version = load_manifest(final_dir).version
        except OmniDeskError:
            pass
        old_hash = compute_tree_hash(final_dir)
        bdir = self._ensure_backup_dirs(plugin_id)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        ver_tag = old_version or "0.0.0"
        snap_id = validate_snapshot_id(f"{stamp}-{ver_tag}-{uuid.uuid4().hex[:6]}")
        snap_tmp = bdir / f".snap-{snap_id}"
        snap_dir = safe_child_path(bdir, snap_id)
        try:
            _copy_clean_tree(final_dir, snap_tmp)
            os.rename(snap_tmp, snap_dir)
        except BaseException:
            shutil.rmtree(snap_tmp, ignore_errors=True)
            raise
        return {
            "snapshot_id": snap_id,
            "plugin_id": plugin_id,
            "version": old_version,
            "created_at": _utc_iso(),
            "content_hash": old_hash,
            "lock_entry": dict(old_lock),
        }

    def _prune_snapshots(self, plugin_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bdir = self.paths.plugin_backup_dir(plugin_id)
        kept = list(items)
        while len(kept) > MAX_SNAPSHOTS:
            dropped = kept.pop(0)
            sid = str(dropped.get("snapshot_id", ""))
            if sid:
                try:
                    target = safe_child_path(bdir, validate_snapshot_id(sid))
                except PathSafetyError:
                    continue
                if target.exists() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
        self._write_snapshots_index(plugin_id, kept)
        return kept

    def _swap_dir_and_lock(
        self,
        *,
        plugin_id: str,
        final_dir: Path,
        same_fs_new: Path,
        new_lock_entry: dict[str, Any],
    ) -> None:
        old_tmp = self.plugins_dir / f".old-{plugin_id}-{uuid.uuid4().hex[:8]}"
        moved_old = False
        moved_new = False
        try:
            if final_dir.is_symlink():
                raise PathSafetyError(f"Refusing symlinked active plugin directory: {final_dir}")
            if final_dir.exists():
                os.rename(final_dir, old_tmp)
                moved_old = True
            os.rename(same_fs_new, final_dir)
            moved_new = True
            lock_data = self.read_upstreams_lock()
            lock_data["locks"][plugin_id] = new_lock_entry
            self._write_upstreams_lock(lock_data)
        except BaseException:
            if moved_new and final_dir.exists():
                shutil.rmtree(final_dir, ignore_errors=True)
            if moved_old and old_tmp.exists() and not final_dir.exists():
                os.rename(old_tmp, final_dir)
            shutil.rmtree(same_fs_new, ignore_errors=True)
            raise
        if moved_old and old_tmp.exists():
            shutil.rmtree(old_tmp, ignore_errors=True)

    def _install_python_dependencies(self, manifest: PluginManifest) -> dict[str, Any]:
        """Provision an isolated venv for a plugin's declared ``python_dependencies``."""
        packages = [p.strip() for p in manifest.python_dependencies if isinstance(p, str) and p.strip()]
        env_dir = self.paths.plugin_data_dir(manifest.id) / "python-env"
        info: dict[str, Any] = {
            "enabled": False,
            "packages": packages,
            "env": str(env_dir),
            "site_packages": "",
        }
        if not packages:
            return info
        ensure_safe_dir(env_dir)
        python_bin = env_dir / "bin" / "python"
        uv = shutil.which("uv")
        try:
            if uv:
                subprocess.run(
                    [uv, "venv", "--allow-existing", str(env_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                subprocess.run(
                    [uv, "pip", "install", "--python", str(python_bin), *packages],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=900,
                )
            else:
                subprocess.run(
                    [sys.executable, "-m", "venv", str(env_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                subprocess.run(
                    [str(python_bin), "-m", "pip", "install", *packages],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=900,
                )
            site = subprocess.run(
                [str(python_bin), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise OmniDeskError(
                f"Failed to install python_dependencies for {manifest.id}: {redact_secrets(str(detail))[-800:]}",
                code=ErrorCode.NOT_CONFIGURED,
            ) from exc
        info.update({"enabled": True, "site_packages": site})
        atomic_write_json(env_dir / "env.json", info)
        return info

    def install(
        self,
        source: str | Path,
        *,
        plugin_id: str | None = None,
        ref: str | None = None,
        run_tests: bool = True,
        require_tests: bool = False,
        install_python_deps: bool = True,
        lock_timeout: float | None = None,
    ) -> InstallResult:
        expected_id = validate_plugin_id(plugin_id) if plugin_id is not None else None
        timeout = self.lock_timeout if lock_timeout is None else float(lock_timeout)
        stage_job: Path | None = None
        stage = "init"
        resolved_pid = expected_id or ""
        try:
            with _acquire_file_lock(self.lock_path, timeout):
                self._ensure_install_dirs()
                if expected_id is not None:
                    self._check_duplicate_id(expected_id)
                stage = "fetch"
                source_type, target = _classify_source(source, ref)
                stage_job = self.paths.staging_dir / f"install-{uuid.uuid4().hex[:10]}"
                stage_job.mkdir(parents=True, exist_ok=False)
                pkg_dir, resolved_ref, commit = _stage_source(source_type, target, stage_job, ref)
                stage = "manifest"
                manifest = self._validate_manifest_staged(pkg_dir, expected_id=expected_id)
                resolved_pid = manifest.id
                stage = "conflict_check"
                self._check_duplicate_id(manifest.id)
                stage = "verify"
                manifest, verify_res = self._verify_staged(
                    pkg_dir, manifest=manifest, expected_id=expected_id, run_tests=run_tests, require_tests=require_tests
                )
                stage = "dependencies"
                env_info = (
                    self._install_python_dependencies(manifest)
                    if install_python_deps
                    else {"enabled": False, "packages": list(manifest.python_dependencies)}
                )
                stage = "activate"
                content_hash = compute_tree_hash(pkg_dir)
                same_fs = self.plugins_dir / f".staging-{manifest.id}-{uuid.uuid4().hex[:8]}"
                final_dir = self.plugins_dir / manifest.id
                try:
                    _move_or_copy_clean(pkg_dir, same_fs)
                    lock_entry = {
                        "plugin_id": manifest.id,
                        "version": manifest.version,
                        "source_type": source_type,
                        "url": _scrub_url_creds(target),
                        "ref": resolved_ref,
                        "commit": commit,
                        "content_hash": content_hash,
                        "python_dependencies": list(manifest.python_dependencies),
                        "python_env": env_info.get("env", ""),
                        "locked_at": _utc_iso(),
                    }
                    self._swap_dir_and_lock(
                        plugin_id=manifest.id, final_dir=final_dir, same_fs_new=same_fs, new_lock_entry=lock_entry
                    )
                except BaseException:
                    shutil.rmtree(same_fs, ignore_errors=True)
                    raise
                return InstallResult(
                    True, "install", manifest.id, manifest.version, final_dir, source_type, _scrub_url_creds(target), resolved_ref, commit, content_hash,
                    snapshots=[s.snapshot_id for s in self.list_snapshots(manifest.id)], verify=verify_res.to_dict(),
                )
        except OmniDeskError as exc:
            raise self._fail(exc, action="install", plugin_id=resolved_pid, stage=stage, extra={"source": _scrub_url_creds(str(source))}) from exc
        except OSError as exc:
            raise self._fail(OmniDeskError(f"Filesystem error during install: {exc}"), action="install", plugin_id=resolved_pid, stage=stage, extra={"source": _scrub_url_creds(str(source))}) from exc
        finally:
            if stage_job is not None:
                shutil.rmtree(stage_job, ignore_errors=True)

    def update(
        self,
        plugin_id: str,
        *,
        ref: str | None = None,
        run_tests: bool = True,
        require_tests: bool = False,
        install_python_deps: bool = True,
        lock_timeout: float | None = None,
    ) -> InstallResult:
        pid = validate_plugin_id(plugin_id)
        timeout = self.lock_timeout if lock_timeout is None else float(lock_timeout)
        stage_job: Path | None = None
        stage = "init"
        try:
            with _acquire_file_lock(self.lock_path, timeout):
                self._ensure_install_dirs()
                final_dir = self.plugins_dir / pid
                if final_dir.is_symlink():
                    raise PathSafetyError(f"Refusing symlinked plugin directory: {final_dir}")
                if not final_dir.is_dir():
                    raise PluginNotFoundError(f"Plugin {pid!r} is not installed in {self.plugins_dir}", plugin_id=pid)

                locks = self.read_upstreams_lock().get("locks", {})
                old_lock = dict(locks.get(pid) or {})
                url = str(old_lock.get("url") or "")
                if not url:
                    cur_mf = load_manifest(final_dir)
                    if cur_mf.repository is not None:
                        url = cur_mf.repository.url
                        ref = ref or cur_mf.repository.branch
                if not url:
                    raise OmniDeskError(f"No upstream recorded in upstreams.lock.json for {pid!r}", code=ErrorCode.NOT_CONFIGURED)
                eff_ref = ref if ref is not None else (str(old_lock["ref"]) if old_lock.get("ref") else None)
                recorded_type = str(old_lock.get("source_type") or "")
                if recorded_type == "path" and ref is None:
                    eff_ref = None
                    source_type, target = _classify_source(url, None)
                else:
                    source_type, target = _classify_source(url, eff_ref)

                stage = "fetch"
                stage_job = self.paths.staging_dir / f"update-{uuid.uuid4().hex[:10]}"
                stage_job.mkdir(parents=True, exist_ok=False)
                pkg_dir, resolved_ref, commit = _stage_source(source_type, target, stage_job, eff_ref)
                stage = "verify"
                manifest, verify_res = self._verify_staged(
                    pkg_dir, expected_id=pid, run_tests=run_tests, require_tests=require_tests
                )
                stage = "dependencies"
                env_info = (
                    self._install_python_dependencies(manifest)
                    if install_python_deps
                    else {"enabled": False, "packages": list(manifest.python_dependencies)}
                )
                stage = "snapshot"
                content_hash = compute_tree_hash(pkg_dir)
                existing_snaps = self._read_snapshots_index(pid)
                snap_entry = self._make_snapshot(pid, final_dir, old_lock)
                snap_dir = safe_child_path(self.paths.plugin_backup_dir(pid), snap_entry["snapshot_id"])
                if self._snapshots_meta_path(pid).is_symlink():
                    raise PathSafetyError(f"Refusing symlinked snapshots index: {self._snapshots_meta_path(pid)}")
                same_fs = self.plugins_dir / f".updating-{pid}-{uuid.uuid4().hex[:8]}"
                stage = "activate"
                try:
                    _move_or_copy_clean(pkg_dir, same_fs)
                    new_lock = {
                        "plugin_id": pid,
                        "version": manifest.version,
                        "source_type": source_type,
                        "url": _scrub_url_creds(target),
                        "ref": resolved_ref,
                        "commit": commit,
                        "content_hash": content_hash,
                        "python_dependencies": list(manifest.python_dependencies),
                        "python_env": env_info.get("env", ""),
                        "locked_at": _utc_iso(),
                    }
                    self._swap_dir_and_lock(
                        plugin_id=pid, final_dir=final_dir, same_fs_new=same_fs, new_lock_entry=new_lock
                    )
                    kept = self._prune_snapshots(pid, [*existing_snaps, snap_entry])
                except BaseException:
                    shutil.rmtree(same_fs, ignore_errors=True)
                    shutil.rmtree(snap_dir, ignore_errors=True)
                    raise
                return InstallResult(
                    True, "update", pid, manifest.version, final_dir, source_type, _scrub_url_creds(target), resolved_ref, commit, content_hash,
                    snapshot_id=snap_entry["snapshot_id"], snapshots=[s["snapshot_id"] for s in kept], verify=verify_res.to_dict(),
                )
        except OmniDeskError as exc:
            raise self._fail(exc, action="update", plugin_id=pid, stage=stage) from exc
        except OSError as exc:
            raise self._fail(OmniDeskError(f"Filesystem error during update: {exc}"), action="update", plugin_id=pid, stage=stage) from exc
        finally:
            if stage_job is not None:
                shutil.rmtree(stage_job, ignore_errors=True)

    def rollback(
        self,
        plugin_id: str,
        *,
        snapshot: str | None = None,
        run_tests: bool = True,
        require_tests: bool = False,
        lock_timeout: float | None = None,
    ) -> InstallResult:
        pid = validate_plugin_id(plugin_id)
        target_sid = validate_snapshot_id(snapshot) if snapshot is not None else None
        timeout = self.lock_timeout if lock_timeout is None else float(lock_timeout)
        stage_job: Path | None = None
        stage = "init"
        try:
            with _acquire_file_lock(self.lock_path, timeout):
                self._ensure_install_dirs()
                self._ensure_backup_dirs(pid)
                if self._snapshots_meta_path(pid).is_symlink():
                    raise PathSafetyError(f"Refusing symlinked snapshots index: {self._snapshots_meta_path(pid)}")
                entries = self._read_snapshots_index(pid)
                available = self.list_snapshots(pid)
                if not available:
                    raise PluginNotFoundError(f"No rollback snapshots available for {pid!r}", plugin_id=pid)
                if target_sid is not None:
                    selected = next((s for s in available if s.snapshot_id == target_sid), None)
                    if selected is None:
                        raise PluginNotFoundError(
                            f"Snapshot {target_sid!r} not found for {pid!r}",
                            plugin_id=pid,
                            details={"available_snapshots": [s.snapshot_id for s in available]},
                        )
                else:
                    selected = available[-1]

                stage = "verify"
                validate_safe_tree(selected.path)
                stage_job = self.paths.staging_dir / f"rollback-{uuid.uuid4().hex[:10]}"
                stage_job.mkdir(parents=True, exist_ok=False)
                pkg_dir = stage_job / "package"
                _copy_clean_tree(selected.path, pkg_dir)
                manifest, verify_res = self._verify_staged(
                    pkg_dir, expected_id=pid, run_tests=run_tests, require_tests=require_tests
                )
                content_hash = compute_tree_hash(pkg_dir)
                restored_lock = dict(selected.lock_entry) if selected.lock_entry else {}
                restored_lock.update({"plugin_id": pid, "version": manifest.version, "content_hash": content_hash, "locked_at": _utc_iso()})

                stage = "activate"
                final_dir = self.plugins_dir / pid
                same_fs = self.plugins_dir / f".rollback-{pid}-{uuid.uuid4().hex[:8]}"
                try:
                    _move_or_copy_clean(pkg_dir, same_fs)
                    self._swap_dir_and_lock(
                        plugin_id=pid, final_dir=final_dir, same_fs_new=same_fs, new_lock_entry=restored_lock
                    )
                    remaining = [item for item in entries if item.get("snapshot_id") != selected.snapshot_id]
                    self._write_snapshots_index(pid, remaining)
                    shutil.rmtree(selected.path, ignore_errors=True)
                except BaseException:
                    shutil.rmtree(same_fs, ignore_errors=True)
                    raise
                return InstallResult(
                    True, "rollback", pid, manifest.version, final_dir, "snapshot",
                    str(restored_lock.get("url", "")), str(restored_lock.get("ref", "")), str(restored_lock.get("commit", "")),
                    content_hash, snapshot_id=selected.snapshot_id, snapshots=[s.snapshot_id for s in self.list_snapshots(pid)], verify=verify_res.to_dict(),
                )
        except OmniDeskError as exc:
            raise self._fail(exc, action="rollback", plugin_id=pid, stage=stage) from exc
        except OSError as exc:
            raise self._fail(OmniDeskError(f"Filesystem error during rollback: {exc}"), action="rollback", plugin_id=pid, stage=stage) from exc
        finally:
            if stage_job is not None:
                shutil.rmtree(stage_job, ignore_errors=True)


__all__ = ["DEFAULT_LOCK_TIMEOUT", "InstallResult", "MAX_SNAPSHOTS", "PluginInstaller", "SnapshotInfo", "compute_tree_hash"]
