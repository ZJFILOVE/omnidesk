"""Configuration, binary/model discovery, and request parameter validation for FunASR plugin."""

from __future__ import annotations

import math
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import PluginContext

if __package__:
    from .asr_checkpoint import AsrJob, TranscribeParams
    from .srt_utils import AsrError
else:
    from asr_checkpoint import AsrJob, TranscribeParams
    from srt_utils import AsrError

SUPPORTED_ACTIONS = (
    "status",
    "probe",
    "list_streams",
    "transcribe_audio",
    "transcribe_media",
    "cancel",
    "clear_cache",
    "list_jobs",
)
VALID_DEVICES = frozenset({"vulkan", "cpu"})


def resolve_bin(raw_bin: str) -> str | None:
    """Resolve an executable name on PATH or an explicit executable path."""
    cleaned = raw_bin.strip()
    if not cleaned:
        return None
    if "/" in cleaned or "\\" in cleaned:
        path = Path(cleaned).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(cleaned)


def resolve_model_dir(raw_model_dir: str) -> tuple[bool, Path | None, Path | None]:
    """Locate SenseVoice and optional VAD GGUF files inside user-configured model_dir."""
    cleaned = raw_model_dir.strip()
    if not cleaned:
        return False, None, None
    path = Path(cleaned).expanduser()
    if path.is_symlink() or not path.exists():
        return False, None, None
    if path.is_file():
        return (True, path, None) if path.suffix.lower() == ".gguf" else (False, None, None)
    if not path.is_dir():
        return False, None, None

    model_file: Path | None = None
    for name in ("sensevoice-small-q8.gguf", "sensevoice.gguf", "model.gguf"):
        cand = path / name
        if cand.is_file() and not cand.is_symlink():
            model_file = cand
            break
    if model_file is None:
        sense = [p for p in sorted(path.glob("*sensevoice*.gguf")) if p.is_file() and not p.is_symlink()]
        other = [p for p in sorted(path.glob("*.gguf")) if p.is_file() and not p.is_symlink() and "vad" not in p.name.lower()]
        model_file = (sense or other or [None])[0]

    vad_file: Path | None = None
    for name in ("fsmn-vad.gguf", "vad.gguf"):
        cand = path / name
        if cand.is_file() and not cand.is_symlink():
            vad_file = cand
            break
    if vad_file is None:
        vads = [p for p in sorted(path.glob("*vad*.gguf")) if p.is_file() and not p.is_symlink()]
        vad_file = vads[0] if vads else None
    return True, model_file, vad_file


def parse_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (minimum <= value <= maximum):
        raise AsrError("invalid_argument", f"{name} 必须为 {minimum}..{maximum} 范围内的整数")
    return value


def parse_timeout_sec(value: Any, *, default_ms: int = 180000) -> float:
    raw = default_ms if value is None else value
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)) or raw <= 0:
        raise AsrError("invalid_argument", "timeout_ms 必须为大于 0 的有限数值")
    if float(raw) > 3_600_000:
        raise AsrError("invalid_argument", "timeout_ms 不能超过 3600000")
    return float(raw) / 1000.0


def validate_existing_media(raw_path: Any, field_name: str = "media_path") -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise AsrError("invalid_argument", f"参数无效或缺少: {field_name}")
    path = Path(raw_path.strip()).expanduser()
    if not path.is_file():
        raise AsrError("invalid_argument", f"媒体文件不存在或不可读: {raw_path}")
    return path.resolve()


def resolve_cache_root(ctx: PluginContext) -> Path:
    """Resolve plugin cache root; isolate test contexts that omit explicit `data_dir`."""
    if ctx.data_dir != ctx.plugin_dir:
        return ctx.data_dir / "asr-cache"
    store_root = getattr(getattr(ctx, "_config_store", None), "_root_dir", None)
    if isinstance(store_root, Path):
        return store_root / ".asr-cache"
    for env_var in ("XDG_CACHE_HOME", "XDG_DATA_HOME"):
        raw = os.environ.get(env_var, "").strip()
        if raw and Path(raw).is_absolute():
            return Path(raw) / "omnidesk" / ctx.plugin_id / "asr-cache"
    return Path(tempfile.gettempdir()) / "omnidesk" / ctx.plugin_id / "asr-cache"


def max_concurrency_from_cfg(cfg: Mapping[str, Any]) -> int:
    raw = cfg.get("max_concurrency", 1)
    return max(1, min(16, raw)) if isinstance(raw, int) and not isinstance(raw, bool) else 1


