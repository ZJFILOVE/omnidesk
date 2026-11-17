"""Shared models, path safety, atomic file I/O, and identity extraction for plugin-ai-curator."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from omnidesk_sdk import BookItem, BookPageBlock, BookSpread, MediaItem, TextArticle, TextFeedItem, VocabCard

HISTORY_START_DATE = "2026-09-04"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_OBSIDIAN_REMOTE = "https://github.com/ZJFILOVE/obsidian-vault.git"
DEFAULT_MUSIC_REMOTE = "https://github.com/ZJFILOVE/music-daily.git"
ALLOWED_DAILY_PROJECTS = frozenset({"music", "news-youtube-reddit", "english", "journal", "daily"})
ALLOWED_REPEAT_POLICIES = frozenset({"allow_any", "prefer_familiar", "session_dedup_only"})
MAX_PROVENANCE_ITEMS = 4096

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\]>\"'`,;)]+", re.IGNORECASE)
_AM_SONG_RE = re.compile(r"/song/(?:[^/]+/)?(\d+)")
_REDDIT_POST_RE = re.compile(r"/comments/([A-Za-z0-9]+)")


class CuratorError(Exception):
    """Domain error carrying a machine-readable error_code."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.message = message
        super().__init__(message)


class LedgerReadError(CuratorError):
    """Raised when an upstream archive or ledger file is unreadable, truncated, or malformed."""

    def __init__(self, message: str, *, error_code: str = "unreadable_upstream") -> None:
        super().__init__(error_code, message)


def current_shanghai_date() -> str:
    """Return today's YYYY-MM-DD business date in Asia/Shanghai."""
    return datetime.now(SHANGHAI_TZ).date().isoformat()


def is_valid_iso_date(value: str) -> bool:
    """Validate strict YYYY-MM-DD calendar date string."""
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def safe_identifier(value: str) -> bool:
    """Return True if value is a safe identifier without path traversal."""
    return (
        isinstance(value, str)
        and bool(_SAFE_IDENT_RE.match(value))
        and "/" not in value
        and "\\" not in value
        and ".." not in value
        and "\x00" not in value
    )


def safe_child_path(base_dir: Path, *parts: str) -> Path:
    """Resolve child path under base_dir and reject traversal or symlink escape."""
    root = base_dir.resolve()
    cur = root
    for part in parts:
        if not isinstance(part, str) or not part or part in {".", ".."} or "\x00" in part:
            raise CuratorError("invalid_argument", f"Unsafe path component: {part!r}")
        p = Path(part)
        if p.is_absolute():
            raise CuratorError("invalid_argument", f"Absolute path component forbidden: {part!r}")
        cur = cur / p
        if cur.is_symlink():
            raise CuratorError("path_safety", f"Symlink path forbidden: {cur}")
    resolved = cur.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CuratorError("path_safety", f"Path escapes base directory: {resolved}") from exc
    return resolved


def ensure_safe_file_in_root(target: Path, repo_root: Path | None = None) -> Path:
    """Verify target is a regular non-escaping file (not a dangling or escaping symlink)."""
    if target.is_symlink():
        resolved = target.resolve()
        if repo_root is None:
            raise LedgerReadError(f"Refusing symlinked file: {target}")
        try:
            resolved.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise LedgerReadError(f"Symlink escapes repository root: {target}") from exc
    if not target.exists():
        raise LedgerReadError(f"Missing file: {target}")
    if not target.is_file():
        raise LedgerReadError(f"Not a regular file: {target}")
    return target


