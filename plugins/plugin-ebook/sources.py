"""BookSource implementation for plugin-ebook with sidecar progress and library scan."""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnidesk_sdk import BookFormat, BookItem, BookPageBlock, BookSource, BookSpread, VocabCard

if __package__:
    from .ebook_lib.companion import sync_with_remote
    from .ebook_lib.errors import (
        BookNotFoundError,
        BookParseError,
        EbookPathSafetyError,
        InvalidEbookArgError,
    )
    from .ebook_lib.parsers import (
        SUPPORTED_EXTENSIONS,
        ParsedBook,
        convert_and_parse_mobi,
        parse_book_file,
        resolve_calibre_bin,
    )
    from .ebook_lib.progress import ProgressStore
    from .ebook_lib.security import iter_safe_files, parse_library_roots, validate_book_file, validate_library_root
    from .ebook_lib.vocab import VocabStore
else:
    from ebook_lib.companion import sync_with_remote
    from ebook_lib.errors import (
        BookNotFoundError,
        BookParseError,
        EbookPathSafetyError,
        InvalidEbookArgError,
    )
    from ebook_lib.parsers import (
        SUPPORTED_EXTENSIONS,
        ParsedBook,
        convert_and_parse_mobi,
        parse_book_file,
        resolve_calibre_bin,
    )
    from ebook_lib.progress import ProgressStore
    from ebook_lib.security import iter_safe_files, parse_library_roots, validate_book_file, validate_library_root
    from ebook_lib.vocab import VocabStore

if TYPE_CHECKING:
    from omnidesk_sdk import PluginContext

_WORD_SPLIT_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


def stable_book_id(resolved_path: Path) -> str:
    """Compute deterministic traversal-free ID for a canonical book path."""
    return f"ebook-{hashlib.sha256(str(resolved_path).encode('utf-8')).hexdigest()[:16]}"


def _highlight_vocab_words(text: str, known_words: set[str]) -> list[str]:
    if not text or not known_words:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _WORD_SPLIT_RE.finditer(text):
        low = match.group(0).lower()
        if low in known_words and low not in seen:
            seen.add(low)
            found.append(low)
    return found


