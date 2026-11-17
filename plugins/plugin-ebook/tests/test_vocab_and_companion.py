"""Offline tests for vocab.sm2.v1, companion actions, clip_jump, and book.sync."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import PLUGIN_DIR, build_minimal_epub, load_plugin_module  # noqa: E402
from omnidesk_sdk import CapabilityResult, PluginContext, VocabCard, invoke_lifecycle_hook  # noqa: E402


async def _setup_plugin(tmp_path: Path, *, ctx: PluginContext | None = None) -> tuple[Any, PluginContext]:
    mod = load_plugin_module()
    plugin = mod.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    if ctx is None:
        ctx = PluginContext(
            manifest["id"],
            plugin_dir=PLUGIN_DIR,
            config_dir=tmp_path / "cfg",
            config_schema=manifest["config_schema"],
        )
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    return plugin, ctx


@pytest.mark.asyncio
async def test_vocab_xdg_persistence_0600_and_symlink_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg_data = tmp_path / "custom-xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    plugin, ctx = await _setup_plugin(tmp_path)

    res = await ctx.capabilities.call(
        "vocab.sm2.v1",
        {"action": "add", "word": "Ephemeral", "meaning": "短暂的", "etymology": "epi + hemera"},
    )
    assert res.ok is True
    assert res.data["created"] is True

    expected_path = xdg_data / "omnidesk" / "vocab" / "plugin-ebook.json"
    assert expected_path.is_file()
    assert stat.S_IMODE(expected_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(expected_path.parent.stat().st_mode) == 0o700

    # BookSource.list_vocab returns validated VocabCard models
    cards = await ctx.domains.book_sources()[0].list_vocab()
    assert len(cards) == 1
    assert isinstance(cards[0], VocabCard)
    assert cards[0].word == "ephemeral"

    # Symlink target refusal
    expected_path.unlink()
    evil_target = tmp_path / "evil.json"
    evil_target.write_text("{}", encoding="utf-8")
    os.symlink(evil_target, expected_path)

    res_sym = await ctx.capabilities.call("vocab.sm2.v1", {"action": "list"})
    assert res_sym.ok is False
    assert res_sym.data["error_code"] == "path_safety"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_vocab_case_normalization_sm2_formula_and_due_queue(tmp_path: Path) -> None:
    plugin, ctx = await _setup_plugin(tmp_path)
    vocab_store = plugin.vocab_store

    # Case-insensitive normalization: Ephemeral -> EPHEMERAL -> ephemeral
    r1 = await ctx.capabilities.call("vocab.sm2.v1", {"action": "add", "word": "  Ephemeral  ", "meaning": "短暂的"})
    assert r1.ok is True and r1.data["created"] is True
    assert r1.data["card"]["word"] == "ephemeral"

    # Quality 4 (good): reps 0->1 (interval=1), then 1->2 (interval=6)
    rev1 = await ctx.capabilities.call("vocab.sm2.v1", {"action": "review", "word": "EPHEMERAL", "quality": 4})
    assert rev1.ok is True
    assert rev1.data["card"]["repetitions"] == 1
    assert rev1.data["card"]["interval_days"] == 1
    assert rev1.data["card"]["ease_factor"] == 2.5

    # Re-adding same word with different case must NOT reset SM-2 progress
    r_dup = await ctx.capabilities.call(
        "vocab.sm2.v1",
        {"action": "add", "word": "ePhEmErAl", "etymology": "epi (on) + hemera (day)"},
    )
    assert r_dup.ok is True
    assert r_dup.data["created"] is False
    assert r_dup.data["already_exists"] is True
    assert r_dup.data["card"]["repetitions"] == 1
    assert r_dup.data["card"]["etymology"] == "epi (on) + hemera (day)"

    rev2 = await ctx.capabilities.call("vocab.sm2.v1", {"action": "review", "word": "ephemeral", "quality": 4})
    assert rev2.ok is True
    assert rev2.data["card"]["repetitions"] == 2
    assert rev2.data["card"]["interval_days"] == 6
    assert rev2.data["card"]["ease_factor"] == 2.5

    # Quality 5 (easy): EF = 2.5 + 0.1 = 2.6, interval = round(6 * 2.6) = 16
    rev3 = await ctx.capabilities.call("vocab.sm2.v1", {"action": "review", "word": "Ephemeral", "quality": 5})
    assert rev3.ok is True
    assert rev3.data["card"]["repetitions"] == 3
    assert rev3.data["card"]["ease_factor"] == 2.6
    assert rev3.data["card"]["interval_days"] == 16

    # Not due now, but due after +17 days
    due_now = await ctx.capabilities.call("vocab.sm2.v1", {"action": "due"})
    assert due_now.ok is True and due_now.data["count"] == 0
    future = (datetime.now(UTC) + timedelta(days=17)).isoformat()
    due_future = await ctx.capabilities.call("vocab.sm2.v1", {"action": "due", "as_of": future})
    assert due_future.ok is True and due_future.data["count"] == 1

    # Quality 3 (hard) lowers EF by 0.14 on a fresh card
    await ctx.capabilities.call("vocab.sm2.v1", {"action": "add", "word": "Telemetry"})
    rev_hard = await ctx.capabilities.call("vocab.sm2.v1", {"action": "review", "word": "telemetry", "quality": 3})
    assert rev_hard.ok is True
    assert rev_hard.data["card"]["ease_factor"] == 2.36
    assert rev_hard.data["card"]["repetitions"] == 1
    assert rev_hard.data["card"]["interval_days"] == 1

    # Quality 1 (again) resets repetitions to 0, keeps interval=1, clamps EF >= 1.3, stays due immediately
    rev_again = await ctx.capabilities.call("vocab.sm2.v1", {"action": "review", "word": "ephemeral", "quality": 1})
    assert rev_again.ok is True
    assert rev_again.data["card"]["repetitions"] == 0
    assert rev_again.data["card"]["interval_days"] == 1
    assert rev_again.data["card"]["ease_factor"] == 2.06

    due_after_again = await ctx.capabilities.call("vocab.sm2.v1", {"action": "due"})
    assert [c["word"] for c in due_after_again.data["cards"]] == ["ephemeral"]

    # Repeated quality=1 clamps EF at 1.3
    for _ in range(5):
        vocab_store.review_card("ephemeral", 1)
    assert vocab_store.get_card("ephemeral")["ease_factor"] == 1.3

    # Invalid quality rejected
    for bad_q in (0, 2, 6, True, "unknown"):
        bad_res = await ctx.capabilities.call("vocab.sm2.v1", {"action": "review", "word": "ephemeral", "quality": bad_q})
        assert bad_res.ok is False and bad_res.data["error_code"] == "invalid_argument"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_vocab_idempotent_import_export_and_bus_add(tmp_path: Path) -> None:
    plugin, ctx = await _setup_plugin(tmp_path)

    await ctx.capabilities.call("vocab.sm2.v1", {"action": "add", "word": "Epistemic", "meaning": "认知的"})
    await ctx.capabilities.call("vocab.sm2.v1", {"action": "review", "word": "epistemic", "quality": 4})
    await ctx.capabilities.call("vocab.sm2.v1", {"action": "review", "word": "epistemic", "quality": 5})

    imp = await ctx.capabilities.call(
        "vocab.sm2.v1",
        {
            "action": "import",
            "cards": [
                {"word": "EPISTEMIC", "repetitions": 0, "interval_days": 1, "ease_factor": 2.5, "meaning": "should not overwrite"},
                {"word": "Idempotent", "meaningZh": "幂等的", "bookTitle": "DDIA", "contextSentence": "Idempotent ops."},
                {"word": "idempotent", "meaning": "duplicate in batch"},
                {"word": "   "},
            ],
        },
    )
    assert imp.ok is True
    assert imp.data["imported"] == 1
    assert imp.data["skipped_existing"] == 2
    assert imp.data["skipped_invalid"] == 1
    assert imp.data["total_cards"] == 2

    # Existing progress preserved untouched
    epistemic = (await ctx.capabilities.call("vocab.sm2.v1", {"action": "get", "word": "epistemic"})).data["card"]
    assert epistemic["repetitions"] == 2
    assert epistemic["interval_days"] == 6
    assert epistemic["ease_factor"] == 2.6
    assert epistemic["meaning"] == "认知的"

    # EntityBus "entity.vocab.add" adds without clobbering existing cards
    await ctx.bus.publish(
        "entity.vocab.add",
        {"word": "Provenance", "context_sentence": "Provenance tracks origin.", "source_title": "Daily"},
    )
    await ctx.bus.publish("entity.vocab.add", {"word": "Epistemic", "context_sentence": "ignored"})
    exp = await ctx.capabilities.call("vocab.sm2.v1", {"action": "export"})
    assert exp.ok is True and exp.data["count"] == 3
    words = {c["word"]: c for c in exp.data["cards"]}
    assert words["provenance"]["source_book"] == "Daily"
    assert words["epistemic"]["repetitions"] == 2
    assert words["idempotent"]["meaning"] == "幂等的"
    assert words["idempotent"]["source_book"] == "DDIA"
    assert words["idempotent"]["context_sentence"] == "Idempotent ops."

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_lookup_term_and_explain_sentence_graceful_degradation_and_llm(tmp_path: Path) -> None:
    plugin, ctx = await _setup_plugin(tmp_path)

    # 1. Offline mode (no llm.chat.v1, no media.clip_jump.v1)
    res_lookup = await ctx.capabilities.call(
        "book.source.v1",
        {
            "action": "lookup_term",
            "word": "Telemetry",
            "context_sentence": "Telemetry from the floor sounded reassuring.",
            "source_book": "Severance",
        },
    )
    assert res_lookup.ok is True
    assert res_lookup.data["word"] == "telemetry"
    assert res_lookup.data["text_only"] is True
    assert res_lookup.data["llm_used"] is False
    assert res_lookup.data["clips"] == []
    assert res_lookup.data["clip_jump_available"] is False
    assert "tele-" in res_lookup.data["etymology"]

    res_explain = await ctx.capabilities.call(
        "book.source.v1",
        {
            "action": "explain_sentence",
            "sentence": "Although telemetry sounded reassuring, it left an epistemic gap, which persisted across deployments.",
        },
    )
    assert res_explain.ok is True
    assert res_explain.data["text_only"] is True
    assert res_explain.data["llm_used"] is False
    assert res_explain.data["clause_count"] >= 2
    assert "although" in res_explain.data["connectors"]
    assert "telemetry" in res_explain.data["key_terms"]

    # 2. Register optional llm.chat.v1 and media.clip_jump.v1
    ctx.capabilities.register(
        "llm.chat.v1",
        lambda req: CapabilityResult.ok(
            {
                "reply": json.dumps(
                    {
                        "meaning": "遥测技术",
                        "etymology": "tele (far) + metry (measure)",
                        "phonetic": "/təˈlem.ə.tri/",
                        "pos": "n.",
                        "explanation": "Remote measurement and reporting.",
                    }
                )
                if req.get("action") == "etymology"
                else "Main clause + concessive although clause + non-restrictive relative clause.",
                "served_model": "mock-llm",
            }
        ),
        plugin_id="plugin-magpie-agent",
    )
    ctx.capabilities.register(
        "media.clip_jump.v1",
        lambda req: CapabilityResult.ok(
            {
                "clips": [
                    {
                        "showTitle": "Severance",
                        "episode": "S02E03",
                        "timestampLabel": "28:19",
                        "lineEn": "Telemetry from the floor sounded reassuring.",
                    }
                ]
            }
        ),
        plugin_id="plugin-fnos-nas",
    )

    res_rich = await ctx.capabilities.call(
        "book.source.v1",
        {"action": "lookup_term", "word": "Telemetry", "add_to_vocab": True},
    )
    assert res_rich.ok is True
    assert res_rich.data["text_only"] is False
    assert res_rich.data["llm_used"] is True
    assert res_rich.data["meaning"] == "遥测技术"
    assert res_rich.data["clip_jump_available"] is True
    assert len(res_rich.data["clips"]) == 1
    assert res_rich.data["nas_clip"]["showTitle"] == "Severance"
    assert res_rich.data["in_vocab"] is True

    res_rich_sent = await ctx.capabilities.call(
        "book.source.v1",
        {"action": "explain_sentence", "sentence": "Telemetry sounded reassuring, yet left a gap."},
    )
    assert res_rich_sent.ok is True
    assert res_rich_sent.data["text_only"] is False
    assert res_rich_sent.data["llm_used"] is True
    assert "Main clause" in res_rich_sent.data["analysis"]

    # 3. Failing LLM and failing clip_jump still gracefully degrade
    ctx.capabilities.register("llm.chat.v1", lambda req: CapabilityResult.fail("offline"), plugin_id="plugin-magpie-agent")
    ctx.capabilities.register("media.clip_jump.v1", lambda req: (_ for _ in ()).throw(RuntimeError("nas down")), plugin_id="plugin-fnos-nas")
    res_deg = await ctx.capabilities.call("vocab.sm2.v1", {"action": "lookup_term", "word": "Telemetry"})
    assert res_deg.ok is True
    assert res_deg.data["text_only"] is True
    assert res_deg.data["clips"] == []

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_book_sync_optional_local_first_and_failure_resilience(tmp_path: Path) -> None:
    lib = tmp_path / "sync_lib"
    build_minimal_epub(lib / "sync_book.epub", title="Sync Book")
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "pdata",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    ctx.set_config({"library_roots": str(lib), "sync_enabled": True})
    plugin, ctx = await _setup_plugin(tmp_path, ctx=ctx)

    sync_calls: list[dict[str, Any]] = []

    def _ok_sync(req: dict[str, Any]) -> CapabilityResult:
        sync_calls.append(dict(req))
        return CapabilityResult.ok({"synced": True})

    ctx.capabilities.register("book.sync.v1", _ok_sync, plugin_id="plugin-fnos-nas")

    books = await ctx.domains.book_sources()[0].list_books()
    book_id = books[0].id
    spread = await ctx.capabilities.call("book.source.v1", {"action": "fetch_spread", "book_id": book_id, "page_number": 2})
    assert spread.ok is True
    assert any(c["action"] == "sync_progress" and c["current_page"] == 2 for c in sync_calls)

    await ctx.capabilities.call("vocab.sm2.v1", {"action": "add", "word": "Resilient"})
    assert any(c["action"] == "sync_vocab" for c in sync_calls)

    # Disabling sync_enabled prevents invoking book.sync.v1
    before_count = len(sync_calls)
    ctx.set_config("sync_enabled", False)
    await ctx.capabilities.call("vocab.sm2.v1", {"action": "review", "word": "resilient", "quality": 4})
    assert len(sync_calls) == before_count

    # Failing book.sync.v1 must NOT fail local reads/writes
    ctx.set_config("sync_enabled", True)
    ctx.capabilities.register("book.sync.v1", lambda req: CapabilityResult.fail("nas unreachable"), plugin_id="plugin-fnos-nas")
    spread2 = await ctx.capabilities.call("book.source.v1", {"action": "fetch_spread", "book_id": book_id, "page_number": 1})
    assert spread2.ok is True
    assert spread2.data["extra"]["sync_status"]["synced"] is False
    rev_ok = await ctx.capabilities.call("vocab.sm2.v1", {"action": "review", "word": "resilient", "quality": 5})
    assert rev_ok.ok is True
    assert rev_ok.data["card"]["repetitions"] == 2

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
