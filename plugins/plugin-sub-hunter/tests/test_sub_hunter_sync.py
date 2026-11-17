"""Offline tests for subtitle.sync.v1 (offset/scale linear sync, fake ffsubsync, and atomic protection)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import load_sub_hunter_module, write_fake_ffsubsync  # noqa: E402
from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402


async def _init_ctx(tmp_path: Path, cfg: dict[str, Any] | None = None) -> tuple[PluginContext, Any]:
    module = load_sub_hunter_module("plugin")
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    if cfg:
        ctx.set_config(cfg)
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    return ctx, plugin


@pytest.mark.asyncio
async def test_linear_offset_and_scale_srt_and_ass_with_manual_protection(tmp_path: Path) -> None:
    ctx, _ = await _init_ctx(tmp_path)
    manual_srt = tmp_path / "movie.srt"
    manual_srt.write_text(
        "1\n00:00:02,000 --> 00:00:04,000\nLine one\n\n2\n00:00:10,000 --> 00:00:12,000\nLine two\n",
        encoding="utf-8",
    )

    res = await ctx.capabilities.call(
        "subtitle.sync.v1",
        {"action": "shift", "subtitle_path": str(manual_srt), "offset_ms": 500, "scale": 1.5},
    )
    assert res.ok is True
    out_path = Path(res.data["output_path"])
    assert out_path == tmp_path / "movie.synced.srt"
    synced_txt = out_path.read_text(encoding="utf-8")
    assert "00:00:03,500 --> 00:00:06,500" in synced_txt
    assert "00:00:15,500 --> 00:00:18,500" in synced_txt
    assert "00:00:02,000 --> 00:00:04,000" in manual_srt.read_text(encoding="utf-8")

    blocked = await ctx.capabilities.call(
        "subtitle.sync.v1",
        {"action": "shift", "subtitle_path": str(manual_srt), "output_path": str(manual_srt), "offset_ms": 1000},
    )
    assert blocked.ok is False
    assert blocked.data["error_code"] == "conflict"
    assert "00:00:02,000 --> 00:00:04,000" in manual_srt.read_text(encoding="utf-8")

    ass_in = tmp_path / "clip.ass"
    ass_out = tmp_path / "clip.shifted.ass"
    ass_in.write_text(
        "[Script Info]\nTitle: Test\n\n[Events]\nFormat: Layer, Start, End, Style, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,Hello\n",
        encoding="utf-8",
    )
    ass_res = await ctx.capabilities.call(
        "subtitle.sync.v1",
        {"action": "shift", "subtitle_path": str(ass_in), "output_path": str(ass_out), "offset_ms": 1500, "scale": 1.0},
    )
    assert ass_res.ok is True
    assert "Dialogue: 0,0:00:02.50,0:00:04.50,Default,Hello" in ass_out.read_text(encoding="utf-8")

    bad_scale = await ctx.capabilities.call(
        "subtitle.sync.v1", {"action": "shift", "subtitle_path": str(manual_srt), "scale": 0.0}
    )
    assert bad_scale.ok is False
    assert bad_scale.data["error_code"] == "invalid_argument"

    vtt_in = tmp_path / "talk.vtt"
    vtt_out = tmp_path / "talk.synced.vtt"
    vtt_in.write_text("WEBVTT\n\n00:01.000 --> 00:03.000\nHello VTT\n", encoding="utf-8")
    vtt_res = await ctx.capabilities.call(
        "subtitle.sync.v1",
        {"action": "sync", "mode": "auto", "subtitle_path": str(vtt_in), "output_path": str(vtt_out), "offset_ms": 500},
    )
    assert vtt_res.ok is True
    assert "00:00:01.500 --> 00:00:03.500" in vtt_out.read_text(encoding="utf-8")

    # overwrite_policy='overwrite' allows replacing existing file atomically
    ctx.set_config("overwrite_policy", "overwrite")
    ov_res = await ctx.capabilities.call(
        "subtitle.sync.v1",
        {"action": "shift", "subtitle_path": str(manual_srt), "output_path": str(manual_srt), "offset_ms": 1000},
    )
    assert ov_res.ok is True
    assert "00:00:03,000 --> 00:00:05,000" in manual_srt.read_text(encoding="utf-8")
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_ffsubsync_missing_and_fake_subprocess_execution(tmp_path: Path) -> None:
    ref_media = tmp_path / "episode.mkv"
    ref_media.write_bytes(b"fake-media")
    sub_file = tmp_path / "episode.srt"
    sub_file.write_text("1\n00:00:01,000 --> 00:00:02,000\nRaw\n", encoding="utf-8")

    ctx, _ = await _init_ctx(tmp_path, {"ffsubsync_bin": str(tmp_path / "missing-ffsubsync")})
    st = await ctx.capabilities.call("subtitle.sync.v1", {"action": "status"})
    assert st.ok is True
    assert st.data["ffsubsync_found"] is False

    missing_res = await ctx.capabilities.call(
        "subtitle.sync.v1",
        {"action": "sync", "mode": "ffsubsync", "subtitle_path": str(sub_file), "reference_path": str(ref_media)},
    )
    assert missing_res.ok is False
    assert missing_res.data["error_code"] == "not_configured"

    args_log = tmp_path / "argv.json"
    fake_bin = write_fake_ffsubsync(
        tmp_path,
        f"""
import json, sys
from pathlib import Path
Path({str(args_log)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
out_idx = sys.argv.index("-o") + 1
Path(sys.argv[out_idx]).write_text("1\\n00:00:02,500 --> 00:00:03,500\\nAligned\\n", encoding="utf-8")
""",
    )
    ctx.set_config("ffsubsync_bin", str(fake_bin))
    out_file = tmp_path / "episode.aligned.srt"
    sync_res = await ctx.capabilities.call(
        "subtitle.sync.v1",
        {
            "action": "sync",
            "subtitle_path": str(sub_file),
            "reference_path": str(ref_media),
            "output_path": str(out_file),
            "audio_stream": 1,
        },
    )
    assert sync_res.ok is True
    assert "00:00:02,500 --> 00:00:03,500" in out_file.read_text(encoding="utf-8")
    recorded_argv = json.loads(args_log.read_text(encoding="utf-8"))
    assert recorded_argv[0] == str(ref_media.resolve())
    assert "-i" in recorded_argv and "-o" in recorded_argv
    assert "--reference-stream" in recorded_argv and "a:1" in recorded_argv
    assert not list(tmp_path.glob("*.tmp*"))

    slow_bin = write_fake_ffsubsync(tmp_path, "import time; time.sleep(2.0)", name="slow-ffsubsync")
    ctx.set_config("ffsubsync_bin", str(slow_bin))
    to_res = await ctx.capabilities.call(
        "subtitle.sync.v1",
        {
            "action": "sync",
            "subtitle_path": str(sub_file),
            "reference_path": str(ref_media),
            "output_path": str(tmp_path / "episode.timeout.srt"),
            "timeout_sec": 0.1,
        },
    )
    assert to_res.ok is False
    assert to_res.data["error_code"] == "timeout"
    await ctx.acleanup()
