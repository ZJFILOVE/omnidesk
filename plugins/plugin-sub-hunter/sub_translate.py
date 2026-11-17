"""Bilingual subtitle translation via `llm.translate.v1`.

Ported from the proven `qwen-translator` pipeline: SRT/VTT parsing, resumable
content-hash cache, ten-minute / 24k-character batches, bounded concurrency
(1-8, default 4), split-and-retry on malformed batch responses, and atomic
write-back with the same bilingual ordering (translation above original).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

try:
    from . import sub_archive
except ImportError:
    import sub_archive  # type: ignore[no-redef]

MAX_SUBTITLE_BYTES = 16 * 1024 * 1024
SUPPORTED_SUFFIXES = (".srt", ".vtt")
CACHE_VERSION = "subtitle-bilingual-v1"
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 8
DEFAULT_OUTPUT_MODE = "bilingual"
OUTPUT_MODES = ("bilingual", "translated", "original")
_MAX_BATCH_CHARS = 24_000
_BATCH_WINDOW_MS = 600_000
_MIN_SPLIT_CUES = 32

_TIMESTAMP_RE = re.compile(r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})")
_CUE_LINE_RE = re.compile(
    r"\s*((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*"
    r"((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})(?:\s+.*)?"
)


class TranslateError(RuntimeError):
    """Classified subtitle translation error."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Cue:
    """One subtitle cue with millisecond bounds and plain text."""

    start_ms: int
    end_ms: int
    text: str