def read_bytes_strict(target: Path, *, repo_root: Path | None = None) -> bytes:
    """Read file bytes strictly, raising LedgerReadError on permission/IO/symlink issues."""
    ensure_safe_file_in_root(target, repo_root=repo_root)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if target.is_symlink() and repo_root is not None:
            return target.read_bytes()
        fd = os.open(target, flags)
        with os.fdopen(fd, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise LedgerReadError(f"Unreadable file {target}: {exc}") from exc


def read_text_strict(target: Path, *, repo_root: Path | None = None) -> str:
    """Read UTF-8 text strictly, raising LedgerReadError on unreadable or invalid UTF-8 files."""
    raw = read_bytes_strict(target, repo_root=repo_root)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerReadError(f"Invalid UTF-8 in {target}: {exc}") from exc


def read_json_strict(target: Path, *, repo_root: Path | None = None, error_code: str = "unreadable_upstream") -> Any:
    """Read and parse JSON strictly, failing closed on empty/truncated/corrupted files."""
    try:
        text = read_text_strict(target, repo_root=repo_root)
    except LedgerReadError as exc:
        raise LedgerReadError(exc.message, error_code=error_code) from exc
    if not text.strip():
        raise LedgerReadError(f"Empty or truncated JSON file: {target}", error_code=error_code)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerReadError(
            f"Malformed or truncated JSON in {target} (line {exc.lineno} col {exc.colno}): {exc.msg}",
            error_code=error_code,
        ) from exc


def _is_shared_system_root(path: Path) -> bool:
    resolved = path.resolve()
    protected = {Path("/"), Path("/tmp"), Path("/var/tmp"), Path.home().resolve()}
    for env_name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            protected.add(Path(raw).expanduser().resolve())
    return resolved in protected


def ensure_private_dir(directory: Path, *, dir_mode: int = 0o700) -> Path:
    """Create directory hierarchy with private permissions unless shared system root."""
    if directory.is_symlink():
        raise CuratorError("path_safety", f"Refusing symlinked directory: {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    if not _is_shared_system_root(directory):
        try:
            os.chmod(directory, dir_mode)
        except OSError:
            pass
    return directory


def atomic_write_text(target: Path, content: str, *, file_mode: int = 0o600) -> None:
    """Atomically write UTF-8 text via temp file + fsync + os.replace."""
    if target.is_symlink():
        raise CuratorError("path_safety", f"Refusing symlinked target file: {target}")
    ensure_private_dir(target.parent)
    tmp_path: Path | None = None
    tmp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp_path = Path(tmp_file.name)
        os.fchmod(tmp_file.fileno(), file_mode)
        tmp_file.write(content)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
        tmp_file.close()
        os.replace(tmp_path, target)
        os.chmod(target, file_mode)
    except BaseException:
        try:
            if not tmp_file.closed:
                tmp_file.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(target: Path, payload: Any, *, file_mode: int = 0o600) -> None:
    """Atomically write formatted JSON payload."""
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(target, serialized, file_mode=file_mode)


def git_blob_sha1(data: bytes) -> str:
    """Compute exact Git blob SHA-1 hash for raw bytes."""
    header = f"blob {len(data)}\x00".encode("utf-8")
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324 - Git blob digest


def normalize_for_dedup(text: str) -> str:
    """Normalize string to lowercase ASCII alphanumerics + non-ASCII chars for dedup matching."""
    return "".join(ch.lower() if ch.isascii() and ch.isalnum() else ch for ch in text if (ch.isalnum() or not ch.isascii()))


def normalize_doi(doi: str) -> str:
    """Strip common DOI URL/scheme prefixes and lowercase."""
    val = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if val.startswith(prefix):
            val = val[len(prefix) :]
    return val.strip().rstrip(".,;)")


def extract_doi(text: str) -> str:
    """Extract normalized DOI from free text or URL."""
    m = _DOI_RE.search(text or "")
    return normalize_doi(m.group(0)) if m else ""


def extract_apple_music_song_id(url: str) -> str:
    """Extract numeric Apple Music song ID from URL or ?i= query param."""
    if not url:
        return ""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "i" in qs and qs["i"] and qs["i"][0].isdigit():
        return qs["i"][0]
    m = _AM_SONG_RE.search(parsed.path)
    return m.group(1) if m else ""


def extract_apple_music_storefront(url: str) -> str:
    """Extract 2-letter storefront from music.apple.com/<sf>/... URL."""
    if "music.apple.com/" not in (url or ""):
        return ""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parts and len(parts[0]) == 2 and parts[0].isalpha():
        return parts[0].lower()
    return ""


def extract_youtube_video_id(url: str) -> str:
    """Extract 11-character YouTube video ID from watch/youtu.be/shorts/embed URLs."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    qs = parse_qs(parsed.query)
    if "v" in qs and qs["v"] and _YT_ID_RE.match(qs["v"][0]):
        return qs["v"][0]
    parts = [p for p in parsed.path.split("/") if p]
    if "youtu.be" in host and parts and _YT_ID_RE.match(parts[0]):
        return parts[0]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"} and _YT_ID_RE.match(parts[1]):
        return parts[1]
    return ""


def extract_reddit_post_id(url: str) -> str:
    """Extract Reddit submission ID from /comments/<id>/ URL."""
    m = _REDDIT_POST_RE.search(url or "")
    return m.group(1) if m else ""


def parse_query_filter(raw_query: str) -> tuple[str, str]:
    """Extract optional `date:YYYY-MM-DD` token and lowercased keyword string."""
    date_filter = ""
    tokens: list[str] = []
    for tok in (raw_query or "").split():
        if tok.startswith("date:") and len(tok) == 15 and is_valid_iso_date(tok[5:]):
            date_filter = tok[5:]
        else:
            tokens.append(tok)
    return date_filter, " ".join(tokens).strip().lower()


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate text cleanly at max_chars with ellipsis."""
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max(0, max_chars - 1)].rstrip() + "…"


def record_provenance(mapping: dict[str, str], key: str, iso_date: str) -> None:
    """Record earliest historical recommendation date for a lookup key."""
    if not key or not is_valid_iso_date(iso_date):
        return
    prev = mapping.get(key)
    if prev is None or iso_date < prev:
        mapping[key] = iso_date


@dataclass(slots=True)
class RepoPaths:
    obsidian_vault_dir: Path
    music_daily_dir: Path
    curation_root: Path


@dataclass(slots=True)
class RepoSyncStatus:
    name: str
    path: str
    exists: bool = False
    is_git_repo: bool = False
    has_remote: bool = False
    cloned: bool = False
    pulled: bool = False
    dirty: bool = False
    dirty_worktree_preserved: bool = False
    ahead_count: int = 0
    behind_count: int = 0
    head_sha: str = ""
    remote_head_sha: str = ""
    error: str = ""
    status_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "exists": self.exists,
            "is_git_repo": self.is_git_repo,
            "has_remote": self.has_remote,
            "cloned": self.cloned,
            "pulled": self.pulled,
            "dirty": self.dirty,
            "dirty_worktree_preserved": self.dirty_worktree_preserved,
            "ahead_count": self.ahead_count,
            "behind_count": self.behind_count,
            "head_sha": self.head_sha,
            "remote_head_sha": self.remote_head_sha,
            "error": self.error,
            "status_detail": self.status_detail,
        }


