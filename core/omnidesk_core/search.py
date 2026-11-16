"""SQLite FTS5 search index with automatic LIKE fallback and metadata secret stripping."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import ensure_private_dir, strip_secrets
from .errors import ErrorCode, InvalidArgumentError, OmniDeskError, PathSafetyError
from .paths import OmnideskPaths

logger = logging.getLogger(__name__)
_CJK_CHAR = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af\U00020000-\U0002ebe0"
_CJK_RE = re.compile(f"[{_CJK_CHAR}]")
_CJK_ASCII_RE = re.compile(f"(?<=[A-Za-z0-9])(?=[{_CJK_CHAR}])|(?<=[{_CJK_CHAR}])(?=[A-Za-z0-9])")


def _fts_text(text: str) -> str:
    return _CJK_ASCII_RE.sub(" ", text) if _CJK_RE.search(text) else text


_SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT NOT NULL UNIQUE, "
    "title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT '', "
    "source_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '', tags_text TEXT NOT NULL DEFAULT '', "
    "tags_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL DEFAULT 0.0);"
    "CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    "CREATE INDEX IF NOT EXISTS idx_documents_domain ON documents(domain);"
    "CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);"
    "CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents(kind);"
    "CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated_at DESC);"
)


def _clean_str(value: Any, *, field_name: str, require_nonempty: bool = False) -> str:
    if value is None and not require_nonempty:
        return ""
    if not isinstance(value, str) or "\x00" in value:
        raise InvalidArgumentError(f"{field_name} must be a string without null bytes")
    if require_nonempty and not value.strip():
        raise InvalidArgumentError(f"{field_name} must be a non-empty string")
    return value.strip() if require_nonempty else value


def _normalize_tags(tags: Sequence[str] | str | None) -> tuple[str, ...]:
    if tags is None:
        return ()
    if isinstance(tags, str):
        raw_items = tags.split(",") if "," in tags else tags.split()
    elif isinstance(tags, Sequence) and not isinstance(tags, (bytes, bytearray)):
        raw_items = list(tags)
    else:
        raise InvalidArgumentError("tags must be a sequence of strings or a string")
    return tuple(dict.fromkeys(c for item in raw_items if (c := _clean_str(item, field_name="tag").strip())))


def _sanitize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise InvalidArgumentError("metadata must be a mapping")
    cleaned = strip_secrets(metadata)
    try:
        json.dumps(cleaned, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidArgumentError(f"Document metadata must be JSON-serializable: {exc}") from None
    return cleaned


def _build_fts_query(raw_query: str, *, match_any: bool = False) -> str | None:
    clauses: list[str] = []
    for tok in _fts_text(raw_query).strip().split():
        if tok.endswith("*") and len(tok) > 1 and any(ch.isalnum() for ch in tok[:-1]):
            clauses.append(f'"{tok[:-1].replace(chr(34), chr(34) * 2)}"*')
        elif any(ch.isalnum() for ch in tok):
            clauses.append(f'"{tok.replace(chr(34), chr(34) * 2)}"')
    return (" OR " if match_any else " ").join(clauses) if clauses else None


def _make_like_snippet(title: str, body: str, tags_text: str, terms: Sequence[str], max_len: int = 160) -> str:
    lowered = [t.lower() for t in terms if t]
    for source in (body, title, tags_text):
        low = source.lower()
        for term in lowered:
            idx = low.find(term)
            if idx != -1:
                start, end = max(0, idx - 40), min(len(source), idx + len(term) + 80)
                pre, suf = ("..." if start > 0 else ""), ("..." if end < len(source) else "")
                return f"{pre}{source[start:idx]}<mark>{source[idx:idx + len(term)]}</mark>{source[idx + len(term):end]}{suf}"
    fallback = body or title or tags_text
    return fallback if len(fallback) <= max_len else fallback[:max_len].rstrip() + "..."


@dataclass(frozen=True, slots=True)
class SearchDocument:
    """Normalized document record stored in SearchIndex."""

    doc_id: str
    title: str = ""
    body: str = ""
    domain: str = ""
    source_id: str = ""
    kind: str = ""
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "tags": list(self.tags)}


@dataclass(frozen=True, slots=True)
class SearchHit(SearchDocument):
    """Ranked search match returned by SearchIndex.search."""

    score: float = 0.0
    snippet: str = ""
    backend: str = "fts5"


def _parse_updated_at(raw_ts: Any, fallback: float) -> float:
    if raw_ts is None or raw_ts == 0:
        return fallback
    if isinstance(raw_ts, bool):
        raise InvalidArgumentError("updated_at must be a finite number")
    try:
        val = float(raw_ts)
    except (TypeError, ValueError) as exc:
        raise InvalidArgumentError(f"updated_at must be a finite number: {exc}") from None
    if not math.isfinite(val):
        raise InvalidArgumentError("updated_at must be a finite number")
    return val


def _coerce_document(doc: SearchDocument | Mapping[str, Any] | str | None, **kwargs: Any) -> SearchDocument:
    now = _parse_updated_at(kwargs.get("updated_at"), time.time())
    raw: Mapping[str, Any] = (
        asdict(doc) if isinstance(doc, SearchDocument)
        else doc if isinstance(doc, Mapping)
        else {**kwargs, "doc_id": doc if doc is not None else kwargs.get("doc_id")}
    )
    return SearchDocument(
        doc_id=_clean_str(raw.get("doc_id", raw.get("id")), field_name="doc_id", require_nonempty=True),
        title=_clean_str(raw.get("title", ""), field_name="title"),
        body=_clean_str(raw.get("body", raw.get("content", "")), field_name="body"),
        domain=_clean_str(raw.get("domain", ""), field_name="domain"),
        source_id=_clean_str(raw.get("source_id") or raw.get("plugin_id") or "", field_name="source_id"),
        kind=_clean_str(raw.get("kind", ""), field_name="kind"),
        tags=_normalize_tags(raw.get("tags")),
        metadata=_sanitize_metadata(raw.get("metadata")),
        updated_at=_parse_updated_at(raw.get("updated_at"), now),
    )


def _resolve_source_filter(source_id: str | None, plugin_id: str | None) -> str | None:
    if source_id is not None and plugin_id is not None and source_id != plugin_id:
        raise InvalidArgumentError("Conflicting source_id and plugin_id filters")
    return source_id if source_id is not None else plugin_id


def _validate_pagination(limit: Any, offset: Any) -> None:
    if isinstance(limit, bool) or isinstance(offset, bool) or not isinstance(limit, int) or not isinstance(offset, int):
        raise InvalidArgumentError("limit and offset must be integers")
    if limit < 0 or offset < 0:
        raise InvalidArgumentError("limit and offset must be >= 0")


class SearchIndex:
    """SQLite FTS5 full-text search index with graceful degradation to SQL LIKE."""

    def __init__(self, db_path: Path | str | None = None, *, paths: OmnideskPaths | None = None, enable_fts: bool = True) -> None:
        if isinstance(db_path, str) and db_path != ":memory:" and not db_path.strip():
            raise InvalidArgumentError("db_path must not be empty")
        resolved = (paths or OmnideskPaths.from_env()).search_db_path if db_path is None else (":memory:" if isinstance(db_path, str) and db_path == ":memory:" else Path(db_path).expanduser())
        self._db_path: Path | str = resolved
        self._lock = threading.RLock()
        self._closed = False
        self._fts_enabled = False
        self._last_backend: str | None = None
        if isinstance(self._db_path, Path):
            self._prepare_db_path()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("py_lower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)
        self._initialize_schema(enable_fts=enable_fts)
        self._chmod_db_files()

    def _prepare_db_path(self) -> None:
        assert isinstance(self._db_path, Path)
        if self._db_path.parent.is_symlink():
            raise PathSafetyError(f"Refusing to open search database under symlinked directory: {self._db_path.parent}")
        if self._db_path.is_symlink():
            raise PathSafetyError(f"Refusing to open symlinked search database: {self._db_path}")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{self._db_path}{suffix}")
            if sidecar.is_symlink():
                raise PathSafetyError(f"Refusing symlinked SQLite sidecar file: {sidecar}")
        if self._db_path.exists():
            st = self._db_path.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise PathSafetyError(f"Search database path is not a regular file: {self._db_path}")
        ensure_private_dir(self._db_path.parent)
        if not self._db_path.exists():
            fd = os.open(self._db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)

    def _chmod_db_files(self) -> None:
        if not isinstance(self._db_path, Path):
            return
        for cand in (self._db_path, Path(f"{self._db_path}-wal"), Path(f"{self._db_path}-shm")):
            if cand.exists() and not cand.is_symlink():
                try:
                    os.chmod(cand, 0o600)
                except OSError:
                    pass

    def __enter__(self) -> SearchIndex: return self
    def __exit__(self, *_: object) -> None: self.close()
    @property
    def db_path(self) -> Path | str: return self._db_path
    @property
    def fts_enabled(self) -> bool: return self._fts_enabled
    @property
    def last_backend(self) -> str | None: return self._last_backend

    def _ensure_open(self) -> sqlite3.Connection:
        if self._closed:
            raise OmniDeskError("SearchIndex is closed", code=ErrorCode.CONFLICT)
        return self._conn

    def _exec_fts_write(self, conn: sqlite3.Connection, sql: str, params: Sequence[Any] = (), *, many: bool = False) -> bool:
        if not self._fts_enabled:
            return False
        try:
            conn.executemany(sql, params) if many else conn.execute(sql, params)
            return True
        except sqlite3.DatabaseError as exc:
            self._fts_enabled = False
            logger.warning("FTS5 unavailable or failed; degrading to SQL LIKE: %s", exc)
            return False

    def _initialize_schema(self, *, enable_fts: bool) -> None:
        with self._lock:
            conn = self._ensure_open()
            if isinstance(self._db_path, Path):
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                except sqlite3.DatabaseError:
                    pass
            with conn:
                conn.executescript(_SCHEMA_SQL)
            if enable_fts:
                self._init_fts_unlocked()

    @staticmethod
    def _set_fts_dirty_unlocked(conn: sqlite3.Connection, dirty: bool) -> None:
        conn.execute(
            "INSERT INTO index_meta(key, value) VALUES ('fts_dirty', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("1" if dirty else "0",),
        )

    @staticmethod
    def _is_fts_dirty_unlocked(conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT value FROM index_meta WHERE key = 'fts_dirty'").fetchone()
        return row is not None and row[0] == "1"

    def _init_fts_unlocked(self, *, force_rebuild: bool = False) -> bool:
        conn = self._ensure_open()
        self._fts_enabled = True
        with conn:
            ok = self._exec_fts_write(conn, "CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(title, body, tags, tokenize='unicode61')")
        if not ok:
            return False
        needs_rebuild = force_rebuild or self._is_fts_dirty_unlocked(conn)
        if not needs_rebuild:
            needs_rebuild = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] != conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
        return self._rebuild_fts_unlocked() if needs_rebuild else self._fts_enabled

    def _rebuild_fts_unlocked(self) -> bool:
        conn = self._ensure_open()
        with conn:
            if not self._exec_fts_write(conn, "DELETE FROM documents_fts"):
                return False
            rows = [(r["id"], _fts_text(r["title"]), _fts_text(r["body"]), _fts_text(r["tags_text"])) for r in conn.execute("SELECT id, title, body, tags_text FROM documents")]
            if rows and not self._exec_fts_write(conn, "INSERT INTO documents_fts(rowid, title, body, tags) VALUES (?, ?, ?, ?)", rows, many=True):
                return False
            self._set_fts_dirty_unlocked(conn, False)
            return True

    def rebuild_fts(self) -> bool:
        """Rebuild the FTS5 virtual table from stored documents."""
        with self._lock:
            return self._rebuild_fts_unlocked() if self._fts_enabled else self._init_fts_unlocked(force_rebuild=True)

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()
                self._chmod_db_files()

    def _upsert_unlocked(self, conn: sqlite3.Connection, item: SearchDocument) -> None:
        tags_text = " ".join(item.tags)
        conn.execute(
            "INSERT INTO documents (doc_id, title, body, domain, source_id, kind, tags_text, tags_json, metadata_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(doc_id) DO UPDATE SET "
            "title=excluded.title, body=excluded.body, domain=excluded.domain, source_id=excluded.source_id, "
            "kind=excluded.kind, tags_text=excluded.tags_text, tags_json=excluded.tags_json, "
            "metadata_json=excluded.metadata_json, updated_at=excluded.updated_at",
            (item.doc_id, item.title, item.body, item.domain, item.source_id, item.kind, tags_text,
             json.dumps(list(item.tags), ensure_ascii=False), json.dumps(item.metadata, ensure_ascii=False, sort_keys=True), item.updated_at),
        )
        synced = False
        if self._fts_enabled:
            row = conn.execute("SELECT id FROM documents WHERE doc_id = ?", (item.doc_id,)).fetchone()
            if row is not None and self._exec_fts_write(conn, "DELETE FROM documents_fts WHERE rowid = ?", (row["id"],)):
                synced = self._exec_fts_write(conn, "INSERT INTO documents_fts(rowid, title, body, tags) VALUES (?, ?, ?, ?)", (row["id"], _fts_text(item.title), _fts_text(item.body), _fts_text(tags_text)))
        if not synced:
            self._set_fts_dirty_unlocked(conn, True)

    def upsert(
        self, doc: SearchDocument | Mapping[str, Any] | str | None = None, title: str = "", body: str = "", *,
        doc_id: str | None = None, domain: str = "", source_id: str = "", plugin_id: str | None = None,
        kind: str = "", tags: Sequence[str] | str = (), metadata: Mapping[str, Any] | None = None, updated_at: float | None = None,
    ) -> SearchDocument:
        """Insert or update a searchable document record."""
        item = _coerce_document(doc, title=title, body=body, doc_id=doc_id, domain=domain, source_id=source_id,
                                plugin_id=plugin_id, kind=kind, tags=tags, metadata=metadata, updated_at=updated_at)
        self.upsert_many([item])
        return item

    def upsert_many(self, docs: Iterable[SearchDocument | Mapping[str, Any]]) -> int:
        """Insert or update multiple documents in a single atomic SQLite transaction."""
        items = [_coerce_document(d) for d in docs]
        if not items:
            return 0
        with self._lock:
            conn = self._ensure_open()
            with conn:
                for item in items:
                    self._upsert_unlocked(conn, item)
        return len(items)

    def _delete_unlocked(self, where_col: str, value: str) -> int:
        with self._lock:
            conn = self._ensure_open()
            with conn:
                rows = conn.execute(f"SELECT id FROM documents WHERE {where_col} = ?", (value,)).fetchall()
                if not rows:
                    return 0
                conn.execute(f"DELETE FROM documents WHERE {where_col} = ?", (value,))
                if not self._exec_fts_write(conn, "DELETE FROM documents_fts WHERE rowid = ?", [(r["id"],) for r in rows], many=True):
                    self._set_fts_dirty_unlocked(conn, True)
                return len(rows)

    def delete(self, doc_id: str) -> bool:
        """Delete a document by `doc_id`, returning True if it existed."""
        return self._delete_unlocked("doc_id", _clean_str(doc_id, field_name="doc_id", require_nonempty=True)) > 0

    def delete_by_source(self, source_id: str) -> int:
        """Delete all documents belonging to `source_id` and return the count removed."""
        return self._delete_unlocked("source_id", _clean_str(source_id, field_name="source_id", require_nonempty=True))

    def clear(self) -> int:
        """Remove all indexed documents and return the number deleted."""
        with self._lock:
            conn = self._ensure_open()
            with conn:
                total = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
                conn.execute("DELETE FROM documents")
                if not self._exec_fts_write(conn, "DELETE FROM documents_fts"):
                    if total > 0:
                        self._set_fts_dirty_unlocked(conn, True)
                else:
                    self._set_fts_dirty_unlocked(conn, False)
                return total

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "doc_id": row["doc_id"], "title": row["title"], "body": row["body"], "domain": row["domain"],
            "source_id": row["source_id"], "kind": row["kind"], "tags": tuple(json.loads(row["tags_json"])),
            "metadata": dict(json.loads(row["metadata_json"])), "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _filter_clauses(*, domain: str | None = None, source_id: str | None = None, kind: str | None = None, prefix: str = "") -> tuple[list[str], list[Any]]:
        pairs = [(col, val) for col, val in (("domain", domain), ("source_id", source_id), ("kind", kind)) if val is not None]
        return [f"{prefix}{col} = ?" for col, _ in pairs], [val for _, val in pairs]

    def get(self, doc_id: str) -> SearchDocument | None:
        """Fetch a stored document by `doc_id`."""
        clean_id = _clean_str(doc_id, field_name="doc_id", require_nonempty=True)
        with self._lock:
            row = self._ensure_open().execute("SELECT * FROM documents WHERE doc_id = ?", (clean_id,)).fetchone()
            return SearchDocument(**self._decode_row(row)) if row is not None else None

    def count(self, *, domain: str | None = None, source_id: str | None = None, plugin_id: str | None = None, kind: str | None = None) -> int:
        """Count stored documents matching optional metadata filters."""
        effective_source = _resolve_source_filter(source_id, plugin_id)
        where_sql, params = self._filter_clauses(domain=domain, source_id=effective_source, kind=kind)
        sql = "SELECT COUNT(*) FROM documents" + (f" WHERE {' AND '.join(where_sql)}" if where_sql else "")
        with self._lock:
            return int(self._ensure_open().execute(sql, params).fetchone()[0])

    def list_documents(self, *, domain: str | None = None, source_id: str | None = None, plugin_id: str | None = None, kind: str | None = None, limit: int = 50, offset: int = 0) -> list[SearchDocument]:
        """List stored documents sorted by recency."""
        _validate_pagination(limit, offset)
        effective_source = _resolve_source_filter(source_id, plugin_id)
        where_sql, params = self._filter_clauses(domain=domain, source_id=effective_source, kind=kind)
        sql = "SELECT * FROM documents" + (f" WHERE {' AND '.join(where_sql)}" if where_sql else "") + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        with self._lock:
            return [] if limit == 0 else [SearchDocument(**self._decode_row(r)) for r in self._ensure_open().execute(sql, [*params, limit, offset]).fetchall()]

    def search(
        self, query: str, *, domain: str | None = None, source_id: str | None = None, plugin_id: str | None = None,
        kind: str | None = None, limit: int = 20, offset: int = 0, use_fts: bool | None = None, match_any: bool = False,
    ) -> list[SearchHit]:
        """Search documents via FTS5 BM25 or fall back to SQL LIKE queries."""
        if not isinstance(query, str):
            raise InvalidArgumentError(f"query must be a string, got {type(query).__name__}")
        _validate_pagination(limit, offset)
        effective_source = _resolve_source_filter(source_id, plugin_id)
        if limit == 0 or not query.strip():
            return []
        has_cjk = bool(_CJK_RE.search(query))
        want_fts = self._fts_enabled and (use_fts is True or (use_fts is None and not has_cjk))
        with self._lock:
            if want_fts and (fts_expr := _build_fts_query(query, match_any=match_any)) is not None:
                try:
                    hits = self._search_fts_unlocked(fts_expr, domain=domain, source_id=effective_source, kind=kind, limit=limit, offset=offset)
                    if hits or not has_cjk:
                        self._last_backend = "fts5"
                        return hits
                except sqlite3.DatabaseError as exc:
                    if any(tok in str(exc).lower() for tok in ("no such table", "no such module")):
                        self._fts_enabled = False
                    logger.warning("FTS5 search failed; falling back to LIKE: %s", exc)
            hits = self._search_like_unlocked(query, domain=domain, source_id=effective_source, kind=kind, limit=limit, offset=offset, match_any=match_any)
            self._last_backend = "like"
            return hits

    def _search_fts_unlocked(self, fts_expr: str, *, domain: str | None, source_id: str | None, kind: str | None, limit: int, offset: int) -> list[SearchHit]:
        filters, filter_params = self._filter_clauses(domain=domain, source_id=source_id, kind=kind, prefix="d.")
        sql = (
            "SELECT d.*, bm25(documents_fts, 10.0, 1.0, 5.0) AS rank_score, "
            "snippet(documents_fts, -1, '<mark>', '</mark>', '...', 16) AS match_snippet "
            "FROM documents_fts JOIN documents AS d ON d.id = documents_fts.rowid "
            f"WHERE {' AND '.join(['documents_fts MATCH ?', *filters])} ORDER BY rank_score ASC, d.updated_at DESC, d.id DESC LIMIT ? OFFSET ?"
        )
        rows = self._ensure_open().execute(sql, [fts_expr, *filter_params, limit, offset]).fetchall()
        return [SearchHit(**self._decode_row(r), score=max(0.0, -float(r["rank_score"])), snippet=r["match_snippet"] or "", backend="fts5") for r in rows]

    def _search_like_unlocked(self, raw_query: str, *, domain: str | None, source_id: str | None, kind: str | None, limit: int, offset: int, match_any: bool) -> list[SearchHit]:
        raw_terms = list(dict.fromkeys(tok.rstrip("*") or tok for tok in raw_query.strip().split() if tok))
        if not raw_terms:
            return []
        term_clauses, term_params, score_parts, score_params = [], [], [], []
        for term in raw_terms:
            esc = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pat = f"%{esc}%"
            term_clauses.append("(py_lower(title) LIKE ? ESCAPE '\\' OR py_lower(body) LIKE ? ESCAPE '\\' OR py_lower(tags_text) LIKE ? ESCAPE '\\')")
            term_params.extend([pat, pat, pat])
            score_parts.append("(CASE WHEN py_lower(title) LIKE ? ESCAPE '\\' THEN 10.0 ELSE 0.0 END + CASE WHEN py_lower(tags_text) LIKE ? ESCAPE '\\' THEN 5.0 ELSE 0.0 END + CASE WHEN py_lower(body) LIKE ? ESCAPE '\\' THEN 1.0 ELSE 0.0 END)")
            score_params.extend([pat, pat, pat])
        filters, filter_params = self._filter_clauses(domain=domain, source_id=source_id, kind=kind)
        where_sql = " AND ".join([f"({(' OR ' if match_any else ' AND ').join(term_clauses)})", *filters])
        sql = f"SELECT *, ({' + '.join(score_parts)}) AS like_score FROM documents WHERE {where_sql} ORDER BY like_score DESC, updated_at DESC, id DESC LIMIT ? OFFSET ?"
        rows = self._ensure_open().execute(sql, [*score_params, *term_params, *filter_params, limit, offset]).fetchall()
        return [SearchHit(**self._decode_row(r), score=float(r["like_score"]), snippet=_make_like_snippet(r["title"], r["body"], r["tags_text"], raw_terms), backend="like") for r in rows]
