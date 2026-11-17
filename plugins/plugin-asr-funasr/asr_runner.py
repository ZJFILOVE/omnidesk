"""Chunked ASR subprocess pipeline (`ffprobe`, `ffmpeg`, `llama-funasr-sensevoice`)."""

from __future__ import annotations

import time
import wave
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import PluginContext, SubprocessResult

if __package__:
    from .asr_checkpoint import AsrJob, TranscribeParams, load_checkpoint, write_checkpoint
    from .srt_utils import (
        AsrError,
        atomic_write_text,
        merge_segments_to_srt,
        parse_chunk_asr_output,
        parse_ffprobe_output,
        scrub_text,
    )
else:
    from asr_checkpoint import AsrJob, TranscribeParams, load_checkpoint, write_checkpoint
    from srt_utils import (
        AsrError,
        atomic_write_text,
        merge_segments_to_srt,
        parse_chunk_asr_output,
        parse_ffprobe_output,
        scrub_text,
    )

_FFPROBE_ENTRIES = (
    "format=duration,format_name,size,bit_rate:"
    "stream=index,codec_type,codec_name,sample_rate,channels,channel_layout,bit_rate,duration:"
    "stream_tags=language,title"
)


async def emit_stream(stream: Any, event: str, data: Mapping[str, Any]) -> None:
    """Forward a structured event to CapabilityStream when present."""
    if stream is not None and hasattr(stream, "emit_event"):
        await stream.emit_event(event, dict(data))


async def emit_stage(
    stream: Any, job: AsrJob, media_label: str, stage: str, message: str, **extra: Any
) -> None:
    """Update job stage and emit a `stage` stream event."""
    job.stage = stage
    payload = {"job_id": job.job_id, "media_path": media_label, "stage": stage, "message": message, **extra}
    await emit_stream(stream, "stage", payload)


def check_cancelled(job: AsrJob, stream: Any) -> None:
    """Raise AsrError('cancelled') if cancel was requested via job or stream."""
    if job.cancel_event.is_set() or (stream is not None and bool(getattr(stream, "is_cancelled", False))):
        job.cancel_event.set()
        raise AsrError("cancelled", "ASR 转写任务已取消", data={"job_id": job.job_id})


def _secret_values(ctx: PluginContext) -> tuple[str, ...]:
    vault = getattr(ctx, "_secret_vault", None)
    fn = getattr(vault, "secret_values", None)
    if callable(fn):
        try:
            return tuple(fn())
        except Exception:
            return ()
    return ()


