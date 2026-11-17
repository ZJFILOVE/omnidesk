"""Offline tests for checkpoint resume, cancel process-group cleanup, clear_cache, path safety, concurrency, and PluginManager."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    PLUGIN_DIR,
    create_dummy_models,
    create_fake_toolchain,
    make_configured_context,
    read_calls,
    write_exec_script,
)
from omnidesk_core import OmnideskPaths, PluginManager
from omnidesk_sdk import invoke_lifecycle_hook
from plugin import create_plugin


@pytest.mark.asyncio
async def test_checkpoint_resume_emits_resumed_and_skips_completed_chunks(tmp_path: Path) -> None:
    calls_log = tmp_path / "calls.jsonl"
    state_flag = tmp_path / "fail_once.flag"
    state_flag.write_text("fail-on-chunk-2", encoding="utf-8")

    funasr_body = f"""
import json, sys
from pathlib import Path
args = sys.argv[1:]
Path({str(calls_log)!r}).open("a", encoding="utf-8").write(json.dumps({{"tool": "funasr", "args": args}}) + "\\n")
wav_name = Path(args[args.index("-a") + 1]).name
idx = int(wav_name.split("-")[1].split(".")[0])
if idx == 2 and Path({str(state_flag)!r}).exists():
    sys.stderr.write("simulated crash on chunk 2\\n")
    sys.exit(2)
# Intentionally emit slightly unordered timestamps within chunk to test sort merge too.
print(f"1\\n00:00:05,000 --> 00:00:06,000\\nChunk {{idx}} second\\n")
print(f"2\\n00:00:01,000 --> 00:00:02,000\\nChunk {{idx}} first\\n")
"""
    toolchain = create_fake_toolchain(tmp_path, duration=650.0, funasr_body=funasr_body)
    model_dir = create_dummy_models(tmp_path)
    media = tmp_path / "resume_demo.mkv"
    media.write_bytes(b"video-content")

    # Run 1: fails on chunk 2 after persisting chunks 0 and 1.
    ctx1 = make_configured_context(tmp_path, toolchain, model_dir)
    plugin1 = create_plugin()
    await invoke_lifecycle_hook(plugin1.on_load, ctx1)
    fail_res = await ctx1.capabilities.call_stream(
        "speech.asr.v1", {"action": "transcribe_media", "media_path": str(media)}
    )
    assert fail_res.ok is False
    job_id = fail_res.data["job_id"]
    ckpt_path = tmp_path / "data" / "asr-cache" / "jobs" / job_id / ".checkpoint.json"
    assert ckpt_path.is_file()
    ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
    assert ckpt["completed_chunks"] == [0, 1]
    assert ckpt["offset"] == 600.0
    assert len(ckpt["segments"]) == 4
    assert not media.with_suffix(".srt").exists()
    await invoke_lifecycle_hook(plugin1.on_unload)
    await ctx1.acleanup()

    # Clear flag & restart with fresh plugin instance.
    state_flag.unlink()
    calls_log.unlink(missing_ok=True)
    ctx2 = make_configured_context(tmp_path, toolchain, model_dir)
    plugin2 = create_plugin()
    await invoke_lifecycle_hook(plugin2.on_load, ctx2)

    stream_events: list[dict[str, Any]] = []
    res2 = await ctx2.capabilities.call_stream(
        "speech.asr.v1",
        {"action": "transcribe_media", "media_path": str(media)},
        on_chunk=lambda c: stream_events.append(c),
    )
    assert res2.ok is True
    assert res2.data["resumed"] is True
    assert res2.data["cue_count"] == 6

    resumed_events = [c for c in stream_events if c["event"] == "resumed"]
    assert len(resumed_events) == 1
    assert resumed_events[0]["data"]["completed_chunks"] == 2
    assert resumed_events[0]["data"]["offset"] == 600.0

    # Only chunk 2 should have been re-executed!
    resumed_calls = [c for c in read_calls(calls_log) if c["tool"] == "funasr"]
    assert len(resumed_calls) == 1
    assert "chunk-00002.wav" in resumed_calls[0]["args"][-1]

    # Verify merged cues are strictly sorted by start_ms across all 3 chunks.
    starts = [cue["start_ms"] for cue in res2.data["cues"]]
    assert starts == sorted(starts)
    assert starts == [1000, 5000, 301000, 305000, 601000, 605000]

    # reset_cache=True must re-run all 3 chunks without emitting resumed.
    calls_log.unlink(missing_ok=True)
    reset_events: list[dict[str, Any]] = []
    res_reset = await ctx2.capabilities.call_stream(
        "speech.asr.v1",
        {"action": "transcribe_media", "media_path": str(media), "reset_cache": True},
        on_chunk=lambda c: reset_events.append(c),
    )
    assert res_reset.ok is True
    assert res_reset.data["resumed"] is False
    assert all(c["event"] != "resumed" for c in reset_events)
    assert len([c for c in read_calls(calls_log) if c["tool"] == "funasr"]) == 3

    await invoke_lifecycle_hook(plugin2.on_unload)
    await ctx2.acleanup()


@pytest.mark.asyncio
async def test_cancel_terminates_process_group_and_updates_list_jobs(tmp_path: Path) -> None:
    parent_pid_file = tmp_path / "parent.pid"
    child_pid_file = tmp_path / "child.pid"
    funasr_body = f"""
