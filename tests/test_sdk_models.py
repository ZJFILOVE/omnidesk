"""Tests for omnidesk_sdk Pydantic v2 models, BasePlugin, and PluginContext scoping/cleanup."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnidesk_sdk import (
    BasePlugin,
    BookItem,
    BookPageBlock,
    BookSource,
    BookSpread,
    CapabilityResult,
    ChapterSegment,
    CommentNode,
    DockWidgetSpec,
    InMemoryCapabilityRegistry,
    InMemoryDomainRegistry,
    InMemoryEntityBus,
    LocalConfigStore,
    LocalSecretVault,
    LyricLine,
    MediaItem,
    MediaSource,
    PluginContext,
    SubprocessResult,
    TextArticle,
    TextChannel,
    TextFeedItem,
    TextParagraph,
    TextSource,
    VocabCard,
    WorkspaceTabSpec,
    invoke_lifecycle_hook,
)


class _DummyMediaSource(MediaSource):
    source_id = "dummy-media"
    display_name = "Dummy Media"


class _DummyTextSource(TextSource):
    source_id = "dummy-text"
    display_name = "Dummy Text"
    category = "journal"


class _DummyBookSource(BookSource):
    source_id = "dummy-book"
    display_name = "Dummy Book"


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_capability_result_ok_and_fail_factories() -> None:
    res_ok = CapabilityResult.ok({"gateway_running": True, "group_id": "omnidesk-active"})
    assert res_ok.ok is True
    assert res_ok.data == {"gateway_running": True, "group_id": "omnidesk-active"}
    assert res_ok.error == ""
    assert res_ok.provider_plugin_id == ""

    res_empty = CapabilityResult.ok()
    assert res_empty.ok is True
    assert res_empty.data == {}

    res_kw = CapabilityResult.ok(reply="hi", provider_plugin_id="plugin-magpie-agent")
    assert res_kw.ok is True
    assert res_kw.data == {"reply": "hi"}
    assert res_kw.provider_plugin_id == "plugin-magpie-agent"

    caller_data = {"hint": "configure token"}
    res_fail = CapabilityResult.fail(
        "尚未配置凭据",
        caller_data,
        error_code="not_configured",
        provider_plugin_id="plugin-fnos-nas",
    )
    assert res_fail.ok is False
    assert res_fail.error == "尚未配置凭据"
    assert res_fail.data == {"hint": "configure token", "error_code": "not_configured"}
    assert caller_data == {"hint": "configure token"}
    assert res_fail.provider_plugin_id == "plugin-fnos-nas"

    validated = CapabilityResult.model_validate_json(res_ok.model_dump_json())
    assert validated.ok is True
    assert validated.data["group_id"] == "omnidesk-active"

    res_ok.provider_plugin_id = "mutated-plugin"
    assert res_ok.provider_plugin_id == "mutated-plugin"
    with pytest.raises(ValidationError):
        res_ok.ok = ["invalid"]  # type: ignore[assignment]


def test_base_plugin_optional_hooks_and_custom_lifecycle() -> None:
    async def _run() -> None:
        class MinimalPlugin(BasePlugin):
            pass

        minimal = MinimalPlugin()
        ctx = PluginContext("plugin-minimal")
        await minimal.on_load(ctx)
        assert await minimal.on_health_check() == {"ok": True}
        await minimal.on_unload()

        events: list[str] = []

        class CustomPlugin(BasePlugin):
            async def on_load(self, plugin_ctx: PluginContext) -> None:
                await super().on_load(plugin_ctx)
                plugin_ctx.register_capability("llm.router.v1", self.handle_router)
                plugin_ctx.register_streaming_capability("llm.chat.v1", self.handle_stream)
                events.append("loaded")

            async def handle_router(self, req: dict[str, Any]) -> CapabilityResult:
                return CapabilityResult.ok({"echo": req.get("action", "status")})

            async def handle_stream(self, req: dict[str, Any], stream: Any) -> CapabilityResult:
                await stream.emit_delta("Hello ")
                await stream.emit_delta(req.get("name", "OmniDesk"))
                return CapabilityResult.ok({"reply": "Hello OmniDesk"})

            async def on_health_check(self) -> dict[str, Any]:
                return {"ok": True, "plugin": "custom"}

            async def on_unload(self) -> None:
                events.append("unloaded")

        plugin = CustomPlugin()
        ctx2 = PluginContext("plugin-magpie-agent")
        await invoke_lifecycle_hook(plugin.on_load, ctx2)
        assert await plugin.on_health_check() == {"ok": True, "plugin": "custom"}

        res = await ctx2.capabilities.call("llm.router.v1", {"action": "current_route"})
        assert res.ok is True
        assert res.data == {"echo": "current_route"}
        assert res.provider_plugin_id == "plugin-magpie-agent"

        chunks: list[dict[str, Any]] = []
        stream_res = await ctx2.capabilities.call_stream(
            "llm.chat.v1",
            {"name": "World"},
            on_chunk=chunks.append,
        )
        assert stream_res.ok is True
        assert [c["data"]["text"] for c in chunks] == ["Hello ", "World"]

        await invoke_lifecycle_hook(plugin.on_unload)
        ctx2.cleanup()
        assert events == ["loaded", "unloaded"]
        assert not ctx2.capabilities.has("llm.router.v1")

    asyncio.run(_run())


def test_media_item_validation() -> None:
    item = MediaItem(
        id="am-cn-1643009587",
        sourceId="plugin-music",
        title="Muerte",
        subtitle="Natalia Lafourcade",
        creator="Natalia Lafourcade",
        kind="music",
        aspect="1:1",
        durationSec=214.5,
        progressSec=12.0,
        hasBilingualSub=True,
        curationMode="daily_immutable",
        businessDate="2026-11-14",
        chapters=[ChapterSegment(id="c1", start_sec=0, end_sec=30, label="Intro", kind="content")],
        lyrics=[LyricLine(time_sec=5.2, original="Hola", translation="你好")],
        extra={"storefront": "cn"},
    )
    assert item.source_id == "plugin-music"
    assert item.duration_sec == 214.5
    roundtrip = MediaItem.model_validate_json(item.model_dump_json())
    assert roundtrip == item

    for bad_kwargs in [
        {"id": "  ", "title": "Song"},
        {"id": "m1", "title": ""},
        {"id": "m1", "title": "Song", "kind": "podcast"},
        {"id": "m1", "title": "Song", "aspect": "4:3"},
        {"id": "m1", "title": "Song", "duration_sec": -1.0},
        {"id": "m1", "title": "Song", "progress_sec": -0.5},
        {"id": "m1", "title": "Song", "view_count": -10},
        {"id": "m1", "title": "Song", "business_date": "2026-02-30"},
        {"id": "m1", "title": "Song", "chapters": [{"start_sec": 20, "end_sec": 10}]},
    ]:
        with pytest.raises(ValidationError):
            MediaItem(**bad_kwargs)  # type: ignore[arg-type]


def test_text_feed_item_and_article_validation() -> None:
    channel = TextChannel(id="journal", name="科学期刊", count=3)
    assert channel.id == "journal"

    feed = TextFeedItem(
        id="feed-1",
        sourceId="plugin-journals",
        sourceName="Nature Machine Intelligence",
        category="journal",
        title="Entropy-Guided KV Eviction",
        tags=["KV Cache", "LLM"],
        commentCount=12,
        score=-2,
        businessDate="2026-11-16",
    )
    assert feed.comment_count == 12
    assert feed.score == -2

    article = TextArticle(
        id="art-1",
        source_name="Linux.do",
        category="linuxdo",
        title="Zero-ABI Python Plugins",
        author="arch_zhu",
        blocks=[
            TextParagraph(id="p1", kind="heading", original="Overview", translated="概览"),
            TextParagraph(id="p2", kind="formula", original=r"E = mc^2"),
            TextParagraph(id="p3", kind="code", original="print('ok')", language="python"),
        ],
        comments=[
            CommentNode(
                id="c1",
                author="alice",
                body="Great writeup",
                replies=[CommentNode(id="c2", author="bob", body="Agreed", score=5)],
            )
        ],
    )
    assert article.authors == "arch_zhu"
    assert len(article.paragraphs) == 3
    assert article.comments[0].replies[0].author == "bob"
    assert TextArticle.model_validate_json(article.model_dump_json()) == article

    with pytest.raises(ValidationError):
        TextFeedItem(id="t1", title="Valid", comment_count=-1)
    with pytest.raises(ValidationError):
        TextFeedItem(id="t1", title="Valid", category=" ")
    with pytest.raises(ValidationError):
        TextArticle(id="", title="Missing ID")
    with pytest.raises(ValidationError):
        TextArticle(id="a1", title="Bad Block", paragraphs=[{"kind": "unknown"}])  # type: ignore[arg-type]


def test_book_item_spread_and_vocab_card_validation() -> None:
    spread = BookSpread(
        bookId="book-1",
        chapterTitle="Chapter 7",
        page=12,
        blocks=[
            BookPageBlock(
                id="b1",
                kind="paragraph",
                original="An idempotent operation.",
                translated="幂等操作。",
                keyTerms=["idempotent"],
            )
        ],
    )
    assert spread.page_number == 12
    assert spread.total_pages == 12
    assert spread.blocks[0].highlighted_words == ["idempotent"]

    book = BookItem(
        id="book-1",
        title="Designing Data-Intensive Applications",
        format="epub",
        current_page=40,
        progress_percent=15.5,
        spreads=[spread],
    )
    assert book.total_pages == 40
    assert BookItem.model_validate_json(book.model_dump_json()) == book

    card = VocabCard(
        word="  ephemeral  ",
        phonetic="/ɪˈfem.ər.əl/",
        meaningZh="短暂的",
        bookTitle="The Remains of the Day",
        interval_days=4,
        repetitions=2,
        ease_factor=2.5,
    )
    assert card.word == "ephemeral"
    assert card.meaning == "短暂的"
    assert card.source_book == "The Remains of the Day"

    for bad_book in [
        {"id": "", "title": "Title"},
        {"id": "b1", "title": "Title", "format": "docx"},
        {"id": "b1", "title": "Title", "current_page": 0},
        {"id": "b1", "title": "Title", "current_page": 10, "total_pages": 5},
        {"id": "b1", "title": "Title", "progress_percent": 105.0},
    ]:
        with pytest.raises(ValidationError):
            BookItem(**bad_book)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        BookSpread(book_id="b1", page_number=8, total_pages=4)
    with pytest.raises(ValidationError):
        BookSpread(book_id=" ", page_number=1)

    with pytest.raises(ValidationError):
        VocabCard(word="   ")
    with pytest.raises(ValidationError):
        VocabCard(word="ephemeral", ease_factor=1.1)
    with pytest.raises(ValidationError):
        VocabCard(word="ephemeral", interval_days=-1)


def test_plugin_context_registrations_and_scoped_cleanup() -> None:
    async def _run() -> None:
        caps = InMemoryCapabilityRegistry()
        domains = InMemoryDomainRegistry()
        bus = InMemoryEntityBus()

        ctx_a = PluginContext("plugin-alpha", capabilities=caps, domains=domains, bus=bus)
        ctx_b = PluginContext("plugin-beta", capabilities=caps, domains=domains, bus=bus)

        received_a: list[dict[str, Any]] = []
        received_b: list[dict[str, Any]] = []

        ctx_a.register_capability("demo.echo.v1", lambda req: {"from": "alpha", **req}, priority=50)
        ctx_b.register_capability("demo.echo.v1", lambda req: {"from": "beta", **req}, priority=200)
        ctx_a.register_streaming_capability(
            "demo.stream.v1",
            lambda req, stream: stream.emit_delta("alpha-delta"),
        )
        ctx_a.register_media_source(_DummyMediaSource())
        ctx_a.register_text_source(_DummyTextSource())
        ctx_a.register_book_source(_DummyBookSource())
        ctx_b.register_media_source(_DummyMediaSource())

        tab = ctx_a.register_workspace_tab(
            WorkspaceTabSpec(tab_id="alpha-tab", plugin_id="wrong-id", title="Alpha Tab", domain="text")
        )
        assert tab.plugin_id == "plugin-alpha"
        widget = ctx_a.register_dock_widget(
            DockWidgetSpec(widget_id="alpha-dock", title="Alpha Dock", supported_domains=["text"])
        )
        assert widget.plugin_id == "plugin-alpha"
        ctx_b.register_workspace_tab(tab_id="beta-tab", title="Beta Tab", domain="media")

        ctx_a.subscribe("entity.*", lambda evt: received_a.append(evt.payload))
        ctx_b.subscribe("entity.media.open", lambda evt: received_b.append(evt.payload))

        task_started = asyncio.Event()
        task_cancelled = asyncio.Event()

        async def _bg_worker() -> None:
            task_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                task_cancelled.set()
                raise

        bg_task = ctx_a.spawn_task(_bg_worker())
        await task_started.wait()

        counts = ctx_a.resource_counts()
        assert counts == {
            "capabilities": 1,
            "streaming_capabilities": 1,
            "media_sources": 1,
            "text_sources": 1,
            "book_sources": 1,
            "workspace_tabs": 1,
            "dock_widgets": 1,
            "subscriptions": 1,
            "tasks": 1,
            "subprocesses": 0,
        }

        await ctx_b.emit("entity.media.open", {"item_id": "m1"})
        assert received_a == [{"item_id": "m1"}]
        assert received_b == [{"item_id": "m1"}]

        # Higher priority beta wins before cleanup.
        res_before = await caps.call("demo.echo.v1", {"x": 1})
        assert res_before.provider_plugin_id == "plugin-beta"

        # Cleanup alpha only; beta must stay intact.
        summary = await ctx_a.acleanup()
        assert summary["capabilities"] == 1
        assert summary["tasks"] == 1
        assert bg_task.cancelled()
        assert task_cancelled.is_set()
        assert ctx_a.tracked_resources()["capabilities"] == []
        assert all(v == 0 for v in ctx_a.resource_counts().values())

        # Second cleanup is idempotent.
        assert all(v == 0 for v in ctx_a.cleanup().values())

        assert not caps.has("demo.stream.v1")
        assert caps.has("demo.echo.v1")
        assert len(caps.providers_for("demo.echo.v1")) == 1
        assert len(domains.media_sources()) == 1
        assert domains.text_sources() == []
        assert domains.book_sources() == []
        assert [t.tab_id for t in domains.workspace_tabs()] == ["beta-tab"]
        assert domains.dock_widgets() == []

        await ctx_b.emit("entity.media.open", {"item_id": "m2"})
        assert received_a == [{"item_id": "m1"}]
        assert received_b == [{"item_id": "m1"}, {"item_id": "m2"}]

    asyncio.run(_run())


def test_plugin_context_config_and_secret_isolation(tmp_path: Path) -> None:
    schema = [
        {"key": "magpie_bin", "label": "Bin", "type": "string", "default": "magpie"},
        {"key": "gateway_key", "label": "Key", "type": "secret", "default": ""},
    ]
    ctx = PluginContext("plugin-cfg", config_dir=tmp_path, config_schema=schema)

    assert ctx.get_config() == {"magpie_bin": "magpie"}
    assert ctx.get_config("magpie_bin") == "magpie"

    updated = ctx.set_config({"magpie_bin": "/usr/bin/magpie", "gateway_key": "leak-attempt", "api_token": "x"})
    assert updated == {"magpie_bin": "/usr/bin/magpie"}
    assert ctx.get_config("gateway_key") is None

    ctx.set_config("routing_strategy", "smart")
    assert ctx.get_config("routing_strategy") == "smart"

    ctx.set_secret("gateway_key", "sk-secret-value")
    assert ctx.get_secret("gateway_key") == "sk-secret-value"
    assert "gateway_key" not in ctx.get_config()

    cfg_file = tmp_path / "plugins" / "plugin-cfg.json"
    vault_file = tmp_path / "vault.json"
    assert stat.S_IMODE(cfg_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(vault_file.stat().st_mode) == 0o600
    assert "sk-secret-value" not in cfg_file.read_text(encoding="utf-8")

    ctx.set_secret("gateway_key", None)
    assert ctx.get_secret("gateway_key", "fallback") == "fallback"

    with pytest.raises(ValueError):
        PluginContext("../escape")


def test_exec_subprocess_isolation_timeout_and_cancellation() -> None:
    async def _run() -> None:
        async with PluginContext("plugin-proc") as ctx:
            ok_res = await ctx.exec_subprocess(
                [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"]
            )
            assert ok_res.ok is True
            assert ok_res.returncode == 0
            assert ok_res.stdout.strip() == "out"
            assert ok_res.stderr.strip() == "err"

            with pytest.raises(TypeError):
                await ctx.exec_subprocess("echo nope")  # type: ignore[arg-type]

            # Spawn parent + child process; timeout must terminate the entire process group.
            pid_file_code = (
                "import subprocess, sys, time; "
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "print(c.pid, flush=True); "
                "time.sleep(30)"
            )
            timeout_res = await ctx.exec_subprocess(
                [sys.executable, "-c", pid_file_code],
                timeout_sec=0.25,
                grace_sec=0.1,
            )
            assert timeout_res.timed_out is True
            assert timeout_res.ok is False
            child_pid_line = timeout_res.stdout.strip().splitlines()[0]
            child_pid = int(child_pid_line)
            # Allow kernel a moment to reap terminated child process.
            deadline = time.monotonic() + 1.0
            while _is_pid_alive(child_pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert not _is_pid_alive(child_pid)

            cancel_event = asyncio.Event()

            async def _trigger_cancel() -> None:
                await asyncio.sleep(0.08)
                cancel_event.set()

            canceller = asyncio.create_task(_trigger_cancel())
            cancel_res = await ctx.exec_subprocess(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cancel_token=cancel_event,
                grace_sec=0.1,
            )
            await canceller
            assert cancel_res.cancelled is True
            assert cancel_res.ok is False

    asyncio.run(_run())


def test_core_bus_and_config_store_interoperability(tmp_path: Path) -> None:
    from core.omnidesk_core.bus import EntityBus
    from core.omnidesk_core.config import ConfigStore

    async def _run() -> None:
        bus = EntityBus()
        config_store = ConfigStore(tmp_path)
        ctx = PluginContext("plugin-core-interop", bus=bus, config_store=config_store)

        seen: list[str] = []
        ctx.subscribe("entity.text.open", lambda evt: seen.append(evt.payload["article_id"]))
        await ctx.emit("entity.text.open", {"article_id": "a-42"})
        assert seen == ["a-42"]

        ctx.set_config({"endpoint": "http://127.0.0.1:3425", "api_key": "should-strip"})
        assert ctx.get_config() == {"endpoint": "http://127.0.0.1:3425"}

        ctx.cleanup()
        assert bus.subscription_count(plugin_id="plugin-core-interop") == 0

    asyncio.run(_run())


def test_capability_fault_isolation_and_domain_sources() -> None:
    async def _run() -> None:
        ctx = PluginContext("plugin-faulty")

        def _boom(_req: dict[str, Any]) -> CapabilityResult:
            raise RuntimeError("upstream crashed")

        ctx.register_capability("demo.boom.v1", _boom)
        fault_res = await ctx.capabilities.call("demo.boom.v1", {})
        assert fault_res.ok is False
        assert fault_res.data["error_code"] == "plugin_fault"
        assert "upstream crashed" in fault_res.error
        assert fault_res.provider_plugin_id == "plugin-faulty"

        missing_res = await ctx.capabilities.call("demo.missing.v1", {})
        assert missing_res.ok is False
        assert missing_res.data["error_code"] == "capability_not_found"

        class ConcreteMediaSource(MediaSource):
            source_id = "demo-media"

            def categories(self) -> list[str]:
                return ["featured"]

            async def fetch_items(
                self, category: str = "", query: str = "", page: int = 1, *, force_refresh: bool = False
            ) -> list[MediaItem]:
                return [MediaItem(id="m1", source_id=self.source_id, title="Video 1")]

        src = ConcreteMediaSource()
        ctx.register_media_source(src)
        items = await ctx.domains.media_sources()[0].fetch_items("featured")
        assert len(items) == 1
        assert items[0].source_id == "demo-media"

        with pytest.raises(ValidationError):
            ctx.register_workspace_tab({"tab_id": "", "title": "Bad Tab"})
        with pytest.raises(ValidationError):
            ctx.register_dock_widget({"widget_id": "w1", "title": "Bad", "supported_domains": ["unknown"]})

        ctx.cleanup()
        assert ctx.domains.media_sources() == []

    asyncio.run(_run())


def test_pydantic_serializability_and_capability_result_contract() -> None:
    from core.omnidesk_core.registry import CapabilityResult as CoreCapabilityResult

    # Non-UTF-8 binary subprocess output must serialize and roundtrip cleanly via base64 JSON.
    raw_res = SubprocessResult(
        returncode=0,
        stdout="ok",
        stderr="",
        stdout_bytes=b"\xff\xfe\x00\x80",
        stderr_bytes=b"\x81\x82",
    )
    roundtrip = SubprocessResult.model_validate_json(raw_res.model_dump_json())
    assert roundtrip == raw_res
    assert roundtrip.stdout_bytes == b"\xff\xfe\x00\x80"
    assert roundtrip.stderr_bytes == b"\x81\x82"

    # CapabilityResult.ok/fail edge cases, to_dict, with_provider, and CoreCapabilityResult coercion.
    wrapped_sr = CapabilityResult.ok(raw_res)
    assert '"stdout_bytes"' in wrapped_sr.model_dump_json()

    res_empty_code = CapabilityResult.fail("oops", error_code="")
    assert "error_code" not in res_empty_code.data
    res_warn = CapabilityResult.ok({"x": 1}, error="non-fatal")
    assert res_warn.error == "non-fatal"
    assert res_warn.data == {"x": 1}

    tagged = res_warn.with_provider("plugin-p1")
    assert tagged.provider_plugin_id == "plugin-p1"
    assert res_warn.provider_plugin_id == ""
    assert tagged.to_dict()["provider_plugin_id"] == "plugin-p1"

    core_fail = CoreCapabilityResult.fail("core err", error_code="timeout", provider_plugin_id="plugin-c")
    sdk_from_core = CapabilityResult.model_validate(core_fail)
    assert sdk_from_core.ok is False
    assert sdk_from_core.error == "core err"
    assert sdk_from_core.data == {"error_code": "timeout"}
    assert sdk_from_core.provider_plugin_id == "plugin-c"

    # Declarative UI specs support JSON Schema generation even with callable hooks.
    tab_schema = WorkspaceTabSpec.model_json_schema()
    dock_schema = DockWidgetSpec.model_json_schema()
    assert "render" not in tab_schema["properties"]
    assert "on_action" not in dock_schema["properties"]
    tab = WorkspaceTabSpec(tab_id="t1", title="Tab 1", render=lambda: {"type": "Markdown"})
    assert "render" not in tab.model_dump()
    assert WorkspaceTabSpec.model_validate_json(tab.model_dump_json()).tab_id == "t1"

    # Non-finite floats are rejected rather than becoming null in JSON roundtrips.
    with pytest.raises(ValidationError):
        MediaItem(id="m-inf", title="Inf", duration_sec=float("inf"))
    with pytest.raises(ValidationError):
        VocabCard(word="test", ease_factor=float("nan"))


def test_secret_security_redaction_symlinks_and_core_vault_interop(tmp_path: Path) -> None:
    from core.omnidesk_core.vault import SecretVault

    async def _run() -> None:
        # 1. LocalSecretVault <-> core.SecretVault format interoperability on the same vault.json.
        core_vault = SecretVault(config_dir=tmp_path)
        core_vault.set("global_key", "global-secret-1234")
        core_vault.set("plugin-a", "api_token", "core-token-9876")

        local_vault = LocalSecretVault(tmp_path)
        assert local_vault.get_secret("plugin-a", "api_token") == "core-token-9876"
        local_vault.set_secret("plugin-b", "gateway_key", "local-secret-5555")
        assert core_vault.get("plugin-b", "gateway_key") == "local-secret-5555"
        assert core_vault.get("global_key") == "global-secret-1234"
        assert "local-secret-5555" not in repr(local_vault)

        # Tighten overly permissive vault permissions automatically on read.
        vault_path = tmp_path / "vault.json"
        os.chmod(vault_path, 0o644)
        assert local_vault.get_secret("plugin-b", "gateway_key") == "local-secret-5555"
        assert stat.S_IMODE(vault_path.stat().st_mode) == 0o600

        # Symlinked config & vault files are rejected, and tuple secrets are stripped.
        real_file = tmp_path / "real.json"
        real_file.write_text('{"leak": "1"}', encoding="utf-8")
        sym_vault_dir = tmp_path / "symvault"
        sym_vault_dir.mkdir()
        (sym_vault_dir / "vault.json").symlink_to(real_file)
        with pytest.raises(ValueError, match="symlink"):
            LocalSecretVault(sym_vault_dir).get_secret("plugin-a", "api_token")

        cfg_store = LocalConfigStore(tmp_path)
        cfg_store.register_schema("plugin-tuple", [{"key": "custom_pin", "type": "secret"}])
        written = cfg_store.write(
            "plugin-tuple",
            {"nested": ({"custom_pin": "pin-123", "ok": 1}, {"api_key": "raw", "ok": 2})},
        )
        assert written == {"nested": ({"ok": 1}, {"ok": 2})}
        sym_cfg = tmp_path / "plugins" / "plugin-sym.json"
        sym_cfg.symlink_to(real_file)
        with pytest.raises(ValueError, match="symlink"):
            cfg_store.read("plugin-sym")

        # Corrupted vault JSON never leaks secret content in exception text.
        bad_dir = tmp_path / "corrupt"
        bad_dir.mkdir()
        (bad_dir / "vault.json").write_text('{"secrets": {"p": {"k": "SUPER_RAW_SECRET"', encoding="utf-8")
        with pytest.raises(ValueError) as corrupt_exc:
            LocalSecretVault(bad_dir).get_secret("p", "k")
        assert "SUPER_RAW_SECRET" not in str(corrupt_exc.value)

        # InMemoryCapabilityRegistry and InMemoryEntityBus redact vaulted secrets & credential patterns.
        ctx = PluginContext("plugin-sec", config_dir=tmp_path)
        ctx.set_secret("custom_secret", "vault-custom-val-42")
        ctx.register_capability(
            "sec.boom.v1",
            lambda _req: (_ for _ in ()).throw(RuntimeError("failed Bearer tok123456 and vault-custom-val-42")),
        )
        cap_fail = await ctx.capabilities.call("sec.boom.v1", {})
        assert cap_fail.ok is False
        assert "tok123456" not in cap_fail.error
        assert "vault-custom-val-42" not in cap_fail.error
        assert "[REDACTED]" in cap_fail.error

        ctx.subscribe(
            "sec.topic",
            lambda _evt: (_ for _ in ()).throw(RuntimeError("api_key=rawkey999 and vault-custom-val-42")),
        )
        pub = await ctx.emit("sec.topic", {})
        assert pub.failed == 1
        assert "rawkey999" not in pub.faults[0]["error"]
        assert "vault-custom-val-42" not in pub.faults[0]["error"]

        # Unserializable values propagate TypeError instead of bypassing secret_keys/config_schema.
        with pytest.raises(TypeError):
            ctx.set_config({"bad": {1, 2, 3}})
        with pytest.raises(ValueError):
            ctx.get_secret("  ")
        with pytest.raises(ValueError):
            ctx.set_secret("bad\x00key", "x")
        await ctx.acleanup()

    asyncio.run(_run())


def test_subprocess_sigterm_ignoring_child_threading_event_and_self_acleanup() -> None:
    async def _run() -> None:
        # 1. Parent dies on SIGTERM while grandchild ignores SIGTERM -> SIGKILL must still kill grandchild.
        child_code = (
            "import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('READY', flush=True); "
            "time.sleep(30)"
        )
        parent_code = (
            "import subprocess, sys, time; "
            f"c = subprocess.Popen([sys.executable, '-c', {child_code!r}], stdout=subprocess.PIPE); "
            "ready = c.stdout.readline().decode().strip(); "
            "print(c.pid, ready, flush=True); "
            "time.sleep(30)"
        )
        async with PluginContext("plugin-stubborn") as ctx:
            timeout_res = await ctx.exec_subprocess(
                [sys.executable, "-c", parent_code],
                timeout_sec=0.25,
                grace_sec=0.1,
            )
            assert timeout_res.timed_out is True
            assert timeout_res.ok is False
            stubborn_pid = int(timeout_res.stdout.strip().splitlines()[0].split()[0])
            deadline = time.monotonic() + 1.0
            while _is_pid_alive(stubborn_pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert not _is_pid_alive(stubborn_pid)

            # 2. threading.Event cancel_token must not block the asyncio event loop.
            sync_cancel = threading.Event()
            timer = threading.Timer(0.08, sync_cancel.set)
            timer.start()
            try:
                cancel_res = await ctx.exec_subprocess(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout_sec=2.0,
                    cancel_token=sync_cancel,
                    grace_sec=0.05,
                )
            finally:
                timer.cancel()
            assert cancel_res.cancelled is True
            assert cancel_res.timed_out is False

        # 3. Calling acleanup() from inside a spawned task must not self-deadlock.
        ctx_self = PluginContext("plugin-self-clean")
        other_cancelled = asyncio.Event()

        async def _other() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                other_cancelled.set()
                raise

        async def _trigger_self_cleanup() -> dict[str, int]:
            await asyncio.sleep(0.02)
            return await ctx_self.acleanup(grace_sec=0.05)

        ctx_self.spawn_task(_other())
        cleaner = ctx_self.spawn_task(_trigger_self_cleanup())
        summary = await cleaner
        assert summary["tasks"] == 2
        assert other_cancelled.is_set()

    asyncio.run(_run())


def test_core_registry_and_domain_direct_interop_and_generator_streaming() -> None:
    from core.omnidesk_core.domain import DomainRegistry
    from core.omnidesk_core.registry import CapabilityRegistry

    async def _run() -> None:
        core_caps = CapabilityRegistry(isolate_errors=True)
        core_domains = DomainRegistry()

        ctx_a = PluginContext("plugin-alpha", capabilities=core_caps, domains=core_domains)
        ctx_b = PluginContext("plugin-beta", capabilities=core_caps, domains=core_domains)

        ctx_a.register_capability(
            "alpha.fail.v1",
            lambda _req: CapabilityResult.fail("bad input", error_code="invalid_argument"),
            priority=50,
            metadata={"tag": "v1"},
        )

        async def _alpha_stream(req: dict[str, Any], stream: Any) -> CapabilityResult:
            await stream.emit_event("stage", {"step": 1})
            await stream.emit_delta(req.get("msg", "hi"))
            return CapabilityResult.ok({"done": True})

        ctx_a.register_streaming_capability("alpha.stream.v1", _alpha_stream)
        ctx_b.register_capability("beta.ping.v1", lambda: {"pong": True})

        ctx_a.register_media_source(_DummyMediaSource())
        ctx_b.register_text_source(_DummyTextSource())

        fail_res = await core_caps.call("alpha.fail.v1", {})
        assert fail_res.ok is False
        assert fail_res.data["error_code"] == "invalid_argument"
        assert fail_res.provider_plugin_id == "plugin-alpha"

        pong_res = await core_caps.call("beta.ping.v1", {})
        assert pong_res.ok is True and pong_res.data == {"pong": True}

        chunks = [c async for c in core_caps.stream("alpha.stream.v1", {"msg": "ok"})]
        assert [c.event for c in chunks] == ["stage", "delta", "result"]
        assert chunks[-1].result is not None and chunks[-1].result.provider_plugin_id == "plugin-alpha"

        assert len(core_domains.list_sources(plugin_id="plugin-alpha")) == 1
        assert len(core_domains.list_sources(plugin_id="plugin-beta")) == 1

        # Cleanup alpha only; beta capabilities and domain sources stay intact.
        await ctx_a.acleanup()
        assert not core_caps.has("alpha.fail.v1")
        assert not core_caps.has("alpha.stream.v1")
        assert core_caps.has("beta.ping.v1")
        assert core_domains.list_sources(plugin_id="plugin-alpha") == []
        assert len(core_domains.list_sources(plugin_id="plugin-beta")) == 1
        await ctx_b.acleanup()

        # Standalone InMemoryCapabilityRegistry also supports generator streaming & sync hooks.
        sdk_ctx = PluginContext("plugin-gen")

        async def _gen_cap(req: dict[str, Any]):
            for item in req.get("items", ["g1", "g2"]):
                yield item

        sdk_ctx.register_streaming_capability("gen.items.v1", _gen_cap)
        streamed: list[dict[str, Any]] = []
        gen_res = await sdk_ctx.capabilities.call_stream("gen.items.v1", {}, on_chunk=streamed.append)
        assert gen_res.ok is True
        assert gen_res.data["chunks"] == ["g1", "g2"]
        assert [c["data"]["text"] for c in streamed] == ["g1", "g2"]

        class SyncPlugin(BasePlugin):
            def on_load(self, plugin_ctx: PluginContext) -> None:  # type: ignore[override]
                self.ctx = plugin_ctx
                self.loaded = True

            def on_health_check(self) -> dict[str, Any]:  # type: ignore[override]
                return {"ok": True, "sync": True}

            def on_unload(self) -> None:  # type: ignore[override]
                self.loaded = False

        sp = SyncPlugin()
        await invoke_lifecycle_hook(sp.on_load, sdk_ctx)
        assert sp.loaded is True
        assert await invoke_lifecycle_hook(sp.on_health_check) == {"ok": True, "sync": True}
        await invoke_lifecycle_hook(sp.on_unload)
        assert sp.loaded is False
        await sdk_ctx.acleanup()

    asyncio.run(_run())