def clamp_concurrency(value: Any, default: int = DEFAULT_CONCURRENCY) -> int:
    """Clamp `translation_concurrency` to the same 1-8 range as qwen-translator."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_CONCURRENCY, parsed))


def normalize_output_mode(value: Any, default: str = DEFAULT_OUTPUT_MODE) -> str:
    """Normalize `subtitle_output_mode` to bilingual / translated / original."""
    mode = str(value or "").strip().lower()
    return mode if mode in OUTPUT_MODES else default


def _parse_timestamp(value: str) -> int:
    match = _TIMESTAMP_RE.fullmatch(value.strip())
    if match is None:
        raise TranslateError("invalid_subtitle", f"字幕时间戳无效: {value!r}")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int(match.group(4))
    if minutes >= 60 or seconds >= 60:
        raise TranslateError("invalid_subtitle", f"字幕时间戳无效: {value!r}")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _clean_text(lines: list[str]) -> str:
    body = html.unescape(" ".join(line.strip() for line in lines if line.strip()))
    body = re.sub(r"<[^>]*>", "", body)
    body = re.sub(r"\{\\[^}]*\}", "", body)
    return " ".join(body.split())


def _normalize(text: str) -> str:
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def parse_srt(text: str) -> list[Cue]:
    """Parse SRT text into sorted cues, skipping malformed blocks."""
    result: list[Cue] = []
    for block in re.split(r"\n\s*\n", _normalize(text).strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        match = _CUE_LINE_RE.fullmatch(lines[0].strip())
        if match is None:
            continue
        start = _parse_timestamp(match.group(1))
        end = _parse_timestamp(match.group(2))
        body = _clean_text(lines[1:])
        if end <= start or not body:
            continue
        result.append(Cue(start, end, body))
    if not result:
        raise TranslateError("invalid_subtitle", "字幕没有有效文本")
    return sorted(result, key=lambda cue: cue.start_ms)


def parse_vtt(text: str) -> list[Cue]:
    """Parse WebVTT text into sorted cues, ignoring NOTE/STYLE/REGION blocks."""
    normalized = _normalize(text)
    normalized = re.sub(r"^WEBVTT[^\n]*\n", "", normalized, count=1)
    result: list[Cue] = []
    for block in re.split(r"\n\s*\n", normalized.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        cue_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if cue_index is None:
            continue
        match = _CUE_LINE_RE.fullmatch(lines[cue_index].strip())
        if match is None:
            continue
        start = _parse_timestamp(match.group(1))
        end = _parse_timestamp(match.group(2))
        body = _clean_text(lines[cue_index + 1 :])
        if end <= start or not body:
            continue
        result.append(Cue(start, end, body))
    if not result:
        raise TranslateError("invalid_subtitle", "字幕没有有效文本")
    return sorted(result, key=lambda cue: cue.start_ms)


def parse_subtitle(text: str, suffix: str) -> list[Cue]:
    """Dispatch parsing by subtitle suffix."""
    lowered = suffix.lower()
    if lowered == ".srt":
        return parse_srt(text)
    if lowered == ".vtt":
        return parse_vtt(text)
    raise TranslateError(
        "unsupported_format",
        f"目前仅支持 .srt / .vtt 字幕翻译，收到 {suffix or '(unknown)'}；请先转换为 SRT",
    )


def _timestamp(ms: int, separator: str) -> str:
    seconds, millis = divmod(max(0, round(ms)), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{millis:03}"


def compose_srt(cues: list[Cue]) -> str:
    """Compose cues as UTF-8 SRT."""
    return "".join(
        f"{index}\n{_timestamp(cue.start_ms, ',')} --> {_timestamp(cue.end_ms, ',')}\n{cue.text}\n\n"
        for index, cue in enumerate(cues, 1)
    )


def compose_vtt(cues: list[Cue]) -> str:
    """Compose cues as WebVTT."""
    body = "".join(
        f"{_timestamp(cue.start_ms, '.')} --> {_timestamp(cue.end_ms, '.')}\n{cue.text}\n\n"
        for cue in cues
    )
    return "WEBVTT\n\n" + body


def compose_subtitle(cues: list[Cue], suffix: str) -> str:
    """Dispatch composition by subtitle suffix."""
    if suffix.lower() == ".vtt":
        return compose_vtt(cues)
    return compose_srt(cues)


def translation_batches(cues: list[Cue], missing: list[int]) -> list[list[int]]:
    """Group by ten-minute media windows with a text-size guard for dense captions."""
    batches: list[list[int]] = []
    batch: list[int] = []
    size = 0
    window: int | None = None
    for index in missing:
        cue_window = cues[index].start_ms // _BATCH_WINDOW_MS
        length = len(cues[index].text)
        if batch and (cue_window != window or size + length > _MAX_BATCH_CHARS):
            batches.append(batch)
            batch = []
            size = 0
        batch.append(index)
        size += length
        window = cue_window
    if batch:
        batches.append(batch)
    return batches


def _split_to_fill_concurrency(batches: list[list[int]], concurrency: int) -> list[list[int]]:
    """Split the largest batches until the requested worker count can be used."""
    while batches and len(batches) < concurrency:
        index = max(range(len(batches)), key=lambda i: len(batches[i]))
        if len(batches[index]) < _MIN_SPLIT_CUES:
            break
        middle = len(batches[index]) // 2
        batches[index : index + 1] = [batches[index][:middle], batches[index][middle:]]
    return batches


def _translation_values(data: Mapping[str, Any], expected: int) -> list[str]:
    raw: Any = data.get("translations")
    if raw is None:
        results = data.get("results")
        if isinstance(results, list):
            raw = [
                item.get("translation") if isinstance(item, Mapping) else item
                for item in results
            ]
    if raw is None and expected == 1:
        raw = [data.get("translation") or data.get("reply")]
    if not isinstance(raw, list) or len(raw) != expected:
        raise ValueError("批量翻译条数不匹配")
    values: list[str] = []
    for item in raw:
        value = item.get("translation") if isinstance(item, Mapping) else item
        if not isinstance(value, str) or not value.strip():
            raise ValueError("翻译结果为空")
        values.append(" ".join(value.split()))
    return values


async def _call_translate(
    ctx: Any,
    texts: list[str],
    *,
    target_lang: str,
    timeout_sec: float,
) -> list[str]:
    if ctx is None or not ctx.capabilities.has("llm.translate.v1"):
        raise TranslateError(
            "capability_unavailable",
            "llm.translate.v1 不可用；请先启用 plugin-magpie-agent 并配置翻译路由",
        )
    try:
        result = await asyncio.wait_for(
            ctx.capabilities.call(
                "llm.translate.v1",
                {
                    "action": "translate_batch",
                    "domain": "media",
                    "target_lang": target_lang,
                    "texts": texts,
                    "preserve_formatting": True,
                },
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError as exc:
        raise TranslateError("timeout", f"llm.translate.v1 超时（{timeout_sec:.0f}s）") from exc
    if not result.ok:
        code = str((result.data or {}).get("error_code") or "translation_failed")
        message = result.error or "llm.translate.v1 调用失败"
        raise TranslateError("translation_failed", f"{code}: {message}")
    try:
        return _translation_values(dict(result.data or {}), len(texts))
    except ValueError as exc:
        raise TranslateError("invalid_response", str(exc)) from exc


def _cache_key(cues: list[Cue], target_lang: str) -> str:
    payload = [CACHE_VERSION, target_lang, [cue.text for cue in cues]]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _cache_path(data_dir: Path, key: str) -> Path:
    return data_dir / "translation-cache" / f"translation-{key}.json"


def _load_cache(path: Path) -> dict[int, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[int, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and key.isdigit() and isinstance(value, str) and value.strip():
            result[int(key)] = value
    return result


def _save_cache(path: Path, translated: Mapping[int, str]) -> None:
    payload = {str(key): value for key, value in sorted(translated.items())}
    sub_archive.atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False),
        mode=0o600,
    )


def _resolve_output_path(
    subtitle_path: Path,
    output_path: str | None,
    *,
    target_lang: str,
    bilingual: bool,
) -> Path:
    if isinstance(output_path, str) and output_path.strip():
        return sub_archive.validate_safe_fs_path(output_path.strip(), allow_nonexistent=True)
    tag = "bilingual" if bilingual else target_lang.split("-")[0].lower()
    return subtitle_path.with_name(f"{subtitle_path.stem}.{tag}{subtitle_path.suffix.lower()}")


def translation_status(
    ctx: Any,
    data_dir: Path,
    concurrency: int,
    output_mode: str = DEFAULT_OUTPUT_MODE,
) -> dict[str, Any]:
    """Return cheap, non-blocking translation readiness diagnostics."""
    return {
        "llm_translate_available": bool(
            ctx is not None and ctx.capabilities.has("llm.translate.v1")
        ),
        "translation_concurrency": clamp_concurrency(concurrency),
        "output_mode": normalize_output_mode(output_mode),
        "output_modes": list(OUTPUT_MODES),
        "supported_formats": list(SUPPORTED_SUFFIXES),
        "cache_dir": str(data_dir / "translation-cache"),
    }


async def translate_subtitle_file(
    ctx: Any,
    *,
    subtitle_path: str,
    output_path: str | None,
    target_lang: str,
    output_mode: str,
    overwrite_policy: str,
    data_dir: Path,
    concurrency: int,
    timeout_sec: float,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Translate a local SRT/VTT and atomically write a bilingual or target-only file."""
    try:
        path = sub_archive.validate_safe_fs_path(subtitle_path, allow_nonexistent=False)
    except sub_archive.ArchiveSecurityError as exc:
        raise TranslateError(exc.error_code, str(exc)) from exc
    if not path.is_file():
        raise TranslateError("not_found", f"字幕文件不存在: {path}")
    if path.stat().st_size > MAX_SUBTITLE_BYTES:
        raise TranslateError("validation_error", "字幕文件超过 16 MB 上限")

    suffix = path.suffix.lower()
    try:
        decoded, source_encoding = sub_archive.decode_subtitle_bytes(path.read_bytes())
    except sub_archive.ArchiveSecurityError as exc:
        raise TranslateError(exc.error_code, str(exc)) from exc
    cues = parse_subtitle(decoded, suffix)

    mode = normalize_output_mode(output_mode)
    if mode == "original":
        return {
            "subtitle_path": str(path),
            "output_path": str(path),
            "skipped": True,
            "output_mode": mode,
            "cue_count": len(cues),
            "translated_count": 0,
            "concurrency": clamp_concurrency(concurrency),
        }
    bilingual = mode == "bilingual"

    try:
        target = _resolve_output_path(
            path,
            output_path,
            target_lang=target_lang,
            bilingual=bilingual,
        )
        overwrite_action = sub_archive.check_overwrite_allowed(
            target, data_dir, overwrite_policy
        )
    except sub_archive.ArchiveSecurityError as exc:
        raise TranslateError(exc.error_code, str(exc)) from exc
    if overwrite_action == "skip":
        return {
            "subtitle_path": str(path),
            "output_path": str(target),
            "skipped": True,
            "cue_count": len(cues),
            "translated_count": 0,
            "concurrency": clamp_concurrency(concurrency),
        }

    workers = clamp_concurrency(concurrency)
    deadline = max(10.0, min(float(timeout_sec), 1800.0))
    key = _cache_key(cues, target_lang)
    cache_path = _cache_path(data_dir, key)
    translated: dict[int, str] = _load_cache(cache_path) if use_cache else {}
    translated = {index: value for index, value in translated.items() if 0 <= index < len(cues)}
    cached_count = len(translated)

    missing = [index for index in range(len(cues)) if index not in translated]
    batches = _split_to_fill_concurrency(translation_batches(cues, missing), workers) if missing else []
    semaphore = asyncio.Semaphore(workers)
    called_batches = 0

    async def translate_batch(ids: list[int]) -> None:
        nonlocal called_batches
        try:
            async with semaphore:
                values = await _call_translate(
                    ctx,
                    [cues[index].text for index in ids],
                    target_lang=target_lang,
                    timeout_sec=deadline,
                )
        except TranslateError as exc:
            if exc.error_code == "capability_unavailable" or len(ids) == 1:
                raise
            middle = len(ids) // 2
            await asyncio.gather(
                translate_batch(ids[:middle]),
                translate_batch(ids[middle:]),
            )
            return
        called_batches += 1
        for index, value in zip(ids, values, strict=True):
            translated[index] = value
        if use_cache:
            _save_cache(cache_path, translated)

    tasks = [asyncio.create_task(translate_batch(ids)) for ids in batches]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if len(translated) != len(cues):
        raise TranslateError("translation_failed", "部分字幕段没有返回译文，已保留原文")

    output_cues = [
        replace(cue, text=f"{translated[index]}\n{cue.text}" if bilingual else translated[index])
        for index, cue in enumerate(cues)
    ]
    content = compose_subtitle(output_cues, suffix)
    try:
        sub_archive.atomic_write_text(target, content)
        sub_archive.record_managed_subtitle(data_dir, target, content)
    except sub_archive.ArchiveSecurityError as exc:
        raise TranslateError(exc.error_code, str(exc)) from exc

    return {
        "subtitle_path": str(path),
        "output_path": str(target),
        "skipped": False,
        "cue_count": len(cues),
        "translated_count": len(translated),
        "cached_count": cached_count,
        "called_batches": called_batches,
        "concurrency": workers,
        "output_mode": mode,
        "target_lang": target_lang,
        "bilingual": bilingual,
        "source_encoding": source_encoding,
        "output_encoding": "utf-8",
        "format": suffix,
    }