import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(child_pid_file)!r}).write_text(str(child.pid), encoding="utf-8")
Path({str(parent_pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(60)
"""
    toolchain = create_fake_toolchain(tmp_path, duration=60.0, funasr_body=funasr_body)
    model_dir = create_dummy_models(tmp_path)
    ctx = make_configured_context(tmp_path, toolchain, model_dir)
    plugin = create_plugin()
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    media = tmp_path / "long.mp4"
    media.write_bytes(b"media")
    job_task = asyncio.create_task(
        ctx.capabilities.call_stream("speech.asr.v1", {"action": "transcribe_media", "media_path": str(media)})
    )

    for _ in range(100):
        if parent_pid_file.is_file() and child_pid_file.is_file():
            break
        await asyncio.sleep(0.02)
    assert parent_pid_file.is_file() and child_pid_file.is_file()
    parent_pid = int(parent_pid_file.read_text(encoding="utf-8"))
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))

    jobs_res = await ctx.capabilities.call("speech.asr.v1", {"action": "list_jobs", "status": "running"})
    assert jobs_res.ok is True
    assert jobs_res.data["count"] == 1
    job_id = jobs_res.data["jobs"][0]["job_id"]

    cancel_res = await ctx.capabilities.call("speech.asr.v1", {"action": "cancel", "job_id": job_id})
    assert cancel_res.ok is True
    assert cancel_res.data["cancelled"] is True

    task_res = await job_task
    assert task_res.ok is False
    assert task_res.data["error_code"] == "cancelled"

    # Both parent and spawned grandchild process in the process group must be gone.
    for pid in (parent_pid, child_pid):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    after_jobs = await ctx.capabilities.call("speech.asr.v1", {"action": "list_jobs"})
    assert after_jobs.data["jobs"][0]["status"] == "cancelled"
    assert after_jobs.data["active_count"] == 0

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_clear_cache_preserves_media_and_subtitles(tmp_path: Path) -> None:
    toolchain = create_fake_toolchain(tmp_path, duration=30.0)
    model_dir = create_dummy_models(tmp_path)
    ctx = make_configured_context(tmp_path, toolchain, model_dir)
    plugin = create_plugin()
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    media = tmp_path / "keep_me.mp4"
    media.write_bytes(b"original-media-bytes")
    res = await ctx.capabilities.call("speech.asr.v1", {"action": "transcribe_media", "media_path": str(media)})
    assert res.ok is True
    ckpt_path = Path(res.data["checkpoint_path"])
    srt_path = Path(res.data["output_srt_path"])
    assert ckpt_path.is_file()
    assert srt_path.is_file()

    clear_res = await ctx.capabilities.call("speech.asr.v1", {"action": "clear_cache"})
    assert clear_res.ok is True
    assert clear_res.data["removed_count"] == 1
    assert not ckpt_path.exists()
    assert media.is_file() and media.read_bytes() == b"original-media-bytes"
    assert srt_path.is_file() and "Chunk 0 line" in srt_path.read_text(encoding="utf-8")

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_output_path_escape_and_symlink_blocked(tmp_path: Path) -> None:
    toolchain = create_fake_toolchain(tmp_path, duration=10.0)
    model_dir = create_dummy_models(tmp_path)
    media_dir = tmp_path / "media"
    out_dir = tmp_path / "allowed_out"
    outside_dir = tmp_path / "outside"
    media_dir.mkdir()
    out_dir.mkdir()
    outside_dir.mkdir()
    media = media_dir / "clip.mp4"
    media.write_bytes(b"bytes")

    ctx = make_configured_context(tmp_path, toolchain, model_dir)
    plugin = create_plugin()
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    # Without output_root: cannot escape media_dir.
    for bad_out in (
        str(outside_dir / "evil.srt"),
        "../outside/evil.srt",
    ):
        res = await ctx.capabilities.call(
            "speech.asr.v1",
            {"action": "transcribe_media", "media_path": str(media), "output_srt_path": bad_out},
        )
        assert res.ok is False
        assert res.data["error_code"] == "path_safety"
        assert not (outside_dir / "evil.srt").exists()

    # Symlink escape inside media_dir rejected.
    sym_link = media_dir / "linked.srt"
    sym_link.symlink_to(outside_dir / "target.srt")
    sym_res = await ctx.capabilities.call(
        "speech.asr.v1",
        {"action": "transcribe_media", "media_path": str(media), "output_srt_path": str(sym_link)},
    )
    assert sym_res.ok is False
    assert sym_res.data["error_code"] == "path_safety"

    # With output_root configured: writes outside output_root (even in media_dir) rejected; inside allowed.
    ctx.set_config("output_root", str(out_dir))
    escape_res = await ctx.capabilities.call(
        "speech.asr.v1",
        {"action": "transcribe_media", "media_path": str(media), "output_srt_path": str(media_dir / "out.srt")},
    )
    assert escape_res.ok is False
    assert escape_res.data["error_code"] == "path_safety"

    ok_res = await ctx.capabilities.call(
        "speech.asr.v1",
        {"action": "transcribe_media", "media_path": str(media), "output_srt_path": "sub/good.srt"},
    )
    assert ok_res.ok is True
    assert Path(ok_res.data["output_srt_path"]) == (out_dir / "sub" / "good.srt").resolve()
    assert (out_dir / "sub" / "good.srt").is_file()

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_max_concurrency_and_log_redaction_and_plugin_manager(tmp_path: Path) -> None:
    lock_dir = tmp_path / "conc"
    lock_dir.mkdir()
    peak_file = tmp_path / "peak.txt"
    funasr_body = f"""
import os, sys, time
from pathlib import Path
lock_dir = Path({str(lock_dir)!r})
peak_file = Path({str(peak_file)!r})
my_lock = lock_dir / f"{{os.getpid()}}.lock"
my_lock.write_text("1", encoding="utf-8")
try:
    cur = len(list(lock_dir.glob("*.lock")))
    prev = int(peak_file.read_text(encoding="utf-8")) if peak_file.is_file() else 0
    if cur > prev:
        peak_file.write_text(str(cur), encoding="utf-8")
    time.sleep(0.08)
    print("1\\n00:00:00,500 --> 00:00:01,500\\nConcurrent ok\\n")
finally:
    my_lock.unlink(missing_ok=True)
"""
    toolchain = create_fake_toolchain(tmp_path, duration=10.0, funasr_body=funasr_body)
    model_dir = create_dummy_models(tmp_path)
    ctx = make_configured_context(tmp_path, toolchain, model_dir, extra_config={"max_concurrency": 1})
    plugin = create_plugin()
    await invoke_lifecycle_hook(plugin.on_load, ctx)

    media_files = []
    for i in range(3):
        f = tmp_path / f"item_{i}.mp4"
        f.write_bytes(f"data-{i}".encode())
        media_files.append(f)

    results = await asyncio.gather(
        *(ctx.capabilities.call("speech.asr.v1", {"action": "transcribe_media", "media_path": str(f)}) for f in media_files)
    )
    assert all(r.ok for r in results)
    assert int(peak_file.read_text(encoding="utf-8")) == 1

    # Secret / URL userinfo redaction in subprocess stderr errors.
    leaky_probe = write_exec_script(
        tmp_path / "leaky-ffprobe",
        "import sys; sys.stderr.write('failed https://alice:topsecret@nas.lan/v.mp4 Bearer sk-secret-token-999'); sys.exit(1)",
    )
    ctx.set_config("ffprobe_bin", str(leaky_probe))
    scrub_res = await ctx.capabilities.call("speech.asr.v1", {"action": "probe", "media_path": str(media_files[0])})
    assert scrub_res.ok is False
    assert "topsecret" not in scrub_res.error
    assert "sk-secret-token-999" not in scrub_res.error
    assert "[REDACTED]" in scrub_res.error

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()

    # Full PluginManager load/stream/bus/unload integration.
    paths = OmnideskPaths.from_env(
        {"XDG_CONFIG_HOME": str(tmp_path / "pm_cfg"), "XDG_DATA_HOME": str(tmp_path / "pm_data"), "XDG_RUNTIME_DIR": str(tmp_path / "pm_run")}
    )
    mgr = PluginManager(plugins_dir=PLUGIN_DIR.parent, paths=paths)
    rec = await mgr.load_plugin(PLUGIN_DIR)
    assert rec.plugin_id == "plugin-asr-funasr"
    assert mgr.capabilities.has("speech.asr.v1")
    unconf = await mgr.capabilities.call("speech.asr.v1", {"action": "status"})
    assert unconf.ok is False and unconf.data["error_code"] == "not_configured"

    # Configure via ConfigStore and run full streaming capability + bus checks through PluginManager.
    mgr.config_store.write(
        "plugin-asr-funasr",
        {
            "funasr_bin": str(toolchain["funasr"]),
            "ffmpeg_bin": str(toolchain["ffmpeg"]),
            "ffprobe_bin": str(toolchain["ffprobe"]),
            "model_dir": str(model_dir),
            "device": "vulkan",
            "chunk_seconds": 300,
            "max_concurrency": 1,
        },
    )
    pm_media = tmp_path / "pm_clip.mp4"
    pm_media.write_bytes(b"pm-bytes")
    pm_stream: list[dict[str, Any]] = []
    pm_bus: list[str] = []
    mgr.bus.subscribe("asr.job.*", lambda evt: pm_bus.append(evt.topic), plugin_id="test")
    pm_res = await mgr.capabilities.call_stream(
        "speech.asr.v1",
        {"action": "transcribe_media", "media_path": str(pm_media)},
        on_chunk=lambda c: pm_stream.append(c),
    )
    assert pm_res.ok is True
    assert pm_res.provider_plugin_id == "plugin-asr-funasr"
    assert any(c["event"] == "chunk" for c in pm_stream)
    assert "asr.job.progress" in pm_bus and "asr.job.completed" in pm_bus

    await mgr.unload_plugin("plugin-asr-funasr")
    assert not mgr.capabilities.has("speech.asr.v1")
