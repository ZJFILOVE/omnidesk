"""Offline tests for `context.collect.v1` and `ai.agent.v1` context refs."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from omnidesk_sdk import CapabilityResult  # noqa: E402
from magpie_agent_actions import handle_agent  # noqa: E402
from magpie_context import collect_context  # noqa: E402


@pytest.mark.asyncio
async def test_collect_context_combines_whitelisted_sources_and_redacts_secrets() -> None:
    async def fake_call(capability: str, payload: dict[str, Any]) -> CapabilityResult:
        assert capability in {"web.extract.v1", "book.source.v1"}
        if capability == "web.extract.v1":
            return CapabilityResult.ok(
                {"title": "Web title", "content": "web body", "token": "secret-token"},
                provider_plugin_id="plugin-chrome-web",
            )
        return CapabilityResult.ok(
            {"markdown": "# Chapter\nbook body", "cookie": "secret-cookie"},
            provider_plugin_id="plugin-ebook",
        )

    res = await collect_context(
        {
            "items": [
                {"capability": "web.extract.v1", "payload": {"url": "https://example.com"}, "label": "web"},
                {"capability": "book.source.v1", "payload": {"action": "list_books"}, "label": "book"},
            ]
        },
        call_capability=fake_call,
    )

    assert res.ok is True
    text = res.data["context"]
    assert "web body" in text
    assert "book body" in text
    assert "plugin-chrome-web" in str(res.data["items"])
    assert "secret-token" not in text
    assert "secret-cookie" not in text


@pytest.mark.asyncio
async def test_collect_context_rejects_non_whitelisted_capabilities() -> None:
    async def fake_call(capability: str, payload: dict[str, Any]) -> CapabilityResult:
        raise AssertionError("must not be called")

    res = await collect_context(
        {"items": [{"capability": "llm.chat.v1", "payload": {"messages": []}}]},
        call_capability=fake_call,
    )

    assert res.ok is False
    assert res.data["error_code"] == "context_unavailable"
    assert res.data["errors"][0]["error_code"] == "forbidden_capability"


@pytest.mark.asyncio
async def test_collect_context_keeps_partial_success() -> None:
    async def fake_call(capability: str, payload: dict[str, Any]) -> CapabilityResult:
        if capability == "subtitle.fetch.v1":
            return CapabilityResult.fail("字幕未找到", data={"error_code": "not_found"})
        return CapabilityResult.ok({"content": "ok body"})

    res = await collect_context(
        {
            "items": [
                {"capability": "web.fetch.v1", "payload": {"url": "https://example.com"}},
                {"capability": "subtitle.fetch.v1", "payload": {"query": "missing"}},
            ]
        },
        call_capability=fake_call,
    )

    assert res.ok is True
    assert "ok body" in res.data["context"]
    assert res.data["errors"][0]["error_code"] == "not_found"


@pytest.mark.asyncio
async def test_collect_context_passes_explicit_provider() -> None:
    captured: dict[str, Any] = {}

    async def fake_call(capability: str, payload: dict[str, Any], *, plugin_id: str | None = None) -> CapabilityResult:
        captured["capability"] = capability
        captured["provider"] = plugin_id
        return CapabilityResult.ok({"content": "pdf body"}, provider_plugin_id=plugin_id or "")

    res = await collect_context(
        {
            "items": [
                {
                    "capability": "book.source.v1",
                    "payload": {"action": "list_books"},
                    "provider_plugin_id": "plugin-pdf-babeldoc",
                }
            ]
        },
        call_capability=fake_call,
    )

    assert res.ok is True
    assert captured == {"capability": "book.source.v1", "provider": "plugin-pdf-babeldoc"}
    assert res.data["items"][0]["requested_provider_plugin_id"] == "plugin-pdf-babeldoc"


@pytest.mark.asyncio
async def test_agent_context_refs_are_collected_into_system_prompt() -> None:
    captured: dict[str, Any] = {}

    async def fake_chat(req: dict[str, Any], stream_sink: Any) -> CapabilityResult:
        captured.update(req)
        return CapabilityResult.ok({"content": "answer"})

    async def fake_collect(req: dict[str, Any]) -> CapabilityResult:
        captured["collect_req"] = req
        return CapabilityResult.ok({"context": "COLLECTED WEB CONTEXT"})

    res = await handle_agent(
        {
            "action": "qa",
            "prompt": "这篇网页讲了什么？",
            "context_refs": [{"capability": "web.extract.v1", "payload": {"url": "https://example.com"}}],
        },
        call_chat=fake_chat,
        secret=None,
        collect_context=fake_collect,
    )

    assert res.ok is True
    assert captured["collect_req"]["items"][0]["capability"] == "web.extract.v1"
    assert "COLLECTED WEB CONTEXT" in captured["messages"][0]["content"]


@pytest.mark.asyncio
async def test_agent_context_refs_fail_closed_without_collector() -> None:
    async def fake_chat(req: dict[str, Any], stream_sink: Any) -> CapabilityResult:
        raise AssertionError("must not be called")

    res = await handle_agent(
        {"prompt": "summarize", "context_refs": [{"capability": "web.extract.v1"}]},
        call_chat=fake_chat,
        secret=None,
    )

    assert res.ok is False
    assert res.data["error_code"] == "context_unavailable"


@pytest.mark.asyncio
async def test_media_context_uses_dedicated_provider_but_rejects_writes() -> None:
    calls = []
    async def fake_call(capability, payload, **kwargs):
        calls.append((capability, payload, kwargs))
        return CapabilityResult.ok({"chapters": [{"label": "Intro", "start_sec": 0}]})
    result = await collect_context({"items": [{"capability": "media.youtube.v1", "provider_plugin_id": "plugin-youtube", "payload": {"action": "fetch_chapters", "item_id": "video"}}]}, call_capability=fake_call)
    assert result.ok and calls[0][0] == "media.youtube.v1"
    calls.clear()
    denied = await collect_context({"items": [{"capability": "media.youtube.v1", "payload": {"action": "update_settings"}}]}, call_capability=fake_call)
    assert not denied.ok and not calls

@pytest.mark.asyncio
async def test_complete_text_and_snapshot_resume_are_lossless() -> None:
    from magpie_context import ContextSnapshots
    snapshots = ContextSnapshots()
    text = ('长段落 🐈 Unicode\n\n' * 18000) + '最后一段'
    calls = 0
    async def source(capability, payload):
        nonlocal calls
        calls += 1
        return CapabilityResult.ok({'text': text, 'title': 'Full page'})
    full = await collect_context({'items': [{'capability': 'web.extract.v1'}]}, call_capability=source)
    assert text in full.data['context'] and not full.data['has_more']
    result = await collect_context({'items': [{'capability': 'web.extract.v1'}], 'max_chars': 1001}, call_capability=source, snapshots=snapshots)
    chunks = [result.data['context']]
    first_cursor = result.data['next_cursor']
    while result.data['next_cursor']:
        result = await collect_context({'cursor': result.data['next_cursor'], 'max_chars': 1001}, call_capability=source, snapshots=snapshots)
        assert result.ok
        chunks.append(result.data['context'])
    assert ''.join(chunks) == full.data['context'] and calls == 2
    snapshots.clear()
    expired = await collect_context({'cursor': first_cursor}, call_capability=source, snapshots=snapshots)
    assert not expired.ok and expired.data['error_code'] == 'context_expired'


@pytest.mark.asyncio
async def test_images_are_multimodal_and_comment_context_readable() -> None:
    async def source(capability, payload):
        if capability == 'book.source.v1':
            assert payload['record_progress'] is False
            return CapabilityResult.ok({'blocks': [{'kind': 'image', 'image_url': 'data:image/png;base64,aW1n'}]})
        assert capability == 'text.comments.v1'
        return CapabilityResult.ok({'comments': [{'body': '读者评论', 'replies': [{'body': '嵌套回复'}]}]})
    async def collector(req):
        return await collect_context(req, call_capability=source)
    captured = {}
    async def chat(req, sink):
        captured.update(req)
        return CapabilityResult.ok({'reply': 'answer'})
    result = await handle_agent({'prompt': 'compare', 'context_refs': [
        {'capability': 'book.source.v1', 'payload': {'action': 'fetch_spread'}},
        {'capability': 'text.comments.v1', 'payload': {'action': 'fetch_comments'}},
    ]}, call_chat=chat, collect_context=collector, secret=None)
    assert result.ok
    assert '嵌套回复' in captured['messages'][0]['content']
    assert 'base64' not in captured['messages'][0]['content']
    assert captured['messages'][-1]['content'][1]['image_url']['url'].startswith('data:image/')


@pytest.mark.asyncio
@pytest.mark.parametrize('capability,payload', [
    ('web.fetch.v1', {'method': 'POST'}), ('book.source.v1', {'action': 'open'}),
    ('book.bilingual_pdf.v1', {'action': 'translate'}), ('text.comments.v1', {'action': 'post'}),
])
async def test_context_reader_cannot_execute_writes(capability, payload):
    async def source(*args):
        raise AssertionError('write must not execute')
    result = await collect_context({'items': [{'capability': capability, 'payload': payload}]}, call_capability=source)
    assert not result.ok and result.data['errors'][0]['error_code'] == 'forbidden_capability'