class EbookBookSource(BookSource):
    """Domain 3 BookSource backed by user-configured local library_roots."""

    source_id: str = "plugin-ebook"
    display_name: str = "Ebook & SM-2 Reader"

    def __init__(self, ctx: PluginContext | None, vocab_store: VocabStore) -> None:
        self._ctx = ctx
        self._vocab_store = vocab_store
        self._progress = ProgressStore()
        self._lock = threading.RLock()
        self._cache: dict[tuple[Path, int, int, int], ParsedBook] = {}

    def bind_context(self, ctx: PluginContext | None) -> None:
        self._ctx = ctx

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
        self._progress.clear()

    def _cfg(self, key: str, default: Any = None) -> Any:
        return default if self._ctx is None else self._ctx.get_config(key, default)

    def _page_budget(self) -> int:
        try:
            return max(200, int(self._cfg("page_char_budget", 1800)))
        except (TypeError, ValueError):
            return 1800

    def _resolved_roots(self) -> list[Path]:
        valid: list[Path] = []
        for raw in parse_library_roots(self._cfg("library_roots", "")):
            root = validate_library_root(raw)
            if root is not None and root not in valid:
                valid.append(root)
        return valid

    def _scan_entries(self) -> dict[str, tuple[Path, Path, BookFormat]]:
        entries: dict[str, tuple[Path, Path, BookFormat]] = {}
        for root_dir, file_path in iter_safe_files(self._resolved_roots(), SUPPORTED_EXTENSIONS):
            fmt: BookFormat = file_path.suffix.lower().lstrip(".")  # type: ignore[assignment]
            entries[stable_book_id(file_path)] = (root_dir, file_path, fmt)
        return entries

    async def open_file(self, raw_path: str) -> BookItem:
        roots = self._resolved_roots()
        if not roots:
            raise InvalidEbookArgError("请先把下载目录加入电子书插件的 library_roots")
        path = validate_book_file(Path(raw_path).expanduser(), roots=roots)
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise InvalidEbookArgError("此格式不受电子书阅读器支持")
        return await self.get_book(stable_book_id(path))

    def _get_cached_or_parse_sync(self, file_path: Path, fmt: BookFormat) -> ParsedBook | None:
        st = file_path.lstat()
        key = (file_path, st.st_mtime_ns, st.st_size, self._page_budget())
        with self._lock:
            if (cached := self._cache.get(key)) is not None:
                return cached
        if fmt not in ("epub", "txt"):
            return None
        parsed = parse_book_file(file_path, fmt, page_char_budget=self._page_budget())
        with self._lock:
            self._cache[key] = parsed
        return parsed

    def _build_book_item(
        self, book_id: str, root_dir: Path, file_path: Path, fmt: BookFormat, vocab_count: int
    ) -> BookItem:
        title, author, language, total_pages = file_path.stem, "", "", 1
        extra: dict[str, Any] = {"root": str(root_dir)}
        calibre_bin = str(self._cfg("calibre_bin", "") or "").strip()

        if fmt not in ("epub", "txt"):
            st = file_path.lstat()
            with self._lock:
                parsed = self._cache.get((file_path, st.st_mtime_ns, st.st_size, self._page_budget()))
            calibre_ok = resolve_calibre_bin(calibre_bin) is not None
            if parsed is not None:
                title, author, language = parsed.title or title, parsed.author, parsed.language
                total_pages = max(1, len(parsed.pages))
                extra.update({"toc": parsed.toc, "format_available": True, **parsed.extra})
            else:
                extra["format_available"] = calibre_ok
                if not calibre_ok:
                    extra["unavailable_reason"] = "format_unavailable"
        else:
            try:
                parsed = self._get_cached_or_parse_sync(file_path, fmt)
                if parsed is not None:
                    title, author, language = parsed.title or title, parsed.author, parsed.language
                    total_pages = max(1, len(parsed.pages))
                    extra.update({"toc": parsed.toc, "format_available": True, **parsed.extra})
            except (BookParseError, EbookPathSafetyError) as exc:
                extra.update({"format_available": False, "parse_error": str(exc)})

        prog = self._progress.get(self._ctx, book_id, root_dir, file_path)
        raw_cur = prog.get("current_page") or prog.get("page") or prog.get("page_number") or 1
        try:
            cur_page = max(1, min(total_pages, int(raw_cur)))
        except (TypeError, ValueError):
            cur_page = 1
        raw_pct = prog.get("progress_percent")
        if prog and isinstance(raw_pct, (int, float)) and not isinstance(raw_pct, bool) and 0.0 <= raw_pct <= 100.0:
            pct = round(float(raw_pct), 2)
        else:
            pct = round((cur_page / total_pages) * 100.0, 2) if prog else 0.0

        return BookItem(
            id=book_id,
            source_id=self.source_id,
            title=title,
            author=author,
            format=fmt,
            language=language,
            file_path=str(file_path),
            badge=fmt.upper(),
            current_page=cur_page,
            total_pages=total_pages,
            progress_percent=pct,
            bilingual_ready=bool(self._ctx and self._ctx.capabilities.has("llm.translate.v1")),
            vocab_count=vocab_count,
            extra=extra,
        )

    async def list_books(self, format_filter: str = "", query: str = "") -> list[BookItem]:
        """Scan configured `library_roots` and return matching BookItem entries."""
        entries = self._scan_entries()
        fmt_q = "" if (f := (format_filter or "").strip().lower()) == "all" else f
        text_q = (query or "").strip().lower()
        vocab_count = len(self._vocab_store.known_words())

        items: list[BookItem] = []
        for bid, (root_dir, file_path, fmt) in entries.items():
            if fmt_q and fmt != fmt_q:
                continue
            item = self._build_book_item(bid, root_dir, file_path, fmt, vocab_count)
            if text_q and not any(
                text_q in val.lower()
                for val in (item.title, item.author, file_path.name, item.language, item.format)
            ):
                continue
            items.append(item)
        items.sort(key=lambda b: (b.title.lower(), b.file_path))
        return items

    def _resolve_book_entry(self, book_id: str) -> tuple[Path, Path, BookFormat]:
        if not isinstance(book_id, str) or not book_id.strip():
            raise InvalidEbookArgError("book_id must be a non-empty string")
        cleaned = book_id.strip()
        if "/" in cleaned or "\\" in cleaned or ".." in cleaned or "\x00" in cleaned:
            raise EbookPathSafetyError(f"Unsafe book_id {book_id!r}")
        entries = self._scan_entries()
        if cleaned not in entries:
            raise BookNotFoundError(f"Book not found: {cleaned}")
        root_dir, file_path, fmt = entries[cleaned]
        validate_book_file(file_path, roots=[root_dir])
        return root_dir, file_path, fmt

    async def _load_parsed_book(self, root_dir: Path, file_path: Path, fmt: BookFormat) -> ParsedBook:
        validate_book_file(file_path, roots=[root_dir])
        st = file_path.lstat()
        key = (file_path, st.st_mtime_ns, st.st_size, self._page_budget())
        with self._lock:
            if (cached := self._cache.get(key)) is not None:
                return cached
        if fmt not in ("epub", "txt"):
            if self._ctx is None:
                raise InvalidEbookArgError("PluginContext is required for Calibre conversion")
            calibre_bin = str(self._cfg("calibre_bin", "") or "").strip()
            parsed = await convert_and_parse_mobi(
                file_path, fmt, calibre_bin, self._ctx.exec_subprocess, page_char_budget=self._page_budget()
            )
        else:
            parsed = parse_book_file(file_path, fmt, page_char_budget=self._page_budget())
        with self._lock:
            self._cache[key] = parsed
        return parsed

    async def get_book(self, book_id: str) -> BookItem:
        root_dir, file_path, fmt = self._resolve_book_entry(book_id)
        return self._build_book_item(book_id.strip(), root_dir, file_path, fmt, len(self._vocab_store.known_words()))

    async def _maybe_translate_text(self, text: str, target_language: str) -> str:
        if self._ctx is not None and self._ctx.capabilities.has("llm.translate.v1"):
            try:
                res = await self._ctx.capabilities.call("llm.translate.v1", {"text": text, "target_lang": target_language})
                if res.ok:
                    val = res.data.get("translation") or res.data.get("translated") or res.data.get("reply")
                    if isinstance(val, str) and val.strip():
                        return val.strip()
            except Exception:
                return ""
        return ""

    async def fetch_spread(
        self, book_id: str, page_number: int = 1, *, translate: bool = False, record_progress: bool = True
    ) -> BookSpread:
        """Fetch paginated bilingual BookSpread for a real book in `library_roots`."""
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            raise InvalidEbookArgError("page_number must be an integer >= 1")
        root_dir, file_path, fmt = self._resolve_book_entry(book_id)
        parsed = await self._load_parsed_book(root_dir, file_path, fmt)
        total_pages = max(1, len(parsed.pages))
        if page_number > total_pages:
            raise InvalidEbookArgError(f"page_number {page_number} exceeds total_pages {total_pages}")

        page = parsed.pages[page_number - 1]
        known_words = self._vocab_store.known_words()
        target_lang = str(self._cfg("target_language", "zh-CN") or "zh-CN").strip()

        out_blocks: list[BookPageBlock] = []
        for blk in page.blocks:
            translated = blk.translated
            if translate and not translated and blk.kind != "image" and blk.original:
                translated = await self._maybe_translate_text(blk.original, target_lang)
            out_blocks.append(
                BookPageBlock(
                    id=blk.id,
                    kind=blk.kind,
                    original=blk.original,
                    translated=translated,
                    image_url=blk.image_url,
                    highlighted_words=_highlight_vocab_words(blk.original, known_words),
                )
            )

        sync_status: dict[str, Any] | None = None
        if record_progress:
            pct = round((page_number / total_pages) * 100.0, 2)
            self._progress.save(self._ctx, book_id.strip(), current_page=page_number, total_pages=total_pages, progress_percent=pct)
            sync_status = await sync_with_remote(
                self._ctx,
                sync_enabled=bool(self._cfg("sync_enabled", True)),
                action="sync_progress",
                payload={
                    "plugin_id": self.source_id,
                    "book_id": book_id.strip(),
                    "title": parsed.title,
                    "format": parsed.book_format,
                    "current_page": page_number,
                    "total_pages": total_pages,
                    "progress_percent": pct,
                },
            )

        extra: dict[str, Any] = {"format": parsed.book_format, "book_title": parsed.title, "toc_count": len(parsed.toc)}
        if sync_status is not None:
            extra["sync_status"] = sync_status
        return BookSpread(
            book_id=book_id.strip(),
            chapter_title=page.chapter_title,
            page_number=page_number,
            total_pages=total_pages,
            blocks=out_blocks,
            extra=extra,
        )

    async def update_progress(self, book_id: str, current_page: int) -> dict[str, Any]:
        """Persist reading progress locally first, then best-effort sync via book.sync.v1."""
        if isinstance(current_page, bool) or not isinstance(current_page, int) or current_page < 1:
            raise InvalidEbookArgError("current_page must be an integer >= 1")
        root_dir, file_path, fmt = self._resolve_book_entry(book_id)
        parsed = await self._load_parsed_book(root_dir, file_path, fmt)
        total_pages = max(1, len(parsed.pages))
        if current_page > total_pages:
            raise InvalidEbookArgError(f"current_page {current_page} exceeds total_pages {total_pages}")
        pct = round((current_page / total_pages) * 100.0, 2)
        record = self._progress.save(
            self._ctx, book_id.strip(), current_page=current_page, total_pages=total_pages, progress_percent=pct
        )
        sync_status = await sync_with_remote(
            self._ctx,
            sync_enabled=bool(self._cfg("sync_enabled", True)),
            action="sync_progress",
            payload={
                "plugin_id": self.source_id,
                "book_id": book_id.strip(),
                "title": parsed.title,
                "format": parsed.book_format,
                "current_page": current_page,
                "total_pages": total_pages,
                "progress_percent": pct,
            },
        )
        return {**record, "sync_status": sync_status}

    async def list_vocab(self) -> list[VocabCard]:
        """Return SM-2 vocabulary cards stored by plugin-ebook."""
        return self._vocab_store.list_vocab_models()
