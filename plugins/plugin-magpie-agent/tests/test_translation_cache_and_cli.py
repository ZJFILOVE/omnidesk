"""Tests for translation cache, compact batch context, and CLI agent model forwarding."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from omnidesk_sdk import PluginContext, SubprocessResult

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import magpie_chat  # noqa: E402
from plugin import MagpieAgentPlugin  # noqa: E402


def test_batch_translate_request_does_not_duplicate_context_per_segment() -> None:
    req = magpie_chat._build_batch_translate_request(
        [
            {"id": "0", "text": "First paragraph.", "context_before": "Global before."},
            {"id": "1", "text": "Second paragraph."},
            {"id": "2", "text": "Third paragraph.", "context_after": "Global after."},
        ],
        sys_prompt="Translate",
        target_lang="zh-CN",
        source_lang="en",
        shared_ctx="Article Title",
        before_global="",
        after_global="",
        req={},
    )
    user_content = req["messages"][1]["content"]
    payload = json.loads(user_content.splitlines()[-1])
    assert payload["context"] == "Article Title"
    assert payload["context_before"] == "Global before."
    assert payload["context_after"] == "Global after."
    for seg in payload["segments"]:
        assert set(seg.keys()) == {"text"}


def test_handle_translate_caches_segments_and_supports_cache_lookup(tmp_path: Path) -> None:
    import asyncio

    calls: list[list[str]] = []

    async def fake_chat(chat_req: dict[str, Any], _stream: Any) -> Any:
        from omnidesk_sdk import CapabilityResult

        content = chat_req["messages"][1]["content"]
        if "<target_text>" in content:
            text = content.split("<target_text>", 1)[1].split("</target_text>", 1)[0].strip()
            calls.append([text])
            return CapabilityResult.ok({"reply": f"<translation>译:{text}</translation>", "served_model": "test-model"})
        payload = json.loads(content.splitlines()[-1])
        texts = [s["text"] for s in payload["segments"]]
        calls.append(texts)
        return CapabilityResult.ok(
            {"reply": json.dumps([f"译:{t}" for t in texts], ensure_ascii=False), "served_model": "test-model"}
        )

    plugin = MagpieAgentPlugin()
    ctx = PluginContext(plugin_id="plugin-magpie-agent", data_dir=tmp_path)
    plugin.ctx = ctx
    plugin._call_chat_via_ctx = fake_chat  # type: ignore[method-assign]

    async def run() -> None:
        first = await plugin.handle_translate(
            {
                "action": "translate",
                "target_lang": "zh-CN",
                "segments": [{"id": "a", "text": "Hello"}, {"id": "b", "text": "World"}],
            }
        )
        assert first.ok
        assert len(calls) == 1
        assert first.data["translations"] == ["译:Hello", "译:World"]

        lookup = await plugin.handle_translate(
            {
                "action": "cache_lookup",
                "target_lang": "zh-CN",
                "segments": [{"id": "a", "text": "Hello"}, {"id": "c", "text": "New"}],
            }
        )
        assert lookup.ok
        assert lookup.data["cache_hits"] == 1
        assert lookup.data["results"] == [{"index": 0, "id": "a", "source": "Hello", "translation": "译:Hello", "cached": True}]

        second = await plugin.handle_translate(
            {
                "action": "translate",
                "target_lang": "zh-CN",
                "segments": [{"id": "a", "text": "Hello"}, {"id": "c", "text": "New"}],
            }
        )
        assert second.ok
        assert second.data["cache_hits"] == 1
        assert second.data["translations"] == ["译:Hello", "译:New"]
        assert calls[1] == ["New"]

        all_hit = await plugin.handle_translate(
            {
                "action": "translate",
                "target_lang": "zh-CN",
                "segments": [{"id": "a", "text": "Hello"}, {"id": "c", "text": "New"}],
            }
        )
        assert all_hit.ok
        assert all_hit.data["cache_hits"] == 2
        assert len(calls) == 2

    asyncio.run(run())


def test_cli_agent_uses_explicit_model_and_ignores_group_route_override(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import asyncio
    import cli_chat

    recorded_args: list[list[str]] = []

    async def fake_exec(args: list[str], **_kwargs: Any) -> SubprocessResult:
        recorded_args.append(args)
        stdout = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "ok"}})
        return SubprocessResult(returncode=0, stdout=stdout, stderr="", timed_out=False, cancelled=False)

    monkeypatch.setattr(cli_chat, "binary", lambda _b, _c="": "/usr/bin/agy")
    ctx = PluginContext(plugin_id="plugin-magpie-agent", data_dir=tmp_path)
    ctx.exec_subprocess = fake_exec  # type: ignore[method-assign]

    asyncio.run(
        cli_chat.chat(
            {"messages": [{"role": "user", "content": "hi"}], "route_override": "group/omnidesk-active", "model": "gemini-4-carbon"},
            ctx=ctx,
            backend="agy",
        )
    )
    assert "--model" in recorded_args[0]
    idx = recorded_args[0].index("--model")
    assert recorded_args[0][idx + 1] == "gemini-4-carbon"

    asyncio.run(
        cli_chat.chat(
            {"messages": [{"role": "user", "content": "hi"}], "route_override": "group/omnidesk-active"},
            ctx=ctx,
            backend="agy",
        )
    )
    assert "--model" not in recorded_args[1]
