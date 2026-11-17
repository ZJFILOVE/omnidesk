"""Subtitle linear offset/scale correction and ffsubsync VAD subprocess synchronization."""

from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

try:
    from .sub_archive import (
        HARD_MAX_DOWNLOAD_BYTES,
        ArchiveSecurityError,
        atomic_write_text,
        check_overwrite_allowed,
        decode_subtitle_bytes,
        record_managed_subtitle,
        validate_safe_fs_path,
    )
except ImportError:
    from sub_archive import (
        HARD_MAX_DOWNLOAD_BYTES,
        ArchiveSecurityError,
        atomic_write_text,
        check_overwrite_allowed,
        decode_subtitle_bytes,
        record_managed_subtitle,
        validate_safe_fs_path,
    )

_SRT_ARROW_RE = re.compile(
    r"^\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{3})([ \t]+.*)?$"
)
_VTT_ARROW_RE = re.compile(
    r"^\s*((?:\d{1,3}:)?\d{2}:\d{2}\.\d{3})\s*-->\s*((?:\d{1,3}:)?\d{2}:\d{2}\.\d{3})([ \t]+.*)?$"
)
_ASS_TIME_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d{1,3})$")
_UNSAFE_BIN_CHARS = frozenset(";&|$`\n\r\x00")


class SyncError(RuntimeError):
    """Classified subtitle sync/shift error."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


def resolve_ffsubsync_bin(ffsubsync_bin: str) -> str | None:
    """Resolve ffsubsync binary without shell evaluation or path traversal."""
    cleaned = (ffsubsync_bin or "").strip()
    if not cleaned or any(ch in cleaned for ch in _UNSAFE_BIN_CHARS):
        return None
    if ".." in Path(cleaned).parts:
        return None
    if "/" in cleaned or "\\" in cleaned:
        candidate = Path(cleaned).expanduser()
        return str(candidate.resolve()) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return shutil.which(cleaned)


def _parse_ms(ts: str) -> int:
    parts = ts.strip().replace(",", ".").split(":")
    if len(parts) == 2:
        h, m_str, s_ms = 0, parts[0], parts[1]
    elif len(parts) == 3:
        h, m_str, s_ms = int(parts[0]), parts[1], parts[2]
    else:
        raise SyncError("validation_error", f"无效字幕时间戳: {ts!r}")
    s_str, ms_str = s_ms.split(".")
    m, s, ms = int(m_str), int(s_str), int(ms_str)
    if m >= 60 or s >= 60:
        raise SyncError("validation_error", f"字幕时间戳超出范围: {ts!r}")
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def _format_srt_ms(ms: int, sep: str = ",") -> str:
    s, rem_ms = divmod(max(0, ms), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{rem_ms:03d}"


def _parse_ass_ms(ts: str) -> int:
    m = _ASS_TIME_RE.match(ts.strip())
    if not m:
        raise SyncError("validation_error", f"无效 ASS 时间戳: {ts!r}")
    h, mn, s, frac = m.groups()
    ms = int(frac.ljust(3, "0")[:3])
    return ((int(h) * 60 + int(mn)) * 60 + int(s)) * 1000 + ms


def _format_ass_ms(ms: int) -> str:
    cs_total = max(0, round(ms / 10.0))
    s, cs = divmod(cs_total, 100)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _transform_bounds(start_ms: int, end_ms: int, offset_ms: float, scale: float) -> tuple[int, int] | None:
    new_start = round(start_ms * scale + offset_ms)
    new_end = round(end_ms * scale + offset_ms)
    if new_end <= 0:
        return None
    clamped_start = max(0, new_start)
    clamped_end = max(clamped_start + 1, new_end)
    return clamped_start, clamped_end


def _shift_srt(text: str, offset_ms: float, scale: float) -> tuple[str, int]:
    norm = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not norm:
        raise SyncError("validation_error", "SRT 字幕内容为空")
    blocks = re.split(r"\n[ \t]*\n", norm)
    out_blocks: list[str] = []
    for block in blocks:
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        m = _SRT_ARROW_RE.match(lines[0])
        if not m:
            raise SyncError("validation_error", f"SRT 时间轴格式无效: {lines[0]!r}")
        start_ms = _parse_ms(m.group(1))
        end_ms = _parse_ms(m.group(2))
        suffix = m.group(3) or ""
        transformed = _transform_bounds(start_ms, end_ms, offset_ms, scale)
        if transformed is None:
            continue
        n_start, n_end = transformed
        body = "\n".join(lines[1:])
        idx = len(out_blocks) + 1
        out_blocks.append(f"{idx}\n{_format_srt_ms(n_start)} --> {_format_srt_ms(n_end)}{suffix}\n{body}")
    if not out_blocks:
        raise SyncError("invalid_argument", "线性校正后没有任何有效字幕片段保留")
    return "\n\n".join(out_blocks) + "\n", len(out_blocks)


def _shift_vtt(text: str, offset_ms: float, scale: float) -> tuple[str, int]:
    norm = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = norm.splitlines()
    out_lines: list[str] = []
    cue_count = 0
    for line in lines:
        m = _VTT_ARROW_RE.match(line)
        if m:
            start_ms = _parse_ms(m.group(1))
            end_ms = _parse_ms(m.group(2))
            suffix = m.group(3) or ""
            transformed = _transform_bounds(start_ms, end_ms, offset_ms, scale)
            if transformed is not None:
                n_start, n_end = transformed
                cue_count += 1
                out_lines.append(f"{_format_srt_ms(n_start, '.')} --> {_format_srt_ms(n_end, '.')}{suffix}")
                continue
        out_lines.append(line)
    if cue_count == 0:
        raise SyncError("invalid_argument", "VTT 字幕未包含有效时间轴片段")
    return "\n".join(out_lines).rstrip("\n") + "\n", cue_count


def _shift_ass(text: str, offset_ms: float, scale: float) -> tuple[str, int]:
    norm = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    in_events = False
    start_idx, end_idx, total_cols = 1, 2, 10
    cue_count = 0
    out_lines: list[str] = []
    for line in norm.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
            out_lines.append(line)
            continue
        if in_events and stripped.lower().startswith("format:"):
            cols = [c.strip().lower() for c in stripped.split(":", 1)[1].split(",")]
            total_cols = len(cols)
            if "start" in cols:
                start_idx = cols.index("start")
            if "end" in cols:
                end_idx = cols.index("end")
            out_lines.append(line)
            continue
        if in_events and (stripped.startswith("Dialogue:") or stripped.startswith("Comment:")):
            prefix, rest = line.split(":", 1)
            leading = " " if rest.startswith(" ") else ""
            fields = rest.lstrip(" ").split(",", maxsplit=max(1, total_cols - 1))
            if len(fields) > max(start_idx, end_idx):
                s_ms = _parse_ass_ms(fields[start_idx])
                e_ms = _parse_ass_ms(fields[end_idx])
                transformed = _transform_bounds(s_ms, e_ms, offset_ms, scale)
                if transformed is None:
                    continue
                fields[start_idx] = _format_ass_ms(transformed[0])
                fields[end_idx] = _format_ass_ms(transformed[1])
                cue_count += 1
                out_lines.append(f"{prefix}:{leading}{','.join(fields)}")
                continue
        out_lines.append(line)
    if cue_count == 0:
        raise SyncError("invalid_argument", "ASS/SSA 字幕未包含有效 Dialogue 时间轴")
    return "\n".join(out_lines).rstrip("\n") + "\n", cue_count


def resolve_sync_output_path(
    sub_path: Path,
    output_path: str | None,
    *,
    overwrite_policy: str,
    in_place: bool = False,
) -> Path:
    """Resolve output subtitle path while defaulting away from clobbering manual files."""
    if isinstance(output_path, str) and output_path.strip():
        return validate_safe_fs_path(output_path.strip(), allow_nonexistent=True)
    if in_place or overwrite_policy == "overwrite":
        return sub_path
    return sub_path.with_name(f"{sub_path.stem}.synced{sub_path.suffix}")


def shift_subtitle_file(
    *,
    subtitle_path: str,
    output_path: str | None,
    offset_ms: Any,
    scale: Any,
    overwrite_policy: str,
    data_dir: Path,
    in_place: bool = False,
) -> dict[str, Any]:
    """Apply linear time transformation (t' = t * scale + offset_ms) and write atomically."""
    try:
        offset_val = float(offset_ms)
        scale_val = float(scale)
    except (TypeError, ValueError) as exc:
        raise SyncError("invalid_argument", f"offset_ms 或 scale 参数无效: {exc}") from exc
    if not math.isfinite(offset_val) or not math.isfinite(scale_val) or not (0.1 <= scale_val <= 10.0):
        raise SyncError("invalid_argument", "offset_ms 必须有限，scale 必须在 [0.1, 10.0] 之间")

    sub_path = validate_safe_fs_path(subtitle_path, allow_nonexistent=False)
    if not sub_path.is_file():
        raise SyncError("not_found", f"字幕文件不存在: {sub_path}")
    target_out = resolve_sync_output_path(
        sub_path, output_path, overwrite_policy=overwrite_policy, in_place=in_place
    )

    action = check_overwrite_allowed(target_out, data_dir, overwrite_policy)
    if action == "skip":
        return {
            "mode": "manual_shift",
            "skipped": True,
            "subtitle_path": str(sub_path),
            "output_path": str(target_out),
            "offset_ms": offset_val,
            "scale": scale_val,
            "cue_count": 0,
        }

    raw_bytes = sub_path.read_bytes()
    if len(raw_bytes) > HARD_MAX_DOWNLOAD_BYTES:
        raise SyncError("validation_error", "字幕文件超过 16 MB 上限")
    decoded, src_encoding = decode_subtitle_bytes(raw_bytes)
    ext = sub_path.suffix.lower()
    if ext == ".vtt":
        transformed, cue_count = _shift_vtt(decoded, offset_val, scale_val)
    elif ext in {".ass", ".ssa"}:
        transformed, cue_count = _shift_ass(decoded, offset_val, scale_val)
    elif ext == ".srt":
        transformed, cue_count = _shift_srt(decoded, offset_val, scale_val)
    else:
        raise SyncError("validation_error", f"不支持线性校正的字幕格式: {ext}")

    atomic_write_text(target_out, transformed)
    record_managed_subtitle(data_dir, target_out, transformed)
    return {
        "mode": "manual_shift",
        "skipped": False,
        "subtitle_path": str(sub_path),
        "output_path": str(target_out),
        "source_encoding": src_encoding,
        "offset_ms": offset_val,
        "scale": scale_val,
        "cue_count": cue_count,
        "format": ext.lstrip("."),
    }


async def sync_with_ffsubsync(
    ctx: Any,
    *,
    ffsubsync_bin: str,
    subtitle_path: str,
    reference_path: str,
    output_path: str | None,
    overwrite_policy: str,
    data_dir: Path,
    audio_stream: int = -1,
    max_offset_seconds: float | None = None,
    timeout_sec: float = 120.0,
    in_place: bool = False,
) -> dict[str, Any]:
    """Invoke ffsubsync via ctx.exec_subprocess arg list (shell=False) and write back atomically."""
    resolved_bin = resolve_ffsubsync_bin(ffsubsync_bin)
    if not resolved_bin:
        raise SyncError("not_configured", f"未检测到可用的 ffsubsync 可执行文件 ({ffsubsync_bin!r})")

    sub_path = validate_safe_fs_path(subtitle_path, allow_nonexistent=False)
    ref_path = validate_safe_fs_path(reference_path, allow_nonexistent=False)
    if not sub_path.is_file():
        raise SyncError("not_found", f"字幕文件不存在: {sub_path}")
    if not ref_path.is_file():
        raise SyncError("not_found", f"参考音视频文件不存在: {ref_path}")

    target_out = resolve_sync_output_path(
        sub_path, output_path, overwrite_policy=overwrite_policy, in_place=in_place
    )
    action = check_overwrite_allowed(target_out, data_dir, overwrite_policy)
    if action == "skip":
        return {
            "mode": "ffsubsync",
            "skipped": True,
            "subtitle_path": str(sub_path),
            "reference_path": str(ref_path),
            "output_path": str(target_out),
        }

    target_out.parent.mkdir(parents=True, exist_ok=True)
    tmp_handle = tempfile.NamedTemporaryFile(
        dir=target_out.parent,
        prefix=f".{target_out.name}.",
        suffix=f".omnidesk.tmp{target_out.suffix or '.srt'}",
        delete=False,
    )
    tmp_path = Path(tmp_handle.name)
    tmp_handle.close()

    args: list[str] = [resolved_bin, str(ref_path), "-i", str(sub_path), "-o", str(tmp_path)]
    if max_offset_seconds is not None:
        max_off = float(max_offset_seconds)
        if not math.isfinite(max_off) or max_off <= 0:
            tmp_path.unlink(missing_ok=True)
            raise SyncError("invalid_argument", "max_offset_seconds 必须为正数")
        args.extend(["--max-offset-seconds", str(max_off)])
    if isinstance(audio_stream, int) and not isinstance(audio_stream, bool) and audio_stream >= 0:
        args.extend(["--reference-stream", f"a:{audio_stream}"])

    try:
        proc_res = await ctx.exec_subprocess(args, timeout_sec=timeout_sec)
        if proc_res.timed_out or proc_res.cancelled:
            raise SyncError("timeout", "ffsubsync 执行超时或已取消")
        if not proc_res.ok:
            err_tail = (proc_res.stderr or proc_res.stdout).strip().splitlines()[-1:] or ["unknown error"]
            raise SyncError("service_offline", f"ffsubsync 对齐失败 (rc={proc_res.returncode}): {err_tail[0][:200]}")
        if not tmp_path.is_file() or tmp_path.stat().st_size == 0:
            raise SyncError("service_offline", "ffsubsync 未生成有效输出字幕文件")
        if tmp_path.stat().st_size > HARD_MAX_DOWNLOAD_BYTES:
            raise SyncError("validation_error", "ffsubsync 输出字幕超过 16 MB 上限")

        utf8_text, _ = decode_subtitle_bytes(tmp_path.read_bytes())
        atomic_write_text(target_out, utf8_text)
        record_managed_subtitle(data_dir, target_out, utf8_text)
        return {
            "mode": "ffsubsync",
            "skipped": False,
            "subtitle_path": str(sub_path),
            "reference_path": str(ref_path),
            "output_path": str(target_out),
            "ffsubsync_bin": resolved_bin,
        }
    except ArchiveSecurityError as exc:
        raise SyncError(exc.error_code, str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
