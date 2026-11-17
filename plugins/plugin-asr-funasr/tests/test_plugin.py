"""Offline contract tests for manifest, status, probe, chunked ASR, SRT merge, and events."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    PLUGIN_DIR,
    REPO_ROOT,
    create_dummy_models,
    create_fake_toolchain,
    make_configured_context,
    read_calls,
    write_exec_script,
)
from omnidesk_sdk import BasePlugin, PluginContext, invoke_lifecycle_hook
from plugin import create_plugin


def test_manifest_matches_schema_and_spec() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["id"] == "plugin-asr-funasr"
    assert manifest["kind"] == "provider"
    assert manifest["domain"] == "core"
    assert manifest["entry"] == "plugin:create_plugin"
    assert manifest["provides"] == ["speech.asr.v1"]
    assert set(manifest.keys()).issubset(set(schema["properties"].keys()))

    fields = {item["key"]: item for item in manifest["config_schema"]}
    assert {
        "funasr_bin", "model_dir", "device", "ffmpeg_bin",
        "ffprobe_bin", "chunk_seconds", "output_root", "max_concurrency",
    }.issubset(fields.keys())
    assert fields["model_dir"]["default"] == ""
    assert fields["device"]["type"] == "select"
    assert fields["device"]["options"] == ["vulkan", "cpu"]
    assert fields["chunk_seconds"]["default"] == 300
    assert re.match(schema["properties"]["provides"]["items"]["pattern"], "speech.asr.v1")


@pytest.mark.asyncio
async def test_unconfigured_fast_load_and_fail_closed_status(tmp_path: Path) -> None:
    plugin = create_plugin()
    assert isinstance(plugin, BasePlugin)
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    marker = tmp_path / "funasr-invoked.txt"
    sentinel_funasr = write_exec_script(
        tmp_path / "fake-funasr",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('CALLED')\n",
    )

    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        config_schema=manifest["config_schema"],
    )
    t0 = time.perf_counter()
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 50.0
    assert ctx.capabilities.has("speech.asr.v1")

    health = await invoke_lifecycle_hook(plugin.on_health_check)
    assert health["ok"] is True
    assert health["configured"] is False

    # Default unconfigured state (model_dir == "") fails closed immediately without spawning model.
    status_res = await ctx.capabilities.call("speech.asr.v1", {"action": "status"})
    assert status_res.ok is False
    assert status_res.data["error_code"] == "not_configured"
    assert not marker.exists()

    media = tmp_path / "sample.mp4"
    media.write_bytes(b"placeholder")
    for act, extra in (("transcribe_media", {"media_path": str(media)}), ("transcribe_audio", {"audio_path": str(media)})):
        res = await ctx.capabilities.call("speech.asr.v1", {"action": act, **extra})
        assert res.ok is False
        assert res.data["error_code"] == "not_configured"

    # Configured funasr_bin but still empty model_dir -> status must NOT spawn funasr_bin.
    ctx.set_config("funasr_bin", str(sentinel_funasr))
    status_res2 = await ctx.capabilities.call("speech.asr.v1", {"action": "status"})
    assert status_res2.ok is False
    assert status_res2.data["error_code"] == "not_configured"
    assert not marker.exists()

    # Configured model_dir + missing executable -> service_offline.
    model_dir = create_dummy_models(tmp_path)
    ctx.set_config({"model_dir": str(model_dir), "funasr_bin": str(tmp_path / "missing-funasr")})
    offline_res = await ctx.capabilities.call("speech.asr.v1", {"action": "status"})
    assert offline_res.ok is False
    assert offline_res.data["error_code"] == "service_offline"
    assert offline_res.data["funasr_available"] is False

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_probe_list_streams_and_error_mapping(tmp_path: Path) -> None:
    toolchain = create_fake_toolchain(tmp_path, duration=125.5)
    model_dir = create_dummy_models(tmp_path)
    ctx = make_configured_context(tmp_path, toolchain, model_dir)
    plugin = create_plugin()
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    media = tmp_path / "movie.mkv"
    media.write_bytes(b"fake-mkv")

    probe_res = await ctx.capabilities.call("speech.asr.v1", {"action": "probe", "media_path": str(media)})
    assert probe_res.ok is True
    assert probe_res.data["duration_sec"] == 125.5
    assert probe_res.data["sample_rate"] == 48000
    assert probe_res.data["channels"] == 2
    assert probe_res.data["codec_name"] == "aac"
    assert probe_res.data["audio_stream_count"] == 1

    streams_res = await ctx.capabilities.call(
        "speech.asr.v1", {"action": "list_streams", "media_path": str(media), "stream_type": "audio"}
    )
    assert streams_res.ok is True
    assert streams_res.data["count"] == 1
    assert streams_res.data["streams"][0]["language"] == "en"

    # Missing file -> invalid_argument
    missing_res = await ctx.capabilities.call(
        "speech.asr.v1", {"action": "probe", "media_path": str(tmp_path / "nope.mkv")}
    )
    assert missing_res.ok is False
    assert missing_res.data["error_code"] == "invalid_argument"

    # Corrupt media (ffprobe rc=1) -> invalid_argument
    bad_probe = write_exec_script(tmp_path / "bad-ffprobe", "import sys; sys.stderr.write('Invalid data'); sys.exit(1)")
    ctx.set_config("ffprobe_bin", str(bad_probe))
    corrupt_res = await ctx.capabilities.call("speech.asr.v1", {"action": "probe", "media_path": str(media)})
    assert corrupt_res.ok is False
    assert corrupt_res.data["error_code"] == "invalid_argument"

    # Hanging ffprobe -> timeout (process group killed quickly)
    slow_probe = write_exec_script(tmp_path / "slow-ffprobe", "import time; time.sleep(10)")
    ctx.set_config("ffprobe_bin", str(slow_probe))
    timeout_res = await ctx.capabilities.call(
        "speech.asr.v1", {"action": "probe", "media_path": str(media), "timeout_ms": 80}
    )
    assert timeout_res.ok is False
    assert timeout_res.data["error_code"] == "timeout"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_chunked_transcribe_media_srt_merge_and_bus_events(tmp_path: Path) -> None:
    # 650s with default chunk_seconds=300 -> 3 chunks (0..300, 300..600, 600..650).
    toolchain = create_fake_toolchain(tmp_path, duration=650.0)
    model_dir = create_dummy_models(tmp_path)
    ctx = make_configured_context(tmp_path, toolchain, model_dir)
    plugin = create_plugin()

    progress_events: list[dict[str, Any]] = []
    completed_events: list[dict[str, Any]] = []
    ctx.bus.subscribe("asr.job.progress", lambda evt: progress_events.append(evt.payload), plugin_id="test")
    ctx.bus.subscribe("asr.job.completed", lambda evt: completed_events.append(evt.payload), plugin_id="test")
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # Check status ok without invoking funasr.
    status_res = await ctx.capabilities.call("speech.asr.v1", {"action": "status"})
    assert status_res.ok is True
    assert status_res.data["ready"] is True
    assert status_res.data["device"] == "vulkan"
    assert read_calls(toolchain["calls_log"]) == []

    media = tmp_path / "lecture.mp4"
    media.write_bytes(b"video-bytes")
    st = media.stat()
    expected_job_id = hashlib.md5(f"{media.resolve()}|{st.st_mtime_ns}|{st.st_size}|300".encode()).hexdigest()
    alt_job_id = hashlib.md5(f"{media.resolve()}|{st.st_mtime_ns}|{st.st_size}|120".encode()).hexdigest()
    assert expected_job_id != alt_job_id

    stream_chunks: list[dict[str, Any]] = []
    res = await ctx.capabilities.call_stream(
        "speech.asr.v1",
        {"action": "transcribe_media", "media_path": str(media)},
        on_chunk=lambda c: stream_chunks.append(c),
    )
    assert res.ok is True
    assert res.data["job_id"] == expected_job_id
    assert res.data["total_chunks"] == 3
    assert res.data["completed_chunks"] == 3
    assert res.data["cue_count"] == 3
    assert res.data["resumed"] is False

    # Verify chunk durations & funasr CLI flags.
    calls = read_calls(toolchain["calls_log"])
    ff_calls = [c["args"] for c in calls if c["tool"] == "ffmpeg"]
    asr_calls = [c["args"] for c in calls if c["tool"] == "funasr"]
    assert len(ff_calls) == 3
    assert len(asr_calls) == 3
    assert ff_calls[0][ff_calls[0].index("-ss") + 1] == "0.000"
    assert ff_calls[0][ff_calls[0].index("-t") + 1] == "300.000"
    assert ff_calls[1][ff_calls[1].index("-ss") + 1] == "300.000"
    assert ff_calls[2][ff_calls[2].index("-ss") + 1] == "600.000"
    assert ff_calls[2][ff_calls[2].index("-t") + 1] == "50.000"
    assert "--backend" in asr_calls[0] and asr_calls[0][asr_calls[0].index("--backend") + 1] == "vulkan"
    assert "--srt" in asr_calls[0]
    assert "-m" in asr_calls[0] and str(model_dir / "sensevoice-small-q8.gguf") in asr_calls[0]

    # Verify SRT written atomically alongside media with exact shifted timestamps & empty cue stripped.
    srt_path = media.with_suffix(".srt")
    assert srt_path.is_file()
    assert res.data["output_srt_path"] == str(srt_path)
    srt_text = srt_path.read_text(encoding="utf-8")
    assert "00:00:01,500 --> 00:00:03,250\nChunk 0 line" in srt_text
    assert "00:05:01,500 --> 00:05:03,250\nChunk 1 line" in srt_text
    assert "00:10:01,500 --> 00:10:03,250\nChunk 2 line" in srt_text
    assert "<|en|>" not in srt_text
    assert "<|NOSPEECH|>" not in srt_text

    # Verify stream stage/chunk events & bus events.
    event_names = [c["event"] for c in stream_chunks]
    assert "stage" in event_names
    assert event_names.count("chunk") == 3
    assert len(progress_events) == 3
    assert [p["completed_chunks"] for p in progress_events] == [1, 2, 3]
    assert all(p["total_chunks"] == 3 for p in progress_events)
    assert progress_events[-1]["progress"] == 1.0
    assert len(completed_events) == 1
    assert completed_events[0] == {
        "job_id": expected_job_id,
        "media_path": str(media.resolve()),
        "output_srt_path": str(srt_path),
        "cue_count": 3,
    }

    # Verify transcribe_audio with base64 PCM (4s audio) and audio_path both work.
    pcm_b64 = base64.b64encode(b"\x00\x00" * 16000 * 4).decode("ascii")
    pcm_res = await ctx.capabilities.call(
        "speech.asr.v1",
        {"action": "transcribe_audio", "pcm_s16le_base64": pcm_b64, "sample_rate": 16000, "channels": 1},
    )
    assert pcm_res.ok is True
    assert pcm_res.data["cue_count"] == 1
    assert pcm_res.data["output_srt_path"] == ""

    audio_file = tmp_path / "voice.wav"
    audio_file.write_bytes(b"RIFF-audio")
    audio_res = await ctx.capabilities.call(
        "speech.asr.v1",
        {"action": "transcribe_audio", "audio_path": str(audio_file), "backend": "cpu", "keep_tags": True},
    )
    assert audio_res.ok is True
    assert audio_res.data["device"] == "cpu"
    assert audio_file.with_suffix(".srt").is_file()
    assert "<|en|>" in audio_file.with_suffix(".srt").read_text(encoding="utf-8")

    bad_stream_res = await ctx.capabilities.call(
        "speech.asr.v1",
        {"action": "transcribe_audio", "audio_path": str(audio_file), "audio_stream": 5},
    )
    assert bad_stream_res.ok is False
    assert bad_stream_res.data["error_code"] == "invalid_argument"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
