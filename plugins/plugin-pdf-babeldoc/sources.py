"""BookSource implementation for local PDF libraries and BabelDOC dual-column artifacts."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BookItem, BookPageBlock, BookSource, BookSpread, VocabCard

if __package__:
    from . import artifacts, job_store, pdf_utils
else:
    import artifacts  # type: ignore[import-not-found,no-redef]
    import job_store  # type: ignore[import-not-found,no-redef]
    import pdf_utils  # type: ignore[import-not-found,no-redef]


class _PageBoundsError(ValueError):
    """Raised when requested page_number exceeds document page count."""


class BabelDocBookSource(BookSource):
    """BookSource scanning `pdf_library_roots` and serving BabelDOC dual-column spreads."""

    source_id = "plugin-pdf-babeldoc"
    display_name = "BabelDOC Dual-Column PDF"

    def __init__(
        self,
        get_config_fn: Callable[[], dict[str, Any]],
        get_job_store_fn: Callable[[Path | None], job_store.JobStore],
    ) -> None:
        self._get_config = get_config_fn
        self._get_job_store = get_job_store_fn

    def _config_roots(self) -> tuple[list[Path], Path | None, str]:
        cfg = self._get_config()
        roots = pdf_utils.parse_roots_config(cfg.get("pdf_library_roots"))
        try:
            output_root = pdf_utils.parse_single_root(cfg.get("output_root"))
        except pdf_utils.PathContainmentError:
            output_root = None
        target_lang = str(cfg.get("target_language") or "zh").strip() or "zh"
        return roots, output_root, target_lang

    def _scan_library(self) -> list[tuple[Path, pdf_utils.PdfMetadata]]:
        roots, output_root, _ = self._config_roots()
        if not roots:
            return []
        raw_files: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            if not root.is_dir() or root.is_symlink():
                continue
            resolved_root = root.resolve()
            for dirpath, dirnames, filenames in os.walk(resolved_root, followlinks=False):
                cur_dir = Path(dirpath)
                dirnames[:] = sorted(
                    d for d in dirnames if not (cur_dir / d).is_symlink() and not d.startswith(".")
                )
                for fname in sorted(filenames):
                    if not fname.lower().endswith(".pdf"):
                        continue
                    candidate = cur_dir / fname
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    try:
                        resolved = candidate.resolve()
                        resolved.relative_to(resolved_root)
                    except (OSError, ValueError):
                        continue
                    if output_root is not None and self._is_in_any(resolved, [output_root.resolve()]):
                        if pdf_utils.is_generated_artifact_name(resolved):
                            continue
                    if resolved not in seen:
                        seen.add(resolved)
                        raw_files.append(resolved)

        primary = [p for p in raw_files if not pdf_utils.is_generated_artifact_name(p)]
        primary_keys = {(p.parent, p.stem.lower()) for p in primary}
        selected = list(primary)
        for p in raw_files:
            if not pdf_utils.is_generated_artifact_name(p) or p.name.lower().endswith((".mono.pdf", "_mono.pdf")):
                continue
            base_stem = p.name.split(".", 1)[0].removesuffix("_dual").lower()
            if (p.parent, base_stem) not in primary_keys:
                selected.append(p)

        results: list[tuple[Path, pdf_utils.PdfMetadata]] = []
        for pdf_path in selected:
            with contextlib.suppress(OSError, ValueError, pdf_utils.PathContainmentError):
                results.append((pdf_path, pdf_utils.inspect_pdf_metadata(pdf_path)))
        return results

    @staticmethod
    def _is_in_any(path: Path, roots: list[Path]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _resolve_artifacts(
        self,
        book_id: str,
        source_path: Path,
        output_root: Path | None,
        target_lang: str,
        roots: list[Path],
    ) -> tuple[Path | None, Path | None]:
        store = self._get_job_store(output_root)
        allowed = [r.resolve() for r in roots] + ([output_root.resolve()] if output_root is not None else [])
        dual_pdf: Path | None = None
        page_map: Path | None = None

        job = store.find_latest_succeeded_for_book(book_id, source_path)
        if job is not None:
            raw_dual = str(job.get("dual_pdf_path") or "").strip()
            raw_map = str(job.get("page_map_path") or "").strip()
            if raw_dual:
                cand = Path(raw_dual)
                if cand.is_file() and not cand.is_symlink() and self._is_in_any(cand.resolve(), allowed):
                    with contextlib.suppress(OSError, ValueError, pdf_utils.PathContainmentError):
                        pdf_utils.verify_pdf_magic(cand.resolve())
                        dual_pdf = cand.resolve()
            if raw_map:
                cand = Path(raw_map)
                if cand.is_file() and not cand.is_symlink() and self._is_in_any(cand.resolve(), allowed):
                    page_map = cand.resolve()

        search_dirs: list[Path] = []
        if output_root is not None and output_root.is_dir():
            search_dirs.append(output_root.resolve())
        search_dirs.append(source_path.parent)
        for sdir in search_dirs:
            if dual_pdf is None or page_map is None:
                found_dual, found_map = artifacts.discover_babeldoc_outputs(sdir, source_path, target_lang)
                if dual_pdf is None and found_dual is not None:
                    dual_pdf = found_dual
                if page_map is None and found_map is not None:
                    page_map = found_map

        if page_map is None:
            page_map = artifacts.find_sidecar_only(source_path, search_dirs, target_lang)
        return dual_pdf, page_map

    async def list_books(self, format_filter: str = "", query: str = "") -> list[BookItem]:
        """List real PDF files under `pdf_library_roots` with metadata and dual-column readiness."""
        fmt = format_filter.strip().lower()
        if fmt and fmt != "pdf":
            return []
        roots, output_root, target_lang = self._config_roots()
        q = query.strip().lower()
        items: list[BookItem] = []

        for pdf_path, meta in self._scan_library():
            book_id = pdf_utils.make_book_id(pdf_path)
            dual_pdf, page_map = self._resolve_artifacts(book_id, pdf_path, output_root, target_lang, roots)
            bilingual_ready = (dual_pdf is not None) or (page_map is not None)
            item = BookItem(
                id=book_id,
                source_id=self.source_id,
                title=meta.title,
                author=meta.author,
                format="pdf",
                language=f"EN → {target_lang}" if bilingual_ready else "",
                file_path=str(pdf_path),
                badge="BabelDOC 双栏" if bilingual_ready else "PDF 原版",
                current_page=1,
                total_pages=meta.total_pages,
                bilingual_ready=bilingual_ready,
                extra={
                    "file_size": meta.file_size,
                    "file_size_bytes": meta.file_size,
                    "source_path": str(pdf_path),
                    "dual_pdf_path": str(dual_pdf) if dual_pdf else "",
                    "page_map_path": str(page_map) if page_map else "",
                    "has_sidecar": page_map is not None,
                    "page_count_estimated": meta.page_count_estimated,
                },
            )
            if q and not any(q in field.lower() for field in (item.title, item.author, item.file_path, item.id)):
                continue
            items.append(item)
        return items

    async def fetch_spread(self, book_id: str, page_number: int = 1) -> BookSpread:
        """Fetch bilingual spread from sidecar or dual PDF pairing; never fabricate prose."""
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            raise ValueError(f"page_number must be an integer >= 1, got {page_number!r}")

        roots, output_root, target_lang = self._config_roots()
        matched = next(((p, m) for p, m in self._scan_library() if pdf_utils.make_book_id(p) == book_id), None)
        if matched is None:
            raise FileNotFoundError(f"Book not found in configured pdf_library_roots: {book_id!r}")

        source_path, meta = matched
        dual_pdf, page_map = self._resolve_artifacts(book_id, source_path, output_root, target_lang, roots)

        if dual_pdf is None and page_map is None:
            if page_number > meta.total_pages:
                raise _PageBoundsError(f"page_number ({page_number}) exceeds total_pages ({meta.total_pages})")
            return BookSpread(
                book_id=book_id,
                chapter_title="",
                page_number=page_number,
                total_pages=meta.total_pages,
                blocks=[],
                extra={
                    "status": "needs_bilingual_pdf",
                    "needs_bilingual_pdf": True,
                    "source_path": str(source_path),
                    "message": "尚未生成 BabelDOC 双栏对照 PDF，请先提交翻译任务。",
                },
            )

        sidecar_error = ""
        if page_map is not None:
            try:
                raw_sidecar = json.loads(page_map.read_text(encoding="utf-8"))
                entry, sidecar_total, layout = artifacts.extract_page_entry(raw_sidecar, page_number)
                effective_total = max(meta.total_pages, sidecar_total)
                if page_number > effective_total:
                    raise _PageBoundsError(f"page_number ({page_number}) exceeds total_pages ({effective_total})")
                if entry is not None:
                    raw_blocks = entry.get("blocks")
                    if isinstance(raw_blocks, list):
                        blocks = [
                            artifacts.normalize_block(b, idx, page_number)
                            for idx, b in enumerate(raw_blocks, start=1)
                            if isinstance(b, Mapping)
                        ]
                    elif any(k in entry for k in ("original", "source", "src", "left", "translated", "target", "tgt", "right")):
                        blocks = [artifacts.normalize_block(entry, 1, page_number)]
                    else:
                        blocks = []
                    left_p = int(entry.get("left_page") or entry.get("source_page") or page_number)
                    right_p = int(entry.get("right_page") or entry.get("dual_page") or entry.get("target_page") or page_number)
                    chapter = str(entry.get("chapter_title") or entry.get("chapterTitle") or f"第 {page_number} 页")
                    return BookSpread(
                        book_id=book_id,
                        chapter_title=chapter,
                        page_number=page_number,
                        total_pages=effective_total,
                        blocks=blocks,
                        extra={
                            "status": "ready",
                            "needs_bilingual_pdf": False,
                            "has_sidecar": True,
                            "placeholder": False,
                            "pairing_mode": layout or ("alternating_pages" if left_p != right_p else "side_by_side"),
                            "left_page": left_p,
                            "right_page": right_p,
                            "source_path": str(source_path),
                            "dual_pdf_path": str(dual_pdf) if dual_pdf else "",
                            "page_map_path": str(page_map),
                        },
                    )
            except _PageBoundsError:
                raise
            except (OSError, ValueError, TypeError) as exc:
                sidecar_error = str(exc)

        if dual_pdf is None:
            if page_number > meta.total_pages:
                raise _PageBoundsError(f"page_number ({page_number}) exceeds total_pages ({meta.total_pages})")
            return BookSpread(
                book_id=book_id,
                chapter_title=f"第 {page_number} 页",
                page_number=page_number,
                total_pages=meta.total_pages,
                blocks=[],
                extra={
                    "status": "needs_bilingual_pdf" if sidecar_error else "missing_page_in_sidecar",
                    "needs_bilingual_pdf": bool(sidecar_error),
                    "has_sidecar": not bool(sidecar_error),
                    "sidecar_error": sidecar_error,
                    "page_map_path": str(page_map) if page_map else "",
                },
            )

        dual_meta = pdf_utils.inspect_pdf_metadata(dual_pdf)
        if dual_meta.total_pages == meta.total_pages * 2 and meta.total_pages >= 1:
            pairing_mode = "alternating_pages"
            effective_total = meta.total_pages
            left_page, right_page = 2 * page_number - 1, 2 * page_number
        else:
            pairing_mode = "side_by_side"
            effective_total = max(meta.total_pages, dual_meta.total_pages)
            left_page = right_page = page_number

        if page_number > effective_total:
            raise _PageBoundsError(f"page_number ({page_number}) exceeds total_pages ({effective_total})")

        placeholder = BookPageBlock(
            id=f"{book_id}-p{page_number}-placeholder",
            kind="paragraph",
            original=(
                f"[第 {page_number}/{effective_total} 页原文占位] "
                f"双栏 PDF（{dual_pdf.name}，左页 p.{left_page} / {pairing_mode}）已生成，"
                "缺少页码文本映射 sidecar JSON。"
            ),
            translated=(
                f"[第 {page_number}/{effective_total} 页译文占位] "
                f"缺少 BabelDOC 文本映射 sidecar JSON，请直接查看双栏 PDF（右页 p.{right_page}），"
                "系统不会编造译文内容。"
            ),
        )
        return BookSpread(
            book_id=book_id,
            chapter_title=f"第 {page_number} 页 · 双栏 PDF 页码配对",
            page_number=page_number,
            total_pages=effective_total,
            blocks=[placeholder],
            extra={
                "status": "ready_placeholder",
                "needs_bilingual_pdf": False,
                "has_sidecar": False,
                "placeholder": True,
                "pairing_mode": pairing_mode,
                "left_page": left_page,
                "right_page": right_page,
                "dual_total_pages": dual_meta.total_pages,
                "source_path": str(source_path),
                "dual_pdf_path": str(dual_pdf),
                "page_map_path": "",
                "sidecar_error": sidecar_error,
            },
        )

    async def list_vocab(self) -> list[VocabCard]:
        """Return associated vocabulary cards (empty until glossary cards are persisted)."""
        return []
