"""Glossary preparation, sidecar page-map parsing, and BabelDOC output discovery."""

from __future__ import annotations

import contextlib
import csv
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omnidesk_sdk import BookPageBlock

if __package__:
    from . import job_store, pdf_utils
else:
    import job_store  # type: ignore[import-not-found,no-redef]
    import pdf_utils  # type: ignore[import-not-found,no-redef]

_VALID_BLOCK_KINDS = frozenset({"heading", "paragraph", "caption", "formula", "image"})


class UnsupportedOptionError(ValueError):
    """Raised when a caller supplies an option or glossary format unsupported by BabelDOC."""


def prepare_glossary_csv(
    glossary: Any,
    *,
    inputs_dir: Path,
    job_id: str,
    target_language: str,
) -> Path | None:
    """Validate and materialize glossary terms into a BabelDOC-compatible 0600 CSV file."""
    if glossary is None or glossary == "" or glossary == [] or glossary == {}:
        return None

    rows: list[tuple[str, str, str]] = []
    if isinstance(glossary, str):
        raw = glossary.strip()
        if "\x00" in raw or ".." in Path(raw).parts:
            raise pdf_utils.PathContainmentError(f"Unsafe glossary path: {glossary!r}")
        gpath = Path(raw).expanduser()
        if gpath.is_symlink():
            raise pdf_utils.PathContainmentError(f"Refusing symlinked glossary file: {gpath}")
        if not gpath.is_file():
            raise FileNotFoundError(f"Glossary file not found: {gpath}")
        suffix = gpath.suffix.lower()
        if suffix == ".csv":
            text = gpath.read_text(encoding="utf-8")
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames or not {"source", "target"}.issubset(set(reader.fieldnames)):
                raise UnsupportedOptionError("Glossary CSV must contain 'source' and 'target' header columns")
            for item in reader:
                src = str(item.get("source") or "").strip()
                tgt = str(item.get("target") or "").strip()
                lng = str(item.get("tgt_lng") or "").strip()
                if src and tgt:
                    rows.append((src, tgt, lng or target_language))
        elif suffix == ".json":
            parsed = json.loads(gpath.read_text(encoding="utf-8"))
            return prepare_glossary_csv(parsed, inputs_dir=inputs_dir, job_id=job_id, target_language=target_language)
        else:
            raise UnsupportedOptionError(f"Unsupported glossary file extension {suffix!r}; expected .csv or .json")
    elif isinstance(glossary, Mapping):
        for src, tgt in glossary.items():
            if not isinstance(src, str) or not isinstance(tgt, str) or not src.strip() or not tgt.strip():
                raise UnsupportedOptionError("Glossary mapping entries must be non-empty string pairs")
            rows.append((src.strip(), tgt.strip(), target_language))
    elif isinstance(glossary, Sequence):
        for entry in glossary:
            if isinstance(entry, Mapping):
                src = str(entry.get("source") or "").strip()
                tgt = str(entry.get("target") or "").strip()
                lng = str(entry.get("tgt_lng") or entry.get("target_language") or target_language).strip()
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                src, tgt, lng = str(entry[0]).strip(), str(entry[1]).strip(), target_language
            else:
                raise UnsupportedOptionError("Unsupported glossary sequence item format")
            if not src or not tgt:
                raise UnsupportedOptionError("Glossary entries require non-empty source and target")
            rows.append((src, tgt, lng))
    else:
        raise UnsupportedOptionError(f"Unsupported glossary type: {type(glossary).__name__}")

    if not rows:
        raise UnsupportedOptionError("Glossary must contain at least one valid term pair")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["source", "target", "tgt_lng"])
    writer.writerows(rows)
    target = inputs_dir / f"{pdf_utils.validate_safe_identifier(job_id, 'job_id')}.glossary.csv"
    job_store.atomic_write_text_0600(target, buf.getvalue())
    return target


