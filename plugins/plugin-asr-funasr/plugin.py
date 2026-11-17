"""Offline FunASR SenseVoiceSmall chunked transcription plugin (`speech.asr.v1`)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext

if __package__:
    from .asr_checkpoint import AsrJob, TranscribeParams, clear_asr_cache, compute_media_job_id, compute_pcm_job_id
    from .asr_config import (
        SUPPORTED_ACTIONS,
        VALID_DEVICES,
        build_status_snapshot,
        build_transcribe_params,
        max_concurrency_from_cfg,
        parse_int,
        parse_timeout_sec,
        resolve_bin,
        resolve_cache_root,
        validate_existing_media,
    )
    from .asr_runner import execute_chunked_asr, run_probe_subprocess
    from .srt_utils import AsrError, resolve_output_srt_path, scrub_text
else:
    from asr_checkpoint import AsrJob, TranscribeParams, clear_asr_cache, compute_media_job_id, compute_pcm_job_id
    from asr_config import (
        SUPPORTED_ACTIONS,
        VALID_DEVICES,
        build_status_snapshot,
        build_transcribe_params,
        max_concurrency_from_cfg,
        parse_int,
        parse_timeout_sec,
        resolve_bin,
        resolve_cache_root,
        validate_existing_media,
    )
    from asr_runner import execute_chunked_asr, run_probe_subprocess
    from srt_utils import AsrError, resolve_output_srt_path, scrub_text

_VALID_JOB_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled"})
_MAX_JOB_HISTORY = 50


class AsrFunasrPlugin(BasePlugin):
    """Provider plugin exposing `speech.asr.v1` via isolated ffprobe/ffmpeg/FunASR subprocesses."""

    def __init__(self) -> None:
        self._jobs: dict[str, AsrJob] = {}
        self._running_slots = 0
        self._slot_cond: asyncio.Condition | None = None

    async def on_load(self, ctx: PluginContext) -> None:
        """Register `speech.asr.v1` non-blockingly (< 5ms) without starting any model."""
        await super().on_load(ctx)
        self._slot_cond = asyncio.Condition()
        ctx.register_streaming_capability("speech.asr.v1", self.handle_speech_asr)

    async def on_unload(self) -> None:
        """Signal cancellation to all active jobs and clear transient runtime state."""
        for job in self._jobs.values():
            if job.status in {"queued", "running"}:
                job.cancel_event.set()
        if self._slot_cond is not None:
            try:
                async with self._slot_cond:
                    self._slot_cond.notify_all()
            except RuntimeError:
                pass
        self._jobs.clear()
        self._running_slots = 0
        self._slot_cond = None

    async def on_health_check(self) -> dict[str, Any]:
        """Return lightweight config/binary readiness without spawning any model."""
        snap = build_status_snapshot(self._read_cfg(), self._jobs)
        plugin_id = self.ctx.plugin_id if self.ctx is not None else "plugin-asr-funasr"
        return {"ok": True, "plugin_id": plugin_id, "registered_capabilities": ["speech.asr.v1"], **snap}

    def _read_cfg(self) -> dict[str, Any]:
        return {} if self.ctx is None else self.ctx.get_config()

    def _prune_job_history(self) -> None:
        finished = [j for j in self._jobs.values() if j.status not in {"queued", "running"}]
        if len(finished) > _MAX_JOB_HISTORY:
            finished.sort(key=lambda j: j.updated_at)
            for old in finished[: len(finished) - _MAX_JOB_HISTORY]:
                self._jobs.pop(old.job_id, None)

    async def handle_speech_asr(self, req: Mapping[str, Any], stream: Any = None) -> CapabilityResult:
        """Dispatch `speech.asr.v1` actions with fail-closed validation and process isolation."""
        if self.ctx is None:
            return CapabilityResult.fail("插件尚未加载", error_code="not_configured")
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "").strip()
        try:
            if action == "status":
                return self._handle_status()
            if action in {"probe", "list_streams"}:
                return await self._handle_probe(payload, list_only=(action == "list_streams"))
            if action in {"transcribe_audio", "transcribe_media"}:
                return await self._handle_transcribe(action, payload, stream)
            if action == "cancel":
                return await self._handle_cancel(payload)
            if action == "clear_cache":
                return CapabilityResult.ok(
                    clear_asr_cache(
                        resolve_cache_root(self.ctx),
                        self._jobs,
                        raw_job_id=payload.get("job_id"),
                        raw_media_path=payload.get("media_path"),
                    )
                )
            if action == "list_jobs":
                return self._handle_list_jobs(payload)
            return CapabilityResult.fail(
                f"不支持的 speech.asr.v1 动作: {action!r}",
                data={"supported_actions": list(SUPPORTED_ACTIONS)},
                error_code="invalid_argument",
            )
        except AsrError as exc:
            return CapabilityResult.fail(scrub_text(exc.message), data=exc.data, error_code=exc.error_code)

    def _handle_status(self) -> CapabilityResult:
        snap = build_status_snapshot(self._read_cfg(), self._jobs)
        if snap["device"] not in VALID_DEVICES:
            return CapabilityResult.fail(f"不支持的推理设备: {snap['device']}", data=snap, error_code="invalid_argument")
        if not snap["configured"]:
            return CapabilityResult.fail("FunASR 模型目录或工具路径尚未配置", data=snap, error_code="not_configured")
        if not snap["ready"]:
            return CapabilityResult.fail("FunASR 或 FFmpeg/FFprobe 可执行文件不可用", data=snap, error_code="service_offline")
        return CapabilityResult.ok(snap)

    async def _handle_probe(self, req: Mapping[str, Any], *, list_only: bool) -> CapabilityResult:
        assert self.ctx is not None
        ffprobe_bin = str(self._read_cfg().get("ffprobe_bin", "ffprobe") or "").strip()
        if not ffprobe_bin:
            raise AsrError("not_configured", "尚未配置 ffprobe_bin")
        ffprobe_path = resolve_bin(ffprobe_bin)
        if ffprobe_path is None:
            raise AsrError("service_offline", f"未找到可用的 ffprobe 可执行文件: {ffprobe_bin}")

        media_file = validate_existing_media(req.get("media_path") or req.get("audio_path") or req.get("path"))
        timeout_sec = parse_timeout_sec(req.get("timeout_ms"), default_ms=30000)
        stream_type = str(req.get("stream_type") or "").strip().lower()
        if list_only and stream_type and stream_type not in {"audio", "video", "subtitle"}:
            raise AsrError("invalid_argument", f"无效的 stream_type: {stream_type!r}")

        info = await run_probe_subprocess(
            self.ctx, ffprobe_path, media_file, timeout_sec=timeout_sec, require_duration=not list_only
        )
        info["media_path"] = str(media_file)
        if not list_only:
            return CapabilityResult.ok(info)
        streams = [s for s in info["streams"] if s["codec_type"] == stream_type] if stream_type else info["streams"]
        return CapabilityResult.ok(
            {
                "media_path": str(media_file),
                "duration_sec": info["duration_sec"],
                "streams": streams,
                "audio_streams": info["audio_streams"],
                "video_streams": info["video_streams"],
                "subtitle_streams": info["subtitle_streams"],
                "count": len(streams),
            }
        )

    async def _acquire_slot(self, job: AsrJob, max_concurrency: int, timeout_sec: float) -> None:
        if self._slot_cond is None:
            self._slot_cond = asyncio.Condition()
        cond, deadline = self._slot_cond, asyncio.get_running_loop().time() + timeout_sec
        async with cond:
            while self._running_slots >= max_concurrency and not job.cancel_event.is_set():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise AsrError("timeout", "等待 ASR 并发执行槽位超时", data={"job_id": job.job_id})
                try:
                    await asyncio.wait_for(cond.wait(), timeout=remaining)
                except TimeoutError:
                    if self._running_slots >= max_concurrency and not job.cancel_event.is_set():
                        raise AsrError("timeout", "等待 ASR 并发执行槽位超时", data={"job_id": job.job_id}) from None
            if job.cancel_event.is_set():
                raise AsrError("cancelled", "ASR 转写任务已取消", data={"job_id": job.job_id})
            self._running_slots += 1
            job.status = "running"
            job.updated_at = time.time()

    async def _release_slot(self) -> None:
        if self._slot_cond is None:
            self._running_slots = max(0, self._running_slots - 1)
            return
        async with self._slot_cond:
            self._running_slots = max(0, self._running_slots - 1)
            self._slot_cond.notify_all()

    async def _handle_transcribe(self, action: str, req: Mapping[str, Any], stream: Any) -> CapabilityResult:
        assert self.ctx is not None
        cfg = self._read_cfg()
        params, max_concurrency = build_transcribe_params(self.ctx, cfg, req)
        output_root = str(cfg.get("output_root", "") or "").strip()

        media_file: Path | None = None
        pcm_bytes: bytes | None = None
        pcm_rate, pcm_channels = 16000, 1
        normalize_audio = bool(req.get("normalize", False))
        raw_media = req.get("media_path") if action == "transcribe_media" else (req.get("audio_path") or req.get("media_path"))
        raw_pcm = req.get("pcm_s16le_base64")

        if raw_media is not None:
            media_file = validate_existing_media(raw_media, "media_path" if action == "transcribe_media" else "audio_path")
            out_srt = resolve_output_srt_path(
                media_file=media_file, output_srt_path=req.get("output_srt_path"), output_root=output_root
            )
            job_id, st = compute_media_job_id(media_file, params.chunk_seconds)
            media_label, mtime_ns, size = str(media_file), st.st_mtime_ns, st.st_size
        elif action == "transcribe_audio" and isinstance(raw_pcm, str) and raw_pcm.strip():
            try:
                pcm_bytes = base64.b64decode(raw_pcm.strip(), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise AsrError("invalid_argument", "pcm_s16le_base64 不是有效的 Base64 数据") from exc
            pcm_rate = parse_int(req.get("sample_rate", 16000), "sample_rate", minimum=8000, maximum=192000)
            pcm_channels = parse_int(req.get("channels", 1), "channels", minimum=1, maximum=8)
            if not pcm_bytes or len(pcm_bytes) % (2 * pcm_channels) != 0:
                raise AsrError("invalid_argument", "PCM 字节长度与采样格式不匹配")
            out_srt = resolve_output_srt_path(
                media_file=None, output_srt_path=req.get("output_srt_path"), output_root=output_root
            )
            job_id = compute_pcm_job_id(pcm_bytes, pcm_rate, pcm_channels, params.chunk_seconds)
            media_label, mtime_ns, size = "<pcm_s16le>", 0, len(pcm_bytes)
        else:
            raise AsrError("invalid_argument", f"{action} 缺少 media_path 或 audio_path")

        existing = self._jobs.get(job_id)
        if existing is not None and existing.status in {"queued", "running"}:
            raise AsrError("conflict", f"相同媒体任务已在运行中: {job_id}", data={"job_id": job_id})

        params = TranscribeParams(**{**params.__dict__, "output_srt_path": out_srt})
        job = AsrJob(
            job_id=job_id, action=action, media_path=media_label,
            output_srt_path=str(out_srt) if out_srt else "",
            device=params.device, chunk_seconds=params.chunk_seconds, task=asyncio.current_task(),
        )
        self._jobs[job_id] = job
        self._prune_job_history()

        acquired = False
        try:
            await self._acquire_slot(job, max_concurrency, params.timeout_sec)
            acquired = True
            result = await execute_chunked_asr(
                self.ctx, job, params, media_file=media_file, media_label=media_label,
                mtime_ns=mtime_ns, size=size, pcm_bytes=pcm_bytes,
                pcm_sample_rate=pcm_rate, pcm_channels=pcm_channels,
                normalize_audio=normalize_audio, stream=stream,
            )
            job.status, job.updated_at = "completed", time.time()
            return CapabilityResult.ok(result)
        except asyncio.CancelledError:
            job.cancel_event.set()
            job.status, job.error_code, job.error, job.updated_at = "cancelled", "cancelled", "ASR 转写任务已取消", time.time()
            raise
        except AsrError as exc:
            job.status = "cancelled" if exc.error_code == "cancelled" else "failed"
            job.error_code, job.error, job.updated_at = exc.error_code, scrub_text(exc.message), time.time()
            return CapabilityResult.fail(job.error, data={**exc.data, "job_id": job_id}, error_code=exc.error_code)
        finally:
            if acquired:
                await self._release_slot()
            job.done_event.set()

    async def _handle_cancel(self, req: Mapping[str, Any]) -> CapabilityResult:
        raw_id = req.get("job_id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise AsrError("invalid_argument", "cancel 缺少必填参数: job_id")
        job_id = raw_id.strip()
        job = self._jobs.get(job_id)
        if job is None:
            raise AsrError("not_found", f"未找到 ASR 任务: {job_id}", data={"job_id": job_id})
        if job.status not in {"queued", "running"}:
            return CapabilityResult.ok({
                "job_id": job_id, "cancelled": job.status == "cancelled", "already_finished": True, "status": job.status
            })
        job.cancel_event.set()
        if self._slot_cond is not None:
            async with self._slot_cond:
                self._slot_cond.notify_all()
        try:
            await asyncio.wait_for(job.done_event.wait(), timeout=3.0)
        except TimeoutError:
            if job.task is not None and not job.task.done():
                job.task.cancel()
        return CapabilityResult.ok({"job_id": job_id, "cancelled": True, "status": job.status})

    def _handle_list_jobs(self, req: Mapping[str, Any]) -> CapabilityResult:
        status_filter = str(req.get("status") or "").strip().lower()
        if status_filter and status_filter not in _VALID_JOB_STATUSES:
            raise AsrError("invalid_argument", f"不支持的任务状态过滤: {status_filter!r}")
        jobs = [j.to_summary() for j in self._jobs.values() if not status_filter or j.status == status_filter]
        jobs.sort(key=lambda item: item["created_at"], reverse=True)
        return CapabilityResult.ok({
            "jobs": jobs, "count": len(jobs),
            "active_count": sum(1 for j in self._jobs.values() if j.status in {"queued", "running"}),
            "max_concurrency": max_concurrency_from_cfg(self._read_cfg()),
        })


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning an `AsrFunasrPlugin` instance."""
    return AsrFunasrPlugin()
