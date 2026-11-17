"""Offline tests for subtitle.translate.v1 (SRT/VTT, cache, split-retry, concurrency)."""

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

from conftest import load_sub_hunter_module  # noqa: E402
from omnidesk_sdk import CapabilityResult, PluginContext  # noqa: E402


def _manifest() -> dict[str, Any]:
    return json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))


def _make_ctx(tmp_path: Path) -> PluginContext:
    manifest = _manifest()
    return PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )


async def _loaded(tmp_path: Path) -> tuple[Any, PluginContext]:
    module = load_sub_hunter_module("plugin")
    ctx = _make_ctx(tmp_path)
    plugin = module.create_plugin()
    await plugin.on_load(ctx)
    return module, ctx


def _register_translate(ctx: PluginContext, calls: list[list[str]]) -> None:
    async def handler(req: dict[str, Any]) -> CapabilityResult:
        texts = list(req.get("texts") or [])
        calls.append(texts)
        return CapabilityResult.ok({"translations": [f"ZH:{text}" for text in texts]})

    ctx.capabilities.register("llm.translate.v1", handler, plugin_id="plugin-magpie-agent")


@pytest.mark.asyncio
async def test_translate_srt_bilingual_cache_and_status(tmp_path: Path) -> None:
    _, ctx = await _loaded(tmp_path)
    calls: list[list[str]] = []
    _register_translate(ctx, calls)

    source = tmp_path / "sample.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello world\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nSecond line\n",
        encoding="utf-8",
    )
    payload = {
        "action": "translate",
        "subtitle_path": str(source),
        "target_lang": "zh-CN",
        "bilingual": True,
    }

    status = await ctx.capabilities.call("subtitle.translate.v1", {"action": "status"})
    assert status.ok is True
    assert status.data["llm_translate_available"] is True
    assert status.data["translation_concurrency"] == 4
    assert status.data["output_mode"] == "bilingual"
    assert status.data["output_modes"] == ["bilingual", "translated", "original"]

    result = await ctx.capabilities.call("subtitle.translate.v1", payload)
    assert result.ok is True, result.error
    output = Path(result.data["output_path"])
    assert output.name == "sample.bilingual.srt"
    content = output.read_text(encoding="utf-8")
    assert "ZH:Hello world\nHello world" in content
    assert "ZH:Second line\nSecond line" in content
    assert [text for batch in calls for text in batch] == ["Hello world", "Second line"]

    first_call_count = len(calls)
    again = await ctx.capabilities.call("subtitle.translate.v1", payload)
    assert again.ok is True
    assert len(calls) == first_call_count
    assert again.data["called_batches"] == 0
    assert again.data["cached_count"] == 2


@pytest.mark.asyncio
async def test_translate_splits_invalid_batch_and_honors_concurrency_cap(tmp_path: Path) -> None:
    _, ctx = await _loaded(tmp_path)
    calls: list[list[str]] = []

    async def handler(req: dict[str, Any]) -> CapabilityResult:
        texts = list(req.get("texts") or [])
        calls.append(texts)
        if len(texts) > 1:
            return CapabilityResult.ok({"translations": ["only-one"]})
        return CapabilityResult.ok({"translations": [f"ZH:{texts[0]}"]})

    ctx.capabilities.register("llm.translate.v1", handler, plugin_id="plugin-magpie-agent")
    source = tmp_path / "split.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nAlpha\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nBeta\n",
        encoding="utf-8",
    )

    result = await ctx.capabilities.call(
        "subtitle.translate.v1",
        {
            "action": "translate",
            "subtitle_path": str(source),
            "translation_concurrency": 99,
        },
    )
    assert result.ok is True, result.error
    assert result.data["concurrency"] == 8
    assert calls[0] == ["Alpha", "Beta"]
    assert ["Alpha"] in calls and ["Beta"] in calls
    content = Path(result.data["output_path"]).read_text(encoding="utf-8")
    assert "ZH:Alpha\nAlpha" in content
    assert "ZH:Beta\nBeta" in content


@pytest.mark.asyncio
async def test_translate_vtt_and_reject_ass(tmp_path: Path) -> None:
    _, ctx = await _loaded(tmp_path)
    _register_translate(ctx, [])

    vtt = tmp_path / "captions.vtt"
    vtt.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n\n"
        "00:00:01.000 --> 00:00:02.000\nWorld\n",
        encoding="utf-8",
    )
    result = await ctx.capabilities.call(
        "subtitle.translate.v1",
        {
            "action": "translate",
            "subtitle_path": str(vtt),
            "target_lang": "zh-CN",
            "bilingual": False,
        },
    )
    assert result.ok is True, result.error
    output = Path(result.data["output_path"])
    assert output.name == "captions.zh.vtt"
    content = output.read_text(encoding="utf-8")
    assert content.startswith("WEBVTT")
    assert "ZH:Hello" in content and "ZH:World" in content
    assert "Hello\n" not in content.replace("ZH:Hello", "")

    original = await ctx.capabilities.call(
        "subtitle.translate.v1",
        {
            "action": "translate",
            "subtitle_path": str(vtt),
            "output_mode": "original",
        },
    )
    assert original.ok is True, original.error
    assert Path(original.data["output_path"]).resolve() == vtt.resolve()
    assert original.data["output_mode"] == "original"
    assert original.data["translated_count"] == 0

    ass = tmp_path / "captions.ass"
    ass.write_text("[Script Info]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello\n")
    rejected = await ctx.capabilities.call(
        "subtitle.translate.v1",
        {"action": "translate", "subtitle_path": str(ass)},
    )
    assert rejected.ok is False
    assert rejected.data["error_code"] == "unsupported_format"


@pytest.mark.asyncio
async def test_translate_requires_llm_and_protects_manual_output(tmp_path: Path) -> None:
    _, ctx = await _loaded(tmp_path)
    source = tmp_path / "manual.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

    missing = await ctx.capabilities.call(
        "subtitle.translate.v1",
        {"action": "translate", "subtitle_path": str(source)},
    )
    assert missing.ok is False
    assert missing.data["error_code"] == "capability_unavailable"

    _register_translate(ctx, [])
    existing = tmp_path / "manual.bilingual.srt"
    existing.write_text("manually edited\n", encoding="utf-8")
    protected = await ctx.capabilities.call(
        "subtitle.translate.v1",
        {"action": "translate", "subtitle_path": str(source)},
    )
    assert protected.ok is False
    assert protected.data["error_code"] == "conflict"
    assert existing.read_text(encoding="utf-8") == "manually edited\n"
