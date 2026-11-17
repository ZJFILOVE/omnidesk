"""0600 atomic state storage, file locks, path safety, and protected playlist checks."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import _is_shared_system_root

PROTECTED_DAILY_PLAYLIST_ID = "p.YJXV7dEIeb3GzZX"
PROTECTED_DAILY_PLAYLIST_NAME = "ChatGPT Daily 5"
DEFAULT_INTERACTIVE_PLAYLIST = "OmniDesk AI 灵感电台"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class GateError(RuntimeError):
    """Fail-closed gate or persistence error with canonical error_code."""

    def __init__(self, error_code: str, message: str, extra: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.extra = dict(extra) if extra else {}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_valid_iso_date(value: str) -> bool:
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_isolated_data_dir(data_dir: Path) -> bool:
    tmp_root = Path(tempfile.gettempdir()).resolve()
    resolved = data_dir.expanduser().resolve()
    if resolved.is_relative_to(tmp_root):
        return True
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        default_data = (Path.home() / ".local" / "share").resolve()
        if Path(xdg_data).expanduser().resolve() != default_data:
            return True
    return bool(os.environ.get("XDG_STATE_HOME", "").strip())


def resolve_data_root(data_dir: Path) -> Path:
    resolved = data_dir.expanduser().resolve()
    return resolved.parent.parent if resolved.parent.name == "plugin-data" else resolved


def resolve_repo_dir(configured: str, data_dir: Path, name: str) -> Path | None:
    raw = (configured or "").strip()
    if raw:
        candidate = Path(raw).expanduser().resolve()
        return candidate if candidate.is_dir() else None
    candidates = [resolve_data_root(data_dir) / "curation" / "repos" / name]
    if not is_isolated_data_dir(data_dir):
        candidates.extend([
            Path("/home/zhu/Projects") / name,
            Path.home() / ".local" / "share" / "omnidesk" / "curation" / "repos" / name,
        ])
    return next((c.resolve() for c in candidates if c.is_dir()), None)


def resolve_daily_checkpoint_path(configured: str, data_dir: Path) -> Path:
    raw = (configured or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg_state:
        return Path(xdg_state).expanduser().resolve() / "daily5" / "apple-music-playlist.json"
    if is_isolated_data_dir(data_dir):
        return data_dir.expanduser().resolve() / "daily5" / "apple-music-playlist.json"
    return (Path.home() / ".local" / "state" / "daily5" / "apple-music-playlist.json").resolve()


def resolve_interactive_store_path(data_dir: Path) -> Path:
    return resolve_data_root(data_dir) / "curation" / "interactive" / "playlists.json"


def resolve_playback_queue_path(data_dir: Path) -> Path:
    return data_dir.expanduser().resolve() / "playback_queue.json"


def ensure_outside_repos(target: Path, *repos: Path | None) -> None:
    resolved = target.expanduser().resolve()
    for repo in repos:
        if repo is not None and resolved.is_relative_to(repo.resolve()):
            raise GateError("invalid_argument", "检查点路径必须位于仓库目录之外")


def atomic_write_json(target: Path, payload: Mapping[str, Any], *, mode: int = 0o600) -> None:
    if target.is_symlink():
        raise GateError("invalid_argument", f"拒绝写入符号链接文件: {target}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _is_shared_system_root(target.parent):
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    tmp_path: Path | None = None
    tmp_file = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False
    )
    try:
        tmp_path = Path(tmp_file.name)
        os.fchmod(tmp_file.fileno(), mode)
        tmp_file.write(serialized)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
        tmp_file.close()
        os.replace(tmp_path, target)
        os.chmod(target, mode)
    except BaseException:
        try:
            if not tmp_file.closed:
                tmp_file.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise


def read_private_json(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {}
    if path.is_symlink() or not path.is_file():
        raise GateError("invalid_argument", f"状态文件非普通文件或为符号链接: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            raw = handle.read()
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError("corrupt_state", f"状态文件损坏或不可读: {path}") from exc
    if not isinstance(data, dict):
        raise GateError("corrupt_state", f"状态文件必须为 JSON 对象: {path}")
    return data


@contextmanager
def acquire_state_lock(state_path: Path) -> Iterator[None]:
    lock_path = Path(f"{state_path}.lock")
    if lock_path.is_symlink():
        raise GateError("invalid_argument", f"拒绝使用符号链接锁文件: {lock_path}")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GateError("conflict", "已有另一份 Apple Music 歌单同步正在运行") from None
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def normalize_daily_state(raw: Mapping[str, Any]) -> dict[str, Any]:
    state = dict(raw) if isinstance(raw, Mapping) else {}
    state["schema_version"] = 2
    cum_id = str(state.get("cumulative_playlist_id") or "").strip() or PROTECTED_DAILY_PLAYLIST_ID
    state["cumulative_playlist_id"] = cum_id
    state["cumulative_playlist_name"] = PROTECTED_DAILY_PLAYLIST_NAME
    state["playlist_id"] = cum_id
    state["name"] = PROTECTED_DAILY_PLAYLIST_NAME
    for key in ("cumulative_track_ids", "pending_song_ids"):
        val = state.get(key)
        state[key] = [str(x) for x in val] if isinstance(val, list) else []
    for key in ("synced_dates", "dated_playlists"):
        val = state.get(key)
        state[key] = dict(val) if isinstance(val, Mapping) else {}
    return state


def collect_protected_ids(daily_state: Mapping[str, Any] | None) -> set[str]:
    protected = {PROTECTED_DAILY_PLAYLIST_ID}
    if not isinstance(daily_state, Mapping):
        return protected
    cum = daily_state.get("cumulative_playlist_id")
    if isinstance(cum, str) and cum.strip():
        protected.add(cum.strip())
    dated = daily_state.get("dated_playlists")
    if isinstance(dated, Mapping):
        for entry in dated.values():
            if isinstance(entry, Mapping):
                pid = entry.get("playlist_id") or entry.get("apple_music_playlist_id")
                if isinstance(pid, str) and pid.strip():
                    protected.add(pid.strip())
    return protected


def is_protected_playlist(playlist_id: str, playlist_name: str, protected_ids: set[str] | None = None) -> bool:
    pid = (playlist_id or "").strip()
    if pid == PROTECTED_DAILY_PLAYLIST_ID or (protected_ids and pid in protected_ids):
        return True
    name_clean = (playlist_name or "").strip().lower()
    if name_clean == PROTECTED_DAILY_PLAYLIST_NAME.lower() or name_clean.startswith("chatgpt daily 5"):
        return True
    return "chatgptdaily5" in re.sub(r"[^a-z0-9]", "", name_clean)