async def run_probe_subprocess(
    ctx: PluginContext,
    ffprobe_path: str,
    media_file: Path,
    *,
    timeout_sec: float,
    cancel_token: Any = None,
    require_duration: bool = True,
) -> dict[str, Any]:
    """Invoke ffprobe via arg-list and parse normalized stream metadata."""
    args = [ffprobe_path, "-v", "error", "-show_entries", _FFPROBE_ENTRIES, "-of", "json", str(media_file)]
    try:
        res: SubprocessResult = await ctx.exec_subprocess(
            args, timeout_sec=timeout_sec, cancel_token=cancel_token, grace_sec=0.5
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise AsrError("service_offline", f"无法启动 ffprobe: {scrub_text(str(exc), _secret_values(ctx))}") from exc
    if res.timed_out:
        raise AsrError("timeout", "ffprobe 探测超时")
    if res.cancelled:
        raise AsrError("cancelled", "ffprobe 探测已取消")
    if res.returncode != 0:
        detail = scrub_text(res.stderr.strip()[-400:], _secret_values(ctx))
        raise AsrError("invalid_argument", f"ffprobe 探测失败 (rc={res.returncode}): {detail or '无效媒体文件'}")
    return parse_ffprobe_output(res.stdout, require_duration=require_duration)


def _select_audio_map(probe_info: Mapping[str, Any], audio_stream: int) -> str:
    audio_streams = probe_info.get("audio_streams") or []
    if not audio_streams:
        raise AsrError("invalid_argument", "媒体文件不包含可用音轨")
    if 0 <= audio_stream < len(audio_streams):
        return f"0:a:{audio_stream}"
    for item in audio_streams:
        if item.get("stream_index") == audio_stream:
            return f"0:{audio_stream}"
    raise AsrError("invalid_argument", f"音轨索引无效: audio_stream={audio_stream}")


async def _run_ffmpeg(ctx: PluginContext, job: AsrJob, params: TranscribeParams, ff_args: list[str], label: str) -> None:
    secrets = _secret_values(ctx)
    try:
        ff_res = await ctx.exec_subprocess(
            ff_args, timeout_sec=params.timeout_sec, cancel_token=job.cancel_event, grace_sec=0.5
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise AsrError("service_offline", f"无法启动 ffmpeg: {scrub_text(str(exc), secrets)}") from exc
    if ff_res.timed_out:
        raise AsrError("timeout", f"ffmpeg {label}超时", data={"job_id": job.job_id})
    if ff_res.cancelled or job.cancel_event.is_set():
        raise AsrError("cancelled", f"ffmpeg {label}已取消", data={"job_id": job.job_id})
    if ff_res.returncode != 0:
        detail = scrub_text(ff_res.stderr.strip()[-400:], secrets)
        raise AsrError("invalid_argument", f"ffmpeg {label}失败 (rc={ff_res.returncode}): {detail or '音轨处理失败'}")


async def _run_funasr_chunk(ctx: PluginContext, job: AsrJob, params: TranscribeParams, wav_path: Path) -> str:
    secrets = _secret_values(ctx)

    async def _invoke(backend: str) -> SubprocessResult:
        cmd = [params.funasr_path, "-m", str(params.model_file)]
        if params.use_vad and params.vad_file is not None:
            cmd.extend(["--vad", str(params.vad_file), "--vad-maxseg", str(params.vad_maxseg_ms)])
        cmd.extend(["--backend", backend, "--srt"])
        if params.keep_tags:
            cmd.append("--keep-tags")
        cmd.extend(["-a", str(wav_path)])
        try:
            return await ctx.exec_subprocess(
                cmd, timeout_sec=params.timeout_sec, cancel_token=job.cancel_event, grace_sec=0.5
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise AsrError("service_offline", f"无法启动 FunASR: {scrub_text(str(exc), secrets)}") from exc

    res = await _invoke(params.device)
    if not res.ok and not res.timed_out and not res.cancelled and params.auto_fallback_cpu and params.device == "vulkan":
        res = await _invoke("cpu")
    if res.timed_out:
        raise AsrError("timeout", "FunASR 转写超时", data={"job_id": job.job_id})
    if res.cancelled or job.cancel_event.is_set():
        raise AsrError("cancelled", "FunASR 转写已取消", data={"job_id": job.job_id})
    if res.returncode != 0:
        detail = scrub_text(res.stderr.strip()[-400:], secrets)
        raise AsrError(
            "service_offline",
            f"FunASR 转写失败 (rc={res.returncode}): {detail or '推理进程异常退出'}",
            data={"job_id": job.job_id},
        )
    return res.stdout


def _write_pcm_wav(wav_path: Path, pcm_slice: bytes, sample_rate: int, channels: int) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_slice)


async def _prepare_chunk_wav(
    ctx: PluginContext,
    job: AsrJob,
    params: TranscribeParams,
    *,
    media_file: Path | None,
    audio_map: str,
    c_start_ms: int,
    c_end_ms: int,
    wav_path: Path,
    raw_wav: Path,
    pcm_bytes: bytes | None,
    pcm_sample_rate: int,
    pcm_channels: int,
    normalize_audio: bool,
) -> None:
    if media_file is not None:
        c_start_sec, c_dur_sec = c_start_ms / 1000.0, max(1, c_end_ms - c_start_ms) / 1000.0
        ff_args = [
            params.ffmpeg_path, "-v", "error", "-nostdin", "-ss", f"{c_start_sec:.3f}", "-t", f"{c_dur_sec:.3f}",
            "-i", str(media_file), "-map", audio_map, "-vn", "-sn", "-dn",
            "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1", "-y", str(wav_path),
        ]
        await _run_ffmpeg(ctx, job, params, ff_args, "切片")
        return

    assert pcm_bytes is not None
    stride = pcm_sample_rate * 2 * pcm_channels
    slice_bytes = pcm_bytes[(c_start_ms * stride) // 1000 : (c_end_ms * stride) // 1000]
    if pcm_sample_rate == 16000 and pcm_channels == 1 and not normalize_audio:
        _write_pcm_wav(wav_path, slice_bytes, pcm_sample_rate, pcm_channels)
        return
    _write_pcm_wav(raw_wav, slice_bytes, pcm_sample_rate, pcm_channels)
    ff_args = [
        params.ffmpeg_path, "-v", "error", "-nostdin", "-i", str(raw_wav),
        *(["-af", "loudnorm"] if normalize_audio else []),
        "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1", "-y", str(wav_path),
    ]
    await _run_ffmpeg(ctx, job, params, ff_args, "重采样")


async def execute_chunked_asr(
    ctx: PluginContext,
    job: AsrJob,
    params: TranscribeParams,
    *,
    media_file: Path | None,
    media_label: str,
    mtime_ns: int,
    size: int,
    pcm_bytes: bytes | None = None,
    pcm_sample_rate: int = 16000,
    pcm_channels: int = 1,
    normalize_audio: bool = False,
    stream: Any = None,
) -> dict[str, Any]:
    """Execute ffprobe -> chunked ffmpeg/WAV -> llama-funasr-sensevoice -> SRT merge."""
    job_dir = params.cache_root / "jobs" / job.job_id
    if params.cache_root.is_symlink() or job_dir.is_symlink():
        raise AsrError("path_safety", f"缓存目录不能为符号链接: {job_dir}")
    chunks_dir = job_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = job_dir / ".checkpoint.json"
    marker = params.cache_root / ".omnidesk-asr-cache"
    if not marker.exists() and not marker.is_symlink():
        atomic_write_text(marker, "plugin-asr-funasr\n", mode=0o600)

    audio_map = "0:a:0"
    if media_file is not None:
        await emit_stage(stream, job, media_label, "probing", "正在探测音轨与时长")
        probe_info = await run_probe_subprocess(
            ctx, params.ffprobe_path, media_file, timeout_sec=params.timeout_sec, cancel_token=job.cancel_event
        )
        duration_sec = float(probe_info["duration_sec"])
        audio_map = _select_audio_map(probe_info, params.audio_stream)
    else:
        assert pcm_bytes is not None
        duration_sec = len(pcm_bytes) / (pcm_sample_rate * 2 * pcm_channels)

    duration_ms = max(1, round(duration_sec * 1000))
    chunk_ms = params.chunk_seconds * 1000
    total_chunks = max(1, (duration_ms + chunk_ms - 1) // chunk_ms)
    job.total_chunks = total_chunks
    job.updated_at = time.time()

    completed: set[int] = set()
    segments: list[dict[str, Any]] = []
    resumed = False

    if params.reset_cache:
        write_checkpoint(
            ckpt_path, job=job, media_label=media_label, mtime_ns=mtime_ns, size=size,
            audio_stream=params.audio_stream, duration_sec=duration_sec, total_chunks=total_chunks,
            completed=completed, segments=segments, offset_sec=0.0, offset_ms=0,
        )
    else:
        loaded = load_checkpoint(
            ckpt_path, expected_job_id=job.job_id, chunk_seconds=params.chunk_seconds, total_chunks=total_chunks
        )
        if loaded is not None:
            completed, segments, loaded_offset_sec, loaded_offset_ms = loaded
            if completed:
                resumed = True
                job.completed_chunks = len(completed)
                job.progress = len(completed) / total_chunks
                await emit_stream(
                    stream, "resumed",
                    {
                        "job_id": job.job_id, "media_path": media_label,
                        "completed_chunks": len(completed), "completed_chunk_indices": sorted(completed),
                        "total_chunks": total_chunks, "offset": loaded_offset_sec,
                        "offset_ms": loaded_offset_ms, "progress": job.progress,
                    },
                )

    for idx in range(total_chunks):
        if idx in completed:
            continue
        check_cancelled(job, stream)

        c_start_ms = idx * chunk_ms
        c_end_ms = min(duration_ms, (idx + 1) * chunk_ms)
        c_dur_ms = max(1, c_end_ms - c_start_ms)
        wav_path = chunks_dir / f"chunk-{idx:05d}.wav"
        raw_wav = chunks_dir / f"chunk-{idx:05d}.raw.wav"

        try:
            await emit_stage(
                stream, job, media_label, "extracting", f"提取分块音频 {idx + 1}/{total_chunks}",
                chunk_index=idx, total_chunks=total_chunks,
            )
            await _prepare_chunk_wav(
                ctx, job, params, media_file=media_file, audio_map=audio_map,
                c_start_ms=c_start_ms, c_end_ms=c_end_ms, wav_path=wav_path, raw_wav=raw_wav,
                pcm_bytes=pcm_bytes, pcm_sample_rate=pcm_sample_rate, pcm_channels=pcm_channels,
                normalize_audio=normalize_audio,
            )
            check_cancelled(job, stream)
            await emit_stage(
                stream, job, media_label, "transcribing", f"识别分块 {idx + 1}/{total_chunks}",
                chunk_index=idx, total_chunks=total_chunks,
            )
            asr_stdout = await _run_funasr_chunk(ctx, job, params, wav_path)
        finally:
            wav_path.unlink(missing_ok=True)
            raw_wav.unlink(missing_ok=True)

        max_end = (duration_ms + max(0, params.offset_ms)) if params.offset_ms >= 0 else None
        chunk_segs = parse_chunk_asr_output(
            asr_stdout, chunk_index=idx, offset_ms=c_start_ms + params.offset_ms,
            chunk_duration_ms=c_dur_ms, max_end_ms=max_end, keep_tags=params.keep_tags,
        )
        segments.extend(chunk_segs)
        completed.add(idx)

        done_count = len(completed)
        progress = round(done_count / total_chunks, 6)
        job.completed_chunks, job.progress, job.updated_at = done_count, progress, time.time()

        write_checkpoint(
            ckpt_path, job=job, media_label=media_label, mtime_ns=mtime_ns, size=size,
            audio_stream=params.audio_stream, duration_sec=duration_sec, total_chunks=total_chunks,
            completed=completed, segments=segments, offset_sec=c_end_ms / 1000.0, offset_ms=c_end_ms,
        )
        await emit_stream(
            stream, "chunk",
            {
                "job_id": job.job_id, "media_path": media_label, "chunk_index": idx,
                "offset": c_start_ms / 1000.0, "offset_ms": c_start_ms, "duration_sec": c_dur_ms / 1000.0,
                "segment_count": len(chunk_segs), "segments": chunk_segs,
                "completed_chunks": done_count, "total_chunks": total_chunks, "progress": progress,
            },
        )
        await ctx.emit(
            "asr.job.progress",
            {
                "job_id": job.job_id, "media_path": media_label,
                "completed_chunks": done_count, "total_chunks": total_chunks, "progress": progress,
            },
        )

    await emit_stage(
        stream, job, media_label, "merging", "正在合并并输出 SRT 字幕",
        completed_chunks=len(completed), total_chunks=total_chunks,
    )
    cues, srt_text, plain_text = merge_segments_to_srt(segments)
    out_srt_str = ""
    if params.output_srt_path is not None:
        atomic_write_text(params.output_srt_path, srt_text, mode=0o644)
        out_srt_str = str(params.output_srt_path)

    job.progress, job.updated_at = 1.0, time.time()
    await emit_stage(
        stream, job, media_label, "done", "ASR 转写完成",
        completed_chunks=len(completed), total_chunks=total_chunks, progress=1.0,
        output_srt_path=out_srt_str, cue_count=len(cues),
    )
    await ctx.emit(
        "asr.job.completed",
        {"job_id": job.job_id, "media_path": media_label, "output_srt_path": out_srt_str, "cue_count": len(cues)},
    )
    return {
        "job_id": job.job_id, "media_path": media_label, "output_srt_path": out_srt_str,
        "cue_count": len(cues), "cues": cues, "srt": srt_text, "text": plain_text,
        "duration_sec": round(duration_sec, 3), "chunk_seconds": params.chunk_seconds,
        "total_chunks": total_chunks, "completed_chunks": len(completed),
        "resumed": resumed, "device": params.device, "checkpoint_path": str(ckpt_path),
    }
