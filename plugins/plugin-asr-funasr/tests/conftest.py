"""Offline fake binary fixtures and helpers for plugin-asr-funasr tests."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "core") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "core"))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402


def write_exec_script(path: Path, body: str) -> Path:
    """Create an executable Python script acting as a fake external CLI tool."""
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(stat.S_IRWXU)
    return path


def create_dummy_models(base_dir: Path, *, with_vad: bool = True) -> Path:
    """Create placeholder GGUF model files under `base_dir` without real weights."""
    model_dir = base_dir / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "sensevoice-small-q8.gguf").write_bytes(b"GGUF-PLACEHOLDER")
    if with_vad:
        (model_dir / "fsmn-vad.gguf").write_bytes(b"VAD-PLACEHOLDER")
    return model_dir


def create_fake_toolchain(
    tmp_path: Path,
    *,
    duration: float = 650.0,
    funasr_body: str | None = None,
    ffmpeg_body: str | None = None,
    ffprobe_body: str | None = None,
) -> dict[str, Path]:
    """Build fake ffprobe, ffmpeg, and llama-funasr-sensevoice executables in tmp_path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls_log = tmp_path / "calls.jsonl"

    probe_script = ffprobe_body or f"""
import json, sys
from pathlib import Path
Path({str(calls_log)!r}).open("a", encoding="utf-8").write(json.dumps({{"tool": "ffprobe", "args": sys.argv[1:]}}) + "\\n")
print(json.dumps({{
    "format": {{"duration": "{duration}", "format_name": "matroska,webm", "size": "123456"}},
    "streams": [
        {{"index": 0, "codec_type": "video", "codec_name": "h264"}},
        {{"index": 1, "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2, "channel_layout": "stereo", "tags": {{"language": "en", "title": "English"}}}},
        {{"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {{"language": "zh"}}}}
    ]
}}))
"""

    ffmpeg_script = ffmpeg_body or f"""
import json, sys
from pathlib import Path
args = sys.argv[1:]
Path({str(calls_log)!r}).open("a", encoding="utf-8").write(json.dumps({{"tool": "ffmpeg", "args": args}}) + "\\n")
out_wav = Path(args[-1])
out_wav.parent.mkdir(parents=True, exist_ok=True)
out_wav.write_bytes(b"RIFF" + b"\\x00" * 40)
"""

    asr_script = funasr_body or f"""
import json, sys
from pathlib import Path
args = sys.argv[1:]
Path({str(calls_log)!r}).open("a", encoding="utf-8").write(json.dumps({{"tool": "funasr", "args": args}}) + "\\n")
wav_name = Path(args[args.index("-a") + 1]).name
idx = int(wav_name.split("-")[1].split(".")[0])
print(f"1\\n00:00:01,500 --> 00:00:03,250\\n<|en|><|NEUTRAL|>Chunk {{idx}} line\\n")
print(f"2\\n00:00:04,000 --> 00:00:05,000\\n<|NOSPEECH|>   \\n")
"""

    return {
        "ffprobe": write_exec_script(bin_dir / "ffprobe", probe_script),
        "ffmpeg": write_exec_script(bin_dir / "ffmpeg", ffmpeg_script),
        "funasr": write_exec_script(bin_dir / "llama-funasr-sensevoice", asr_script),
        "calls_log": calls_log,
    }


def make_configured_context(
    tmp_path: Path,
    toolchain: dict[str, Path],
    model_dir: Path,
    *,
    extra_config: dict[str, Any] | None = None,
) -> PluginContext:
    """Construct a PluginContext isolated in tmp_path with configured binaries and models."""
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        config_schema=manifest["config_schema"],
    )
    cfg: dict[str, Any] = {
        "funasr_bin": str(toolchain["funasr"]),
        "ffmpeg_bin": str(toolchain["ffmpeg"]),
        "ffprobe_bin": str(toolchain["ffprobe"]),
        "model_dir": str(model_dir),
        "device": "vulkan",
        "chunk_seconds": 300,
        "output_root": "",
        "max_concurrency": 1,
    }
    if extra_config:
        cfg.update(extra_config)
    ctx.set_config(cfg)
    return ctx


def read_calls(calls_log: Path) -> list[dict[str, Any]]:
    if not calls_log.is_file():
        return []
    return [json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines() if line.strip()]
