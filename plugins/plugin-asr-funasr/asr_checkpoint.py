"""ASR job metadata, deterministic job IDs, atomic checkpoints, and cache cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__:
    from .srt_utils import AsrError, atomic_write_json
else:
    from srt_utils import AsrError, atomic_write_json

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def compute_media_job_id(media_file: Path, chunk_seconds: int) -> tuple[str, os.stat_result]:
    """Build deterministic job_id = md5(media_path + mtime + size + chunk_seconds)."""
    st = media_file.stat()
    raw = f"{media_file.resolve()}|{st.st_mtime_ns}|{st.st_size}|{chunk_seconds}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest(), st


def compute_pcm_job_id(pcm_bytes: bytes, sample_rate: int, channels: int, chunk_seconds: int) -> str:
    """Build deterministic job_id for raw PCM buffers."""
    digest = hashlib.md5()
    digest.update(pcm_bytes)
    digest.update(f"|{sample_rate}|{channels}|{chunk_seconds}".encode("utf-8"))
    return digest.hexdigest()


@dataclass
class AsrJob:
    """In-memory state for an active or recently finished ASR transcription job."""

    job_id: str
    action: str
    media_path: str
    output_srt_path: str
    device: str
    chunk_seconds: int
    status: str = "queued"
    stage: str = "queued"
    completed_chunks: int = 0
    total_chunks: int = 0
    progress: float = 0.0
    error: str = ""
    error_code: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None

    def to_summary(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "media_path": self.media_path,
            "output_srt_path": self.output_srt_path,
            "device": self.device,
            "chunk_seconds": self.chunk_seconds,
            "status": self.status,
            "stage": self.stage,
            "completed_chunks": self.completed_chunks,
            "total_chunks": self.total_chunks,
            "progress": self.progress,
            "error": self.error,
            "error_code": self.error_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class TranscribeParams:
    """Validated parameters driving a single chunked ASR execution."""

    funasr_path: str
    ffmpeg_path: str
    ffprobe_path: str
    model_file: Path
    vad_file: Path | None
    device: str
    chunk_seconds: int
    audio_stream: int
    vad_maxseg_ms: int
    use_vad: bool
    keep_tags: bool
    auto_fallback_cpu: bool
    reset_cache: bool
    offset_ms: int
    timeout_sec: float
    output_srt_path: Path | None
    cache_root: Path


def load_checkpoint(
    ckpt_path: Path,
    *,
    expected_job_id: str,
    chunk_seconds: int,
    total_chunks: int,
) -> tuple[set[int], list[dict[str, Any]], float, int] | None:
    """Load and validate a `.checkpoint.json` file; return None if missing or corrupt."""
    if ckpt_path.is_symlink():
        raise AsrError("path_safety", f"检查点文件不能为符号链接: {ckpt_path}")
    if not ckpt_path.is_file():
        return None
    try:
        raw = json.loads(ckpt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, Mapping):
        return None
    if raw.get("job_id") != expected_job_id or raw.get("chunk_seconds") != chunk_seconds:
        return None
    if raw.get("total_chunks") != total_chunks:
        return None

    raw_completed = raw.get("completed_chunks")
    if isinstance(raw_completed, int) and not isinstance(raw_completed, bool):
        completed = set(range(max(0, min(raw_completed, total_chunks))))
    elif isinstance(raw_completed, list):
        completed = {
            idx for idx in raw_completed if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < total_chunks
        }
    else:
        return None

    raw_segments = raw.get("segments")
    if not isinstance(raw_segments, list):
        return None
    segments: list[dict[str, Any]] = []
    for item in raw_segments:
        if not isinstance(item, Mapping):
            return None
        cidx, s_ms, e_ms, txt = item.get("chunk_index"), item.get("start_ms"), item.get("end_ms"), item.get("text")
        if (
            not isinstance(cidx, int)
            or isinstance(cidx, bool)
            or not isinstance(s_ms, int)
            or isinstance(s_ms, bool)
            or not isinstance(e_ms, int)
            or isinstance(e_ms, bool)
            or not isinstance(txt, str)
        ):
            return None
        if cidx in completed and txt.strip() and e_ms > s_ms >= 0:
            segments.append({"chunk_index": cidx, "start_ms": s_ms, "end_ms": e_ms, "text": txt})

    offset_val = raw.get("offset", 0.0)
    offset_sec = float(offset_val) if isinstance(offset_val, (int, float)) and not isinstance(offset_val, bool) else 0.0
    if not math.isfinite(offset_sec) or offset_sec < 0:
        offset_sec = 0.0
    offset_ms = raw.get("offset_ms", round(offset_sec * 1000))
    if not isinstance(offset_ms, int) or isinstance(offset_ms, bool) or offset_ms < 0:
        offset_ms = round(offset_sec * 1000)
    return completed, segments, offset_sec, offset_ms


def write_checkpoint(
    ckpt_path: Path,
    *,
    job: AsrJob,
    media_label: str,
    mtime_ns: int,
    size: int,
    audio_stream: int,
    duration_sec: float,
    total_chunks: int,
    completed: set[int],
    segments: list[dict[str, Any]],
    offset_sec: float,
    offset_ms: int,
) -> None:
    """Atomically persist checkpoint state after each completed chunk."""
    payload = {
        "version": 1,
        "job_id": job.job_id,
        "media_path": media_label,
        "mtime_ns": mtime_ns,
        "size": size,
        "chunk_seconds": job.chunk_seconds,
        "audio_stream": audio_stream,
        "duration_sec": duration_sec,
        "total_chunks": total_chunks,
        "completed_chunks": sorted(completed),
        "completed_count": len(completed),
        "offset": offset_sec,
        "offset_ms": offset_ms,
        "segments": segments,
        "updated_at": time.time(),
    }
    atomic_write_json(ckpt_path, payload, mode=0o600)


def clear_asr_cache(
    cache_root: Path,
    jobs: Mapping[str, AsrJob],
    *,
    raw_job_id: Any = None,
    raw_media_path: Any = None,
) -> dict[str, Any]:
    """Delete only plugin-owned checkpoint/chunk cache entries, never user media or subtitles."""
    if cache_root.is_symlink():
        raise AsrError("path_safety", f"缓存根目录不能为符号链接: {cache_root}")
    jobs_dir = cache_root / "jobs"
    if jobs_dir.is_symlink():
        raise AsrError("path_safety", f"缓存任务目录不能为符号链接: {jobs_dir}")
    if not jobs_dir.is_dir():
        return {"removed_jobs": [], "removed_count": 0, "removed_files": 0, "removed_bytes": 0, "skipped_active": []}

    target_job_id = ""
    if raw_job_id is not None:
        if not isinstance(raw_job_id, str) or not _JOB_ID_RE.match(raw_job_id.strip()):
            raise AsrError("invalid_argument", "clear_cache 的 job_id 格式无效")
        target_job_id = raw_job_id.strip()

    raw_media = str(raw_media_path or "").strip()
    resolved_media = str(Path(raw_media).expanduser().resolve()) if raw_media else ""

    removed_jobs: list[str] = []
    skipped_active: list[str] = []
    removed_files = 0
    removed_bytes = 0

    for entry in sorted(jobs_dir.iterdir(), key=lambda p: p.name):
        if entry.is_symlink() or not entry.is_dir():
            continue
        jid = entry.name
        if target_job_id and jid != target_job_id:
            continue
        if resolved_media:
            ckpt = entry / ".checkpoint.json"
            if not ckpt.is_file() or ckpt.is_symlink():
                continue
            try:
                meta = json.loads(ckpt.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(meta.get("media_path") or "") != resolved_media:
                continue

        active = jobs.get(jid)
        if active is not None and active.status in {"queued", "running"}:
            if target_job_id:
                raise AsrError("conflict", f"任务正在运行中，无法清理缓存: {jid}", data={"job_id": jid})
            skipped_active.append(jid)
            continue

        for sub in entry.rglob("*"):
            if sub.is_file() and not sub.is_symlink():
                removed_files += 1
                removed_bytes += sub.stat().st_size
        shutil.rmtree(entry)
        removed_jobs.append(jid)

    return {
        "removed_jobs": removed_jobs,
        "removed_count": len(removed_jobs),
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "skipped_active": skipped_active,
    }