@dataclass(slots=True)
class DateStats:
    music: int = 0
    youtube: int = 0
    news: int = 0
    reddit: int = 0
    journal: int = 0
    english: int = 0

    def total(self) -> int:
        return self.music + self.youtube + self.news + self.reddit + self.journal + self.english

    def to_dict(self) -> dict[str, int]:
        return {
            "music": self.music,
            "youtube": self.youtube,
            "news": self.news,
            "reddit": self.reddit,
            "journal": self.journal,
            "english": self.english,
            "total": self.total(),
        }


@dataclass(slots=True)
class EnglishNoteEntry:
    component: str
    date: str
    title: str
    rel_path: str
    blocks: list[BookPageBlock] = field(default_factory=list)
    vocab_cards: list[VocabCard] = field(default_factory=list)


@dataclass(slots=True)
class EnglishDayBundle:
    date: str
    notes: list[EnglishNoteEntry] = field(default_factory=list)


@dataclass(slots=True)
class InteractiveSession:
    session_id: str
    title: str = "AI 灵感推荐"
    user_prompt: str = ""
    repeat_policy: str = "allow_any"
    media_items: list[MediaItem] = field(default_factory=list)
    text_items: list[TextFeedItem] = field(default_factory=list)
    articles: dict[str, TextArticle] = field(default_factory=dict)
    book_items: list[BookItem] = field(default_factory=list)
    book_spreads: dict[str, BookSpread] = field(default_factory=dict)


@dataclass(slots=True)
class HydratedArchive:
    media_items: list[MediaItem] = field(default_factory=list)
    text_items: list[TextFeedItem] = field(default_factory=list)
    articles: dict[str, TextArticle] = field(default_factory=dict)
    vocab_cards: list[VocabCard] = field(default_factory=list)
    english_days: dict[str, EnglishDayBundle] = field(default_factory=dict)
    date_stats: dict[str, DateStats] = field(default_factory=dict)
    history_anomalies: list[dict[str, Any]] = field(default_factory=list)
    song_id_to_date: dict[str, str] = field(default_factory=dict)
    canonical_id_to_date: dict[str, str] = field(default_factory=dict)
    video_id_to_date: dict[str, str] = field(default_factory=dict)
    post_id_to_date: dict[str, str] = field(default_factory=dict)
    doi_to_date: dict[str, str] = field(default_factory=dict)
    url_to_date: dict[str, str] = field(default_factory=dict)
    norm_title_to_date: dict[str, str] = field(default_factory=dict)
    dates_indexed: int = 0
    music_tracks_hydrated: int = 0
    youtube_videos_hydrated: int = 0
    news_days_hydrated: int = 0
    news_stories_hydrated: int = 0
    reddit_days_hydrated: int = 0
    reddit_posts_hydrated: int = 0
    journal_days_hydrated: int = 0
    journal_papers_hydrated: int = 0
    english_notes_hydrated: int = 0
    vocab_cards_hydrated: int = 0

    def rebuild_indices(self) -> None:
        """Rebuild lookup provenance indices and date count summary."""
        self.dates_indexed = len(self.date_stats)
        for item in self.media_items:
            bdate = str(item.business_date or item.extra.get("business_date") or "")
            if not is_valid_iso_date(bdate):
                continue
            record_provenance(self.song_id_to_date, str(item.extra.get("apple_music_song_id") or ""), bdate)
            record_provenance(self.canonical_id_to_date, str(item.extra.get("canonical_id") or ""), bdate)
            record_provenance(self.video_id_to_date, str(item.extra.get("video_id") or ""), bdate)
            record_provenance(self.url_to_date, item.play_url.rstrip("/"), bdate)
            record_provenance(self.norm_title_to_date, normalize_for_dedup(item.title), bdate)
        for item in self.text_items:
            bdate = str(item.business_date or item.published_at or "")
            if not is_valid_iso_date(bdate):
                continue
            record_provenance(self.url_to_date, item.url.rstrip("/"), bdate)
            record_provenance(self.norm_title_to_date, normalize_for_dedup(item.title), bdate)
            if item.category == "reddit" and item.url:
                record_provenance(self.post_id_to_date, extract_reddit_post_id(item.url), bdate)
            if item.doi:
                record_provenance(self.doi_to_date, normalize_doi(item.doi), bdate)
            art = self.articles.get(item.id)
            if art and art.doi:
                record_provenance(self.doi_to_date, normalize_doi(art.doi), bdate)