def snapshot_directory_mtimes(directory: Path) -> dict[Path, int]:
    """Record current regular file mtimes inside `directory` prior to running BabelDOC."""
    if not directory.is_dir():
        return {}
    snap: dict[Path, int] = {}
    for entry in directory.rglob("*"):
        if entry.is_symlink() or not entry.is_file():
            continue
        with contextlib.suppress(OSError):
            snap[entry.resolve()] = entry.stat().st_mtime_ns
    return snap


def discover_babeldoc_outputs(
    output_dir: Path,
    source_path: Path,
    target_language: str,
    *,
    before_snapshot: Mapping[Path, int] | None = None,
) -> tuple[Path | None, Path | None]:
    """Locate and validate generated dual PDF and optional sidecar JSON inside `output_dir`."""
    resolved_out = output_dir.resolve()
    resolved_src = source_path.resolve()
    stem = source_path.stem
    stem_lower = stem.lower()

    pdf_candidates: list[Path] = []
    preferred_names = [
        f"{stem}.{target_language}.dual.pdf",
        f"{stem}.no_watermark.{target_language}.dual.pdf",
        f"{stem}.dual.pdf",
        f"{stem}_dual.pdf",
    ]
    for name in preferred_names:
        cand = resolved_out / name
        if cand.is_file() and not cand.is_symlink():
            pdf_candidates.append(cand.resolve())

    for entry in sorted(resolved_out.rglob("*.pdf")):
        if entry.is_symlink() or not entry.is_file():
            continue
        resolved = entry.resolve()
        if resolved == resolved_src or resolved in pdf_candidates:
            continue
        if not resolved.name.lower().startswith(stem_lower):
            continue
        if pdf_utils.is_generated_artifact_name(resolved) and not resolved.name.lower().endswith((".mono.pdf", "_mono.pdf")):
            pdf_candidates.append(resolved)

    if before_snapshot is not None:
        fresh: list[Path] = []
        stale: list[Path] = []
        for cand in pdf_candidates:
            mtime = cand.stat().st_mtime_ns if cand.exists() else 0
            (fresh if (cand not in before_snapshot or mtime > before_snapshot[cand]) else stale).append(cand)
        pdf_candidates = fresh + stale

    dual_pdf: Path | None = None
    for cand in pdf_candidates:
        try:
            cand.relative_to(resolved_out)
            pdf_utils.verify_pdf_magic(cand)
        except (ValueError, OSError, pdf_utils.PathContainmentError):
            continue
        dual_pdf = cand
        break

    sidecar_names = [
        f"{dual_pdf.stem}.page_map.json" if dual_pdf else "",
        f"{dual_pdf.name}.page_map.json" if dual_pdf else "",
        f"{dual_pdf.stem}.json" if dual_pdf else "",
        f"{stem}.{target_language}.page_map.json",
        f"{stem}.{target_language}.dual.json",
        f"{stem}.page_map.json",
        f"{stem}.dual.json",
        f"{stem}.pages.json",
        f"{stem}.babeldoc.json",
        f"{stem}.sidecar.json",
        "page_map.json",
        "mapping.json",
    ]
    page_map: Path | None = None
    search_dirs = [dual_pdf.parent, resolved_out] if dual_pdf is not None else [resolved_out]
    for sdir in search_dirs:
        for sname in sidecar_names:
            if not sname:
                continue
            cand = sdir / sname
            if cand.is_symlink() or not cand.is_file():
                continue
            try:
                resolved_sidecar = cand.resolve()
                resolved_sidecar.relative_to(resolved_out)
                parsed = json.loads(resolved_sidecar.read_text(encoding="utf-8"))
                if isinstance(parsed, (dict, list)):
                    page_map = resolved_sidecar
                    break
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        if page_map is not None:
            break

    return dual_pdf, page_map


