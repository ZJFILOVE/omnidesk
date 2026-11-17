"""SRT parsing/merging, ffprobe parsing, atomic writes, path safety, and log redaction utilities."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import redact_secrets

_SENSEVOICE_TAG_RE = re.compile(r"<\|[^|\n]*\|>")
_SRT_TIME_RE = re.compile(
    r"^\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?$"
)
_URL_USERINFO_RE = re.compile(r"(https?://)([^/\s:@]+:[^/\s@]+@)")


class AsrError(Exception):
    """Structured domain error mapped cleanly to CapabilityResult.fail."""

    def __init__(self, error_code: str, message: str, *, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.data = dict(data) if data else {}


def scrub_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Redact credentials, tokens, and URL userinfo from diagnostics/logs."""
    if not text:
        return ""
    cleaned = redact_secrets(text, extra_secrets)
    return _URL_USERINFO_RE.sub(r"\1[REDACTED]@", cleaned)


def atomic_write_text(target: Path, content: str, *, mode: int = 0o644) -> None:
    """Atomically write UTF-8 text via sibling temporary file, fsync, and os.replace."""
    if target.is_symlink():
        raise AsrError("path_safety", f"拒绝覆盖符号链接目标路径: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp_path = Path(handle.name)
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_path, target)
    except BaseException:
        try:
            if not handle.closed:
                handle.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(target: Path, payload: Mapping[str, Any], *, mode: int = 0o600) -> None:
    """Atomically serialize JSON mapping to disk."""
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(target, serialized, mode=mode)


def _check_no_symlink_components(root: Path, target: Path) -> None:
    """Verify no path segment between `root` and `target` is a symlink."""
    if root.is_symlink():
        raise AsrError("path_safety", f"输出根目录不能为符号链接: {root}")
    rel = target.relative_to(root)
    cur = root
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise AsrError("path_safety", f"输出路径不能包含符号链接: {cur}")


def resolve_output_srt_path(
    *,
    media_file: Path | None,
    output_srt_path: Any,
    output_root: str,
) -> Path | None:
    """Resolve and strictly confine output SRT path inside `output_root` or media directory."""
    raw_out = str(output_srt_path).strip() if output_srt_path is not None else ""
    if output_srt_path is not None and not isinstance(output_srt_path, str):
        raise AsrError("invalid_argument", "output_srt_path 必须为字符串")
    if "\x00" in raw_out:
        raise AsrError("path_safety", "output_srt_path 包含非法空字节")

    has_root = bool(output_root.strip())
    if has_root:
        root_raw = Path(output_root.strip()).expanduser()
        if root_raw.is_symlink():
            raise AsrError("path_safety", f"output_root 不能为符号链接: {root_raw}")
        allowed_root = root_raw.resolve()
    elif media_file is not None:
        allowed_root = media_file.parent.resolve()
    else:
        if raw_out:
            raise AsrError("path_safety", "未配置 output_root 且无媒体目录时禁止写入 output_srt_path")
        return None

    if not raw_out:
        if media_file is None:
            return None
        if has_root and not media_file.parent.resolve().is_relative_to(allowed_root):
            candidate = allowed_root / f"{media_file.stem}.srt"
        else:
            candidate = media_file.with_suffix(".srt")
        _check_no_symlink_components(allowed_root, candidate)
        return candidate

    raw_path = Path(raw_out).expanduser()
    if ".." in raw_path.parts:
        raise AsrError("path_safety", "output_srt_path 禁止包含 '..' 路径穿越")

    candidate = raw_path if raw_path.is_absolute() else (allowed_root / raw_path)
    resolved = candidate.resolve()
    if resolved == allowed_root or not resolved.is_relative_to(allowed_root):
        raise AsrError("path_safety", f"输出路径逃逸允许目录: {raw_out}")
    if resolved.exists() and resolved.is_dir():
        raise AsrError("invalid_argument", f"output_srt_path 不能指向目录: {resolved}")
    if raw_path.is_absolute():
        if raw_path.is_symlink() or raw_path.parent.is_symlink():
            raise AsrError("path_safety", f"output_srt_path 不能为符号链接: {raw_path}")
        if raw_path.is_relative_to(allowed_root):
            _check_no_symlink_components(allowed_root, raw_path)
    else:
        _check_no_symlink_components(allowed_root, candidate)
    if candidate.is_symlink() or resolved.is_symlink():
        raise AsrError("path_safety", f"output_srt_path 不能为符号链接: {candidate}")
    return resolved