def build_status_snapshot(cfg: Mapping[str, Any], jobs: Mapping[str, AsrJob]) -> dict[str, Any]:
    """Compute non-blocking status snapshot without invoking any external model."""
    funasr_bin = str(cfg.get("funasr_bin", "llama-funasr-sensevoice") or "").strip()
    ffmpeg_bin = str(cfg.get("ffmpeg_bin", "ffmpeg") or "").strip()
    ffprobe_bin = str(cfg.get("ffprobe_bin", "ffprobe") or "").strip()
    model_dir = str(cfg.get("model_dir", "") or "").strip()
    device = str(cfg.get("device", "vulkan") or "vulkan").strip().lower()
    output_root = str(cfg.get("output_root", "") or "").strip()
    chunk_raw = cfg.get("chunk_seconds", 300)
    chunk_seconds = chunk_raw if isinstance(chunk_raw, int) and not isinstance(chunk_raw, bool) else 300

    funasr_path, ffmpeg_path, ffprobe_path = resolve_bin(funasr_bin), resolve_bin(ffmpeg_bin), resolve_bin(ffprobe_bin)
    model_dir_exists, model_file, vad_file = resolve_model_dir(model_dir)
    active_jobs = sum(1 for j in jobs.values() if j.status in {"queued", "running"})
    configured = bool(funasr_bin and ffmpeg_bin and ffprobe_bin and model_dir and model_dir_exists and model_file)
    ready = bool(configured and funasr_path and ffmpeg_path and ffprobe_path and device in VALID_DEVICES)
    return {
        "configured": configured, "ready": ready, "degraded": not ready, "device": device,
        "funasr_bin": funasr_bin, "funasr_available": funasr_path is not None, "funasr_path": funasr_path or "",
        "ffmpeg_bin": ffmpeg_bin, "ffmpeg_available": ffmpeg_path is not None, "ffmpeg_path": ffmpeg_path or "",
        "ffprobe_bin": ffprobe_bin, "ffprobe_available": ffprobe_path is not None, "ffprobe_path": ffprobe_path or "",
        "model_dir": model_dir, "model_dir_exists": model_dir_exists, "model_available": model_file is not None,
        "model_path": str(model_file) if model_file else "", "vad_available": vad_file is not None,
        "vad_path": str(vad_file) if vad_file else "", "chunk_seconds": chunk_seconds,
        "output_root": output_root, "max_concurrency": max_concurrency_from_cfg(cfg),
        "active_jobs": active_jobs, "supported_actions": list(SUPPORTED_ACTIONS),
    }


def build_transcribe_params(ctx: PluginContext, cfg: Mapping[str, Any], req: Mapping[str, Any]) -> tuple[TranscribeParams, int]:
    """Validate configuration and request parameters for transcribe_audio / transcribe_media."""
    funasr_bin = str(cfg.get("funasr_bin", "llama-funasr-sensevoice") or "").strip()
    ffmpeg_bin = str(cfg.get("ffmpeg_bin", "ffmpeg") or "").strip()
    ffprobe_bin = str(cfg.get("ffprobe_bin", "ffprobe") or "").strip()
    model_dir = str(cfg.get("model_dir", "") or "").strip()
    if not (funasr_bin and ffmpeg_bin and ffprobe_bin and model_dir):
        raise AsrError("not_configured", "FunASR 模型目录或核心可执行文件路径尚未配置")
    model_dir_exists, model_file, vad_file = resolve_model_dir(model_dir)
    if not model_dir_exists or model_file is None:
        raise AsrError("not_configured", "配置的 model_dir 不存在或缺少 SenseVoice GGUF 模型文件")

    funasr_path, ffmpeg_path, ffprobe_path = resolve_bin(funasr_bin), resolve_bin(ffmpeg_bin), resolve_bin(ffprobe_bin)
    if not (funasr_path and ffmpeg_path and ffprobe_path):
        raise AsrError("service_offline", "FunASR 或 FFmpeg/FFprobe 可执行文件不可用")

    cfg_device = str(cfg.get("device", "vulkan") or "vulkan").strip().lower()
    if cfg_device not in VALID_DEVICES:
        raise AsrError("invalid_argument", f"配置中的 device 无效: {cfg_device!r}")
    raw_backend = req.get("backend") if req.get("backend") is not None else req.get("device")
    device = cfg_device if raw_backend is None or str(raw_backend).strip().lower() == "auto" else str(raw_backend).strip().lower()
    if device not in VALID_DEVICES:
        raise AsrError("invalid_argument", f"不支持的推理设备: {device!r}")

    raw_chunk = req.get("chunk_seconds") if req.get("chunk_seconds") is not None else cfg.get("chunk_seconds", 300)
    chunk_seconds = parse_int(raw_chunk, "chunk_seconds", minimum=1, maximum=3600)
    max_concurrency = parse_int(cfg.get("max_concurrency", 1), "max_concurrency", minimum=1, maximum=16)
    params = TranscribeParams(
        funasr_path=funasr_path, ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path,
        model_file=model_file, vad_file=vad_file, device=device, chunk_seconds=chunk_seconds,
        audio_stream=parse_int(req.get("audio_stream", 0), "audio_stream", minimum=0, maximum=128),
        vad_maxseg_ms=parse_int(req.get("vad_maxseg_ms", 10000), "vad_maxseg_ms", minimum=500, maximum=60000),
        use_vad=bool(req.get("use_vad", True)), keep_tags=bool(req.get("keep_tags", False)),
        auto_fallback_cpu=bool(req.get("auto_fallback_cpu", False)), reset_cache=bool(req.get("reset_cache", False)),
        offset_ms=parse_int(req.get("offset_ms", 0), "offset_ms", minimum=-3_600_000, maximum=3_600_000),
        timeout_sec=parse_timeout_sec(req.get("timeout_ms"), default_ms=180000),
        output_srt_path=None, cache_root=resolve_cache_root(ctx),
    )
    return params, max_concurrency