def find_sidecar_only(source_path: Path, search_dirs: list[Path], target_language: str) -> Path | None:
    """Search allowed directories for standalone BabelDOC page-map sidecar JSON files."""
    stem = source_path.stem
    names = [
        f"{stem}.{target_language}.page_map.json",
        f"{stem}.{target_language}.dual.json",
        f"{stem}.page_map.json",
        f"{stem}.dual.json",
        f"{stem}.pages.json",
        f"{stem}.babeldoc.json",
        f"{stem}.sidecar.json",
    ]
    for directory in search_dirs:
        if not directory.is_dir() or directory.is_symlink():
            continue
        resolved_dir = directory.resolve()
        for name in names:
            cand = resolved_dir / name
            if cand.is_symlink() or not cand.is_file():
                continue
            try:
                resolved = cand.resolve()
                resolved.relative_to(resolved_dir)
                parsed = json.loads(resolved.read_text(encoding="utf-8"))
                if isinstance(parsed, (dict, list)):
                    return resolved
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    return None


def normalize_block(item: Mapping[str, Any], index: int, page_number: int) -> BookPageBlock:
    """Normalize a sidecar block dictionary into a `BookPageBlock` model."""
    bid = str(item.get("id") or f"p{page_number}-b{index}")
    raw_kind = str(item.get("kind") or "paragraph").strip().lower()
    kind = raw_kind if raw_kind in _VALID_BLOCK_KINDS else "paragraph"
    original = ""
    for key in ("original", "source", "src", "left", "en", "text"):
        if item.get(key) is not None:
            original = str(item[key])
            break
    translated = ""
    for key in ("translated", "target", "tgt", "right", "zh", "translation"):
        if item.get(key) is not None:
            translated = str(item[key])
            break
    raw_terms = (
        item.get("highlighted_words")
        or item.get("highlightedWords")
        or item.get("keyTerms")
        or item.get("terms")
        or []
    )
    terms = [str(t).strip() for t in raw_terms if str(t).strip()] if isinstance(raw_terms, list) else []
    return BookPageBlock(
        id=bid,
        kind=kind,  # type: ignore[arg-type]
        original=original,
        translated=translated,
        highlighted_words=terms,
    )


def extract_page_entry(raw_data: Any, page_number: int) -> tuple[Mapping[str, Any] | None, int, str]:
    """Extract `(page_entry, inferred_total_pages, layout_mode)` from a sidecar JSON payload."""
    layout = ""
    total_pages = 0
    if isinstance(raw_data, Mapping):
        layout = str(raw_data.get("layout") or raw_data.get("mode") or "").strip()
        if isinstance(raw_data.get("total_pages"), int) and not isinstance(raw_data.get("total_pages"), bool):
            total_pages = max(0, int(raw_data["total_pages"]))
        pages_obj = raw_data.get("pages")
        if pages_obj is None:
            pages_obj = raw_data.get("page_map")
        if pages_obj is None and ("blocks" in raw_data or "original" in raw_data or "translated" in raw_data):
            if int(raw_data.get("page") or raw_data.get("page_number") or 1) == page_number:
                return raw_data, max(1, total_pages), layout
            return None, max(1, total_pages), layout
    elif isinstance(raw_data, list):
        pages_obj = raw_data
    else:
        return None, 1, ""

    if isinstance(pages_obj, Mapping):
        numeric_keys = [int(k) for k in pages_obj if str(k).isdigit()]
        if numeric_keys:
            total_pages = max(total_pages, max(numeric_keys))
        entry = pages_obj.get(str(page_number))
        if entry is None:
            entry = pages_obj.get(page_number)  # type: ignore[arg-type]
        return (entry if isinstance(entry, Mapping) else None), max(1, total_pages), layout

    if isinstance(pages_obj, list):
        matched: Mapping[str, Any] | None = None
        max_p = total_pages
        for idx, item in enumerate(pages_obj, start=1):
            if not isinstance(item, Mapping):
                continue
            p_val = item.get("page_number") or item.get("page") or item.get("source_page") or idx
            try:
                p_num = int(p_val)
            except (TypeError, ValueError):
                continue
            max_p = max(max_p, p_num)
            if p_num == page_number and matched is None:
                matched = item
        return matched, max(1, max_p, len(pages_obj)), layout

    return None, max(1, total_pages), layout