def parse_srt_timestamp_ms(value: str) -> int:
    """Parse `HH:MM:SS,mmm` or `HH:MM:SS.mmm` into integer milliseconds."""
    parts = re.split(r"[:,.]", value.strip())
    if len(parts) != 4:
        raise ValueError(f"Invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = (int(p) for p in parts)
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Out-of-range SRT timestamp: {value!r}")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def format_srt_timestamp(ms: int) -> str:
    """Format integer milliseconds into `HH:MM:SS,mmm`."""
    total_sec, millis = divmod(max(0, round(ms)), 1000)
    total_min, seconds = divmod(total_sec, 60)
    hours, minutes = divmod(total_min, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def clean_asr_text(raw: str, *, keep_tags: bool) -> str:
    """Strip SenseVoice special tokens (unless keep_tags=True) and blank lines."""
    text = raw if keep_tags else _SENSEVOICE_TAG_RE.sub("", raw)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines).strip()


def parse_chunk_asr_output(
    stdout: str,
    *,
    chunk_index: int,
    offset_ms: int,
    chunk_duration_ms: int,
    max_end_ms: int | None,
    keep_tags: bool,
) -> list[dict[str, Any]]:
    """Parse llama-funasr-sensevoice `--srt` output and apply chunk timestamp offset."""
    normalized = stdout.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    lines = normalized.splitlines()
    has_srt_arrows = any(_SRT_TIME_RE.match(line) for line in lines)
    if not has_srt_arrows:
        if "-->" in normalized:
            raise ValueError("Malformed SRT timestamp line in ASR output")
        plain = clean_asr_text(normalized, keep_tags=keep_tags)
        if not plain:
            return []
        start_ms = max(0, offset_ms)
        end_ms = offset_ms + max(1, chunk_duration_ms)
        if max_end_ms is not None:
            end_ms = min(end_ms, max_end_ms)
        if end_ms <= start_ms:
            return []
        return [{"chunk_index": chunk_index, "start_ms": start_ms, "end_ms": end_ms, "text": plain}]

    segments: list[dict[str, Any]] = []
    idx = 0
    while idx < len(lines):
        match = _SRT_TIME_RE.match(lines[idx])
        if not match:
            idx += 1
            continue
        raw_start, raw_end = match.groups()
        idx += 1
        text_lines: list[str] = []
        while idx < len(lines) and lines[idx].strip():
            # Stop if a corrupted stream starts the next cue without a blank separator.
            if lines[idx].strip().isdigit() and idx + 1 < len(lines) and _SRT_TIME_RE.match(lines[idx + 1]):
                break
            if _SRT_TIME_RE.match(lines[idx]):
                break
            text_lines.append(lines[idx])
            idx += 1
        start_ms = parse_srt_timestamp_ms(raw_start) + offset_ms
        end_ms = parse_srt_timestamp_ms(raw_end) + offset_ms
        if max_end_ms is not None and offset_ms >= 0:
            end_ms = min(end_ms, max_end_ms)
        start_ms = max(0, start_ms)
        body = clean_asr_text("\n".join(text_lines), keep_tags=keep_tags)
        if not body or end_ms <= start_ms:
            continue
        segments.append({"chunk_index": chunk_index, "start_ms": start_ms, "end_ms": end_ms, "text": body})
    return segments


def merge_segments_to_srt(segments: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
    """Filter empty segments, sort by start time, re-index cues, and compose SRT text."""
    valid: list[dict[str, Any]] = []
    for seg in segments:
        text = clean_asr_text(str(seg.get("text") or ""), keep_tags=True)
        start_ms = seg.get("start_ms")
        end_ms = seg.get("end_ms")
        if (
            not text
            or isinstance(start_ms, bool)
            or isinstance(end_ms, bool)
            or not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or end_ms <= start_ms
        ):
            continue
        valid.append(
            {
                "chunk_index": int(seg.get("chunk_index", 0)),
                "start_ms": max(0, start_ms),
                "end_ms": end_ms,
                "text": text,
            }
        )

    valid.sort(key=lambda item: (item["start_ms"], item["end_ms"], item["chunk_index"]))
    cues: list[dict[str, Any]] = []
    blocks: list[str] = []
    texts: list[str] = []
    for cue_index, seg in enumerate(valid, start=1):
        start_ms, end_ms, text = seg["start_ms"], seg["end_ms"], seg["text"]
        cues.append(
            {
                "index": cue_index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_sec": round(start_ms / 1000.0, 3),
                "end_sec": round(end_ms / 1000.0, 3),
                "text": text,
            }
        )
        blocks.append(f"{cue_index}\n{format_srt_timestamp(start_ms)} --> {format_srt_timestamp(end_ms)}\n{text}\n\n")
        texts.append(text)
    return cues, "".join(blocks), "\n".join(texts)


def _to_pos_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _to_pos_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def parse_ffprobe_output(stdout: str, *, require_duration: bool = True) -> dict[str, Any]:
    """Parse ffprobe JSON output into normalized duration and typed stream descriptors."""
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AsrError("invalid_argument", f"ffprobe 输出无法解析为 JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise AsrError("invalid_argument", "ffprobe 输出格式无效")

    fmt = raw.get("format") if isinstance(raw.get("format"), Mapping) else {}
    raw_streams = raw.get("streams")
    if not isinstance(raw_streams, list):
        raise AsrError("invalid_argument", "ffprobe 输出缺少 streams 字段")

    streams: list[dict[str, Any]] = []
    audio_streams: list[dict[str, Any]] = []
    video_streams: list[dict[str, Any]] = []
    subtitle_streams: list[dict[str, Any]] = []
    fallback_duration: float | None = None

    for pos, item in enumerate(raw_streams):
        if not isinstance(item, Mapping):
            continue
        tags = item.get("tags") if isinstance(item.get("tags"), Mapping) else {}
        codec_type = str(item.get("codec_type") or "").strip().lower()
        stream_idx = item.get("index", pos)
        stream_index = int(stream_idx) if isinstance(stream_idx, int) and not isinstance(stream_idx, bool) else pos
        s_dur = _to_pos_float(item.get("duration"))
        if fallback_duration is None and s_dur is not None:
            fallback_duration = s_dur

        entry: dict[str, Any] = {
            "index": stream_index,
            "stream_index": stream_index,
            "codec_type": codec_type,
            "codec_name": str(item.get("codec_name") or ""),
            "sample_rate": _to_pos_int(item.get("sample_rate")),
            "channels": _to_pos_int(item.get("channels")),
            "channel_layout": str(item.get("channel_layout") or ""),
            "bit_rate": _to_pos_int(item.get("bit_rate")),
            "language": str(tags.get("language") or "und"),
            "title": str(tags.get("title") or ""),
        }
        if codec_type == "audio":
            entry["audio_index"] = len(audio_streams)
            audio_streams.append(entry)
        elif codec_type == "video":
            video_streams.append(entry)
        elif codec_type == "subtitle":
            subtitle_streams.append(entry)
        streams.append(entry)

    duration_sec = _to_pos_float(fmt.get("duration")) or fallback_duration
    if require_duration and duration_sec is None:
        raise AsrError("invalid_argument", "无法确定有效的媒体时长")

    primary_audio = audio_streams[0] if audio_streams else None
    return {
        "duration_sec": round(duration_sec, 6) if duration_sec is not None else 0.0,
        "format_name": str(fmt.get("format_name") or ""),
        "size_bytes": _to_pos_int(fmt.get("size")),
        "bit_rate": _to_pos_int(fmt.get("bit_rate")),
        "streams": streams,
        "audio_streams": audio_streams,
        "video_streams": video_streams,
        "subtitle_streams": subtitle_streams,
        "audio_stream_count": len(audio_streams),
        "has_audio": bool(audio_streams),
        "sample_rate": primary_audio["sample_rate"] if primary_audio else None,
        "channels": primary_audio["channels"] if primary_audio else None,
        "codec_name": primary_audio["codec_name"] if primary_audio else "",
    }
