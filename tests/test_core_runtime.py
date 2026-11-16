"""Unit tests for OmniDesk v2 Core Runtime (CapabilityRegistry, DomainRegistry, JobCenter, EntityBus)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_CORE_DIR = str(Path(__file__).resolve().parents[1] / "core")
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

from omnidesk_core.bus import FAULT_TOPIC, BusEvent, EntityBus
from omnidesk_core.domain import DomainKind, DomainRegistry
from omnidesk_core.errors import (
    CapabilityConflictError,
    CapabilityInvocationError,
    CapabilityNotFoundError,
    DomainSourceConflictError,
    DomainSourceNotFoundError,
    ErrorCode,
    InvalidArgumentError,
    InvalidCapabilityIdError,
    JobCancelledError,
    JobNotFoundError,
    JobStateError,
    PathSafetyError,
    redact_secrets,
)
from omnidesk_core.jobs import (
    JOB_CANCELLED_TOPIC,
    JOB_COMPLETED_TOPIC,
    JOB_CREATED_TOPIC,
    JOB_FAILED_TOPIC,
    JOB_PROGRESS_TOPIC,
    JobCenter,
    JobContext,
    JobStatus,
)
from omnidesk_core.paths import validate_plugin_id
from omnidesk_core.registry import CapabilityRegistry, CapabilityResult, CapabilityStream
from omnidesk_sdk import CapabilityResult as SdkCapabilityResult
from omnidesk_sdk import CapabilityStream as SdkCapabilityStreamProtocol
from omnidesk_sdk import MediaSource as SdkMediaSource
from omnidesk_sdk import PluginContext


@dataclass
class _FakeModel:
    reply: str
    tokens: int

    def model_dump(self) -> dict[str, Any]:
        return {"reply": self.reply, "tokens": self.tokens}


class _FakeMediaSource:
    title = "Fake Media"

    def categories(self) -> list[str]:
        return ["video", "music"]

    def fetch_items(self, category: str = "", query: str = "", page: int = 1) -> list[dict[str, Any]]:
        return [{"id": "m1", "category": category, "query": query, "page": page}]

    def fetch_chapters(self, item_id: str) -> list[dict[str, Any]]:
        return []

    def fetch_lyrics(self, item_id: str) -> list[dict[str, Any]]:
        return []


class _FakeTextSource:
    title = "Fake Text"
    category = "multi"

    def channels(self) -> list[str]:
        return ["news", "journals"]

    def fetch_feed(self, channel_id: str = "", query: str = "", page: int = 1) -> list[dict[str, Any]]:
        return [{"id": "t1", "channel": channel_id}]

    def fetch_article(self, article_id: str) -> dict[str, Any]:
        return {"id": article_id}


class _FakeBookSource:
    title = "Fake Book"

    def list_books(self, format_filter: str = "", query: str = "") -> list[dict[str, Any]]:
        return [{"id": "b1", "format": format_filter}]

    def fetch_spread(self, book_id: str, page_number: int = 1) -> dict[str, Any]:
        return {"book_id": book_id, "page": page_number}

    def list_vocab(self) -> list[dict[str, Any]]:
        return []


# ---------------------------------------------------------------------------
# CapabilityRegistry tests
# ---------------------------------------------------------------------------


def test_capability_registration_versioning_and_validation() -> None:
    reg = CapabilityRegistry()

    with pytest.raises(InvalidCapabilityIdError):
        reg.register("llm.chat", lambda req: req, plugin_id="plugin-a")
    with pytest.raises(InvalidCapabilityIdError):
        reg.register("LLM.Chat.v1", lambda req: req, plugin_id="plugin-a")
    with pytest.raises(PathSafetyError):
        reg.register("llm.chat.v1", lambda req: req, plugin_id="../escape")

    reg.register("llm.chat.v1", lambda req: {"v": 1, **req}, plugin_id="plugin-a", description="v1")
    reg.register("llm.chat.v2", lambda req: {"v": 2, **req}, plugin_id="plugin-a", description="v2")
    reg.register("media.play.v1", lambda req: {"ok": True}, plugin_id="plugin-b")

    assert "llm.chat.v1" in reg
    assert reg.has("llm.chat.v2")
    assert [r.version for r in reg.list_versions("llm.chat")] == [1, 2]
    latest = reg.latest_version("llm.chat")
    assert latest is not None and latest.capability_id == "llm.chat.v2"
    assert len(reg.list_capabilities(plugin_id="plugin-a")) == 2
    assert len(reg.list_capabilities(prefix="media.")) == 1

    # Conflict from another plugin or without replace=True
    with pytest.raises(CapabilityConflictError):
        reg.register("llm.chat.v1", lambda req: req, plugin_id="plugin-b")
    with pytest.raises(CapabilityConflictError):
        reg.register("llm.chat.v1", lambda req: req, plugin_id="plugin-a")

    # Same plugin with replace=True succeeds
    reg.register("llm.chat.v1", lambda req: {"v": 10}, plugin_id="plugin-a", replace=True)
    assert reg.call_sync("llm.chat.v1").data == {"v": 10}


@pytest.mark.asyncio
async def test_capability_sync_async_and_not_found() -> None:
    reg = CapabilityRegistry()

    with pytest.raises(CapabilityNotFoundError) as exc_info:
        await reg.call("missing.cap.v1")
    assert exc_info.value.code == ErrorCode.CAPABILITY_NOT_FOUND

    with pytest.raises(CapabilityNotFoundError):
        reg.call_sync("missing.cap.v1")

    reg.register("demo.sync.v1", lambda req: CapabilityResult.ok({"echo": req["msg"]}), plugin_id="plugin-sync")
    reg.register("demo.model.v1", lambda req: _FakeModel(reply="hi", tokens=3), plugin_id="plugin-sync")

    async def async_handler(req: dict[str, Any]) -> CapabilityResult:
        await asyncio.sleep(0.005)
        return CapabilityResult.ok({"sum": req["a"] + req["b"]})

    reg.register("demo.async.v1", async_handler, plugin_id="plugin-async")

    res_sync = reg.call_sync("demo.sync.v1", {"msg": "hello"})
    assert res_sync.ok is True
    assert res_sync.data == {"echo": "hello"}
    assert res_sync.provider_plugin_id == "plugin-sync"

    # call_sync also bridges async handlers safely (even inside running loop)
    res_bridged = reg.call_sync("demo.async.v1", {"a": 2, "b": 5})
    assert res_bridged.ok is True and res_bridged.data == {"sum": 7}

    # async call invokes both sync and async handlers seamlessly
    res_a = await reg.call("demo.sync.v1", {"msg": "world"})
    res_b = await reg.call("demo.async.v1", {"a": 10, "b": 20})
    res_m = await reg.call("demo.model.v1", {})
    assert res_a.data == {"echo": "world"}
    assert res_b.data == {"sum": 30}
    assert res_m.data == {"reply": "hi", "tokens": 3}


@pytest.mark.asyncio
async def test_capability_streaming_callback_and_generators() -> None:
    reg = CapabilityRegistry()

    async def chat_stream(req: dict[str, Any], stream: CapabilityStream) -> CapabilityResult:
        await stream.emit("stage", {"phase": "start"})
        for word in req["prompt"].split():
            await stream.emit_delta(word)
        return CapabilityResult.ok({"reply": req["prompt"]})

    async def gen_stream(req: dict[str, Any]):
        for i in range(req["count"]):
            yield f"chunk-{i}"

    reg.register_streaming("llm.chat.v1", chat_stream, plugin_id="plugin-magpie")
    reg.register_streaming("speech.asr.v1", gen_stream, plugin_id="plugin-asr")

    cb_chunks = [c async for c in reg.stream("llm.chat.v1", {"prompt": "hello omnidesk"})]
    assert [c.event for c in cb_chunks] == ["stage", "delta", "delta", "result"]
    assert cb_chunks[1].data == "hello"
    assert cb_chunks[2].data == "omnidesk"
    assert cb_chunks[-1].done is True
    assert cb_chunks[-1].result is not None
    assert cb_chunks[-1].result.provider_plugin_id == "plugin-magpie"

    # Unary .call() on a streaming capability returns the collected final result
    unary_res = await reg.call("llm.chat.v1", {"prompt": "one two"})
    assert unary_res.ok is True and unary_res.data["reply"] == "one two"
    assert unary_res.data["chunks"] == ["one", "two"] or "one" in unary_res.data["chunks"]

    gen_chunks = [c async for c in reg.stream("speech.asr.v1", {"count": 3})]
    assert [c.data for c in gen_chunks] == ["chunk-0", "chunk-1", "chunk-2"]
    assert all(c.provider_plugin_id == "plugin-asr" for c in gen_chunks)


@pytest.mark.asyncio
async def test_capability_revocation_and_fault_isolation() -> None:
    bus = EntityBus()
    faults: list[BusEvent] = []
    bus.subscribe(FAULT_TOPIC, lambda evt: faults.append(evt), plugin_id="core")

    reg = CapabilityRegistry(bus=bus, secret_provider=lambda: ["top-secret-xyz"])
    reg.register("p1.one.v1", lambda req: {"n": 1}, plugin_id="plugin-one")
    reg.register("p1.two.v1", lambda req: {"n": 2}, plugin_id="plugin-one")
    reg.register("p2.keep.v1", lambda req: {"n": 3}, plugin_id="plugin-two")

    # Cross-plugin single unregister must reject
    with pytest.raises(CapabilityConflictError):
        reg.unregister("p1.one.v1", plugin_id="plugin-two")

    removed = reg.revoke_plugin("plugin-one")
    assert removed == 2
    assert not reg.has("p1.one.v1")
    assert not reg.has("p1.two.v1")
    assert reg.has("p2.keep.v1")

    with pytest.raises(CapabilityNotFoundError):
        await reg.call("p1.one.v1")

    def exploding(req: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("upstream failed with api_key=raw123 and top-secret-xyz")

    reg.register("p2.boom.v1", exploding, plugin_id="plugin-two")

    with pytest.raises(CapabilityInvocationError) as exc_info:
        await reg.call("p2.boom.v1")
    assert "raw123" not in str(exc_info.value)
    assert "top-secret-xyz" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)
    assert len(faults) == 1
    assert faults[0].payload["plugin_id"] == "plugin-two"

    isolated = await reg.call("p2.boom.v1", isolate_errors=True)
    assert isolated.ok is False
    assert isolated.data["error_code"] == ErrorCode.PLUGIN_FAULT
    assert "top-secret-xyz" not in isolated.error


# ---------------------------------------------------------------------------
# DomainRegistry tests
# ---------------------------------------------------------------------------


def test_domain_registry_register_query_and_revoke() -> None:
    domains = DomainRegistry()

    media_src = _FakeMediaSource()
    text_src = _FakeTextSource()
    book_src = _FakeBookSource()

    domains.register_media_source("ai-curator", media_src, plugin_id="plugin-ai-curator", priority=10)
    domains.register_text_source("ai-curator", text_src, plugin_id="plugin-ai-curator", priority=5)
    domains.register_book_source("ai-curator", book_src, plugin_id="plugin-ai-curator")
    domains.register_media_source("bilibili", _FakeMediaSource(), plugin_id="plugin-bilibili", priority=20)

    assert domains.counts() == {"media": 2, "text": 1, "book": 1}
    assert [s.source_id for s in domains.list_media_sources()] == ["bilibili", "ai-curator"]
    assert domains.list_text_sources(category="multi")[0].source_id == "ai-curator"
    assert domains.get_source(DomainKind.BOOK, "ai-curator") is book_src

    with pytest.raises(DomainSourceConflictError):
        domains.register_media_source("bilibili", _FakeMediaSource(), plugin_id="plugin-other")
    with pytest.raises(DomainSourceConflictError):
        domains.unregister("media", "bilibili", plugin_id="plugin-other")
    with pytest.raises(DomainSourceNotFoundError):
        domains.resolve("text", "missing")
    with pytest.raises(InvalidArgumentError):
        domains.register("unknown", "x", media_src, plugin_id="plugin-bilibili")

    revoked = domains.revoke_plugin("plugin-ai-curator")
    assert revoked == 3
    assert domains.counts() == {"media": 1, "text": 0, "book": 0}
    assert domains.has("media", "bilibili")


# ---------------------------------------------------------------------------
# EntityBus event fault isolation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_bus_event_fault_isolation_and_revocation() -> None:
    bus = EntityBus(secret_provider=lambda: ["vault-secret-999"])
    received: list[BusEvent] = []
    faults: list[BusEvent] = []

    def broken_listener(evt: BusEvent) -> None:
        raise ValueError("crash with Bearer abcdef123456 and vault-secret-999")

    async def healthy_listener(evt: BusEvent) -> None:
        received.append(evt)

    def broken_fault_listener(evt: BusEvent) -> None:
        faults.append(evt)
        raise RuntimeError("fault subscriber itself exploded")

    bus.subscribe("entity.*", broken_listener, plugin_id="plugin-bad")
    bus.subscribe("entity.media.open", healthy_listener, plugin_id="plugin-good")
    bus.subscribe(FAULT_TOPIC, broken_fault_listener, plugin_id="plugin-monitor")

    result = await bus.publish("entity.media.open", {"item_id": "ep-1"}, source_plugin_id="plugin-nas")
    assert result.delivered == 1
    assert result.failed == 1
    assert len(received) == 1
    assert received[0].payload["item_id"] == "ep-1"

    # Fault event emitted with secrets scrubbed; broken fault listener did not recurse infinitely
    assert len(faults) == 1
    err_msg = faults[0].payload["error"]
    assert "abcdef123456" not in err_msg
    assert "vault-secret-999" not in err_msg
    assert "[REDACTED]" in err_msg

    assert bus.unsubscribe_plugin("plugin-bad") == 1
    second = await bus.publish("entity.media.open", {"item_id": "ep-2"}, source_plugin_id="plugin-nas")
    assert second.delivered == 1 and second.failed == 0


# ---------------------------------------------------------------------------
# JobCenter lifecycle, progress events, and cancellation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_center_manual_lifecycle_progress_and_state_errors() -> None:
    bus = EntityBus()
    events: list[BusEvent] = []
    bus.subscribe("job.*", lambda evt: events.append(evt), plugin_id="core")
    bus.subscribe("asr.job.progress", lambda evt: events.append(evt), plugin_id="core")

    jobs = JobCenter(bus=bus, secret_provider=lambda: ["secret-token-42"])
    created = await jobs.create(
        "asr.transcribe",
        title="Transcribe Clip",
        plugin_id="plugin-asr",
        total_steps=4,
        progress_topic="asr.job.progress",
    )
    assert created.status == JobStatus.PENDING
    assert created.progress == 0.0

    started = await jobs.start(created.job_id, stage="probing", message="Checking audio streams")
    assert started.status == JobStatus.RUNNING

    prog1 = await jobs.update(created.job_id, completed_steps=2, stage="transcribing", message="Chunk 2/4")
    assert prog1.progress == pytest.approx(0.5)
    assert prog1.completed_steps == 2

    with pytest.raises(InvalidArgumentError):
        await jobs.update(created.job_id, progress=1.5)

    completed = await jobs.complete(created.job_id, result={"srt": "/tmp/out.srt"})
    assert completed.status == JobStatus.COMPLETED
    assert completed.progress == 1.0
    assert completed.completed_steps == 4

    with pytest.raises(JobStateError):
        await jobs.complete(created.job_id)
    with pytest.raises(JobStateError):
        await jobs.cancel(created.job_id)
    with pytest.raises(JobNotFoundError):
        jobs.require("nonexistent")

    topics = [e.topic for e in events]
    assert JOB_CREATED_TOPIC in topics
    assert JOB_PROGRESS_TOPIC in topics
    assert "asr.job.progress" in topics
    assert JOB_COMPLETED_TOPIC in topics


@pytest.mark.asyncio
async def test_job_center_submit_fail_and_cancellation() -> None:
    bus = EntityBus()
    events: list[BusEvent] = []
    bus.subscribe("*", lambda evt: events.append(evt), plugin_id="core")

    jobs = JobCenter(bus=bus, secret_provider=lambda: ["my-nas-password"])

    # 1. Failing background job redacts secret and emits job.failed + plugin.fault
    async def failing_worker(ctx: JobContext) -> None:
        await ctx.update(progress=0.25, stage="connecting")
        raise RuntimeError("auth failed for my-nas-password")

    failed_job = await jobs.submit("nas.sync", failing_worker, plugin_id="plugin-nas")
    settled_fail = await jobs.wait(failed_job.job_id)
    assert settled_fail.status == JobStatus.FAILED
    assert "my-nas-password" not in settled_fail.error
    assert "[REDACTED]" in settled_fail.error
    assert any(e.topic == JOB_FAILED_TOPIC for e in events)
    assert any(e.topic == FAULT_TOPIC for e in events)

    # 2. Cancelling a running background job triggers CancellationToken and emits job.cancelled
    started_evt = asyncio.Event()
    cleanup_called = False

    async def long_worker(ctx: JobContext) -> None:
        nonlocal cleanup_called
        ctx.token.on_cancel(lambda: _mark_cleanup())
        await ctx.update(progress=0.1, stage="working")
        started_evt.set()
        await ctx.token.wait()
        ctx.raise_if_cancelled()

    def _mark_cleanup() -> None:
        nonlocal cleanup_called
        cleanup_called = True

    cancellable = await jobs.submit("curation.daily", long_worker, plugin_id="plugin-curator")
    await started_evt.wait()
    cancelled = await jobs.cancel(cancellable.job_id, reason="user pressed Esc")
    assert cancelled.status == JobStatus.CANCELLED
    assert cleanup_called is True
    assert any(e.topic == JOB_CANCELLED_TOPIC and e.payload["job_id"] == cancellable.job_id for e in events)

    with pytest.raises(JobCancelledError):
        await jobs.update(cancellable.job_id, progress=0.9)
    with pytest.raises(JobStateError):
        await jobs.complete(cancellable.job_id)

    # 3. Bulk plugin cancellation cancels pending/running jobs for unloaded plugin
    pending_job = await jobs.create("curate", plugin_id="plugin-curator")
    assert await jobs.cancel_plugin("plugin-curator") == 1
    assert jobs.require(pending_job.job_id).status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_capability_timeout_early_stream_close_and_extra_edge_cases() -> None:
    bus = EntityBus()
    reg = CapabilityRegistry(bus=bus)

    async def slow_handler(req: dict[str, Any]) -> CapabilityResult:
        await asyncio.sleep(1.0)
        return CapabilityResult.ok({})

    cancelled_producer = False

    async def infinite_stream(req: dict[str, Any], stream: CapabilityStream) -> CapabilityResult:
        nonlocal cancelled_producer
        try:
            for idx in range(100):
                await stream.emit_delta(f"part-{idx}")
                await asyncio.sleep(0.01)
            return CapabilityResult.ok({"done": True})
        except asyncio.CancelledError:
            cancelled_producer = True
            raise

    def sync_gen(req: dict[str, Any]):
        yield "s1"
        yield "s2"

    reg.register("slow.op.v1", slow_handler, plugin_id="plugin-slow")
    reg.register_streaming("inf.stream.v1", infinite_stream, plugin_id="plugin-stream")
    reg.register_streaming("sync.gen.v1", sync_gen, plugin_id="plugin-sync")

    timed_out = await reg.call("slow.op.v1", {}, timeout=0.01, isolate_errors=True)
    assert timed_out.ok is False
    assert timed_out.data["error_code"] == ErrorCode.TIMEOUT

    stream_gen = reg.stream("inf.stream.v1", {})
    first = await anext(stream_gen)
    assert first.event == "delta" and first.data == "part-0"
    await stream_gen.aclose()
    assert cancelled_producer is True

    sync_chunks = [c async for c in reg.stream("sync.gen.v1", {})]
    assert [c.data for c in sync_chunks] == ["s1", "s2"]

    # DomainRegistry unregister & replace + JobCenter list_jobs / duplicate / start errors
    domains = DomainRegistry()
    s1 = _FakeMediaSource()
    s2 = _FakeMediaSource()
    domains.register_media_source("feed", s1, plugin_id="plugin-a")
    domains.register_media_source("feed", s2, plugin_id="plugin-a", replace=True)
    assert domains.get_source("media", "feed") is s2
    assert domains.unregister("media", "feed", plugin_id="plugin-a") is True
    assert domains.unregister("media", "feed", plugin_id="plugin-a") is False

    jobs = JobCenter(bus=bus)
    j = await jobs.create("index", plugin_id="plugin-idx", job_id="fixed-1")
    with pytest.raises(JobStateError):
        await jobs.create("index", plugin_id="plugin-idx", job_id="fixed-1")
    await jobs.start(j.job_id)
    with pytest.raises(JobStateError):
        await jobs.start(j.job_id)
    failed = await jobs.fail(j.job_id, "disk full", error_code=ErrorCode.INTERNAL_ERROR)
    assert failed.status == JobStatus.FAILED
    assert failed.error_code == ErrorCode.INTERNAL_ERROR
    assert len(jobs.list_jobs(plugin_id="plugin-idx", status=JobStatus.FAILED, kind="index")) == 1


@pytest.mark.asyncio
async def test_capability_version_validation_timeouts_and_callables() -> None:
    bus = EntityBus()
    faults: list[BusEvent] = []
    bus.subscribe(FAULT_TOPIC, lambda evt: faults.append(evt), plugin_id="core")
    reg = CapabilityRegistry(bus=bus)

    # Newline injection rejected in plugin_id and capability_id
    with pytest.raises(PathSafetyError):
        validate_plugin_id("plugin-a\n")
    with pytest.raises(InvalidCapabilityIdError):
        reg.register("llm.chat.v1\n", lambda req: req, plugin_id="plugin-a")

    reg.register("llm.chat.v2", lambda req: {"v": 2}, plugin_id="plugin-a")
    reg.register("llm.chat.v10", lambda req: {"v": 10}, plugin_id="plugin-a")
    assert [r.version for r in reg.list_versions("llm.chat")] == [2, 10]
    latest = reg.latest_version("llm.chat")
    assert latest is not None and latest.capability_id == "llm.chat.v10"

    # Unversioned or invalid IDs raise InvalidCapabilityIdError on call/call_sync/stream/resolve
    for bad_cap in ("llm.chat", "INVALID.v1", "llm.chat.v1\n"):
        with pytest.raises(InvalidCapabilityIdError):
            reg.resolve(bad_cap)
        with pytest.raises(InvalidCapabilityIdError):
            await reg.call(bad_cap)
        with pytest.raises(InvalidCapabilityIdError):
            reg.call_sync(bad_cap)
        with pytest.raises(InvalidCapabilityIdError):
            async for _ in reg.stream(bad_cap):
                pass

    # Invalid timeout values raise InvalidArgumentError
    for bad_timeout in (0, -0.5, float("nan"), float("inf"), True):
        with pytest.raises(InvalidArgumentError):
            await reg.call("llm.chat.v2", {}, timeout=bad_timeout)  # type: ignore[arg-type]

    # Callable objects (async __call__, async generator __call__) & keyword-only stream parameter
    class AsyncCallable:
        async def __call__(self, req: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(0.001)
            return {"echo": req.get("x")}

    class AsyncGenCallable:
        async def __call__(self, req: dict[str, Any]):
            yield f"a-{req.get('n', 0)}"
            yield f"b-{req.get('n', 0)}"

    async def kw_stream_handler(req: dict[str, Any], *, stream: CapabilityStream) -> CapabilityResult:
        await stream.emit_delta("kw-part")
        return CapabilityResult.ok({"done": True})

    def sync_returning_async_iter(req: dict[str, Any]):
        async def _gen():
            yield "x"
            yield "y"

        return _gen()

    reg.register("obj.async.v1", AsyncCallable(), plugin_id="plugin-obj")
    reg.register_streaming("obj.gen.v1", AsyncGenCallable(), plugin_id="plugin-obj")
    reg.register_streaming("kw.stream.v1", kw_stream_handler, plugin_id="plugin-obj")
    reg.register("wrap.iter.v1", sync_returning_async_iter, plugin_id="plugin-obj")

    assert reg.call_sync("obj.async.v1", {"x": 9}).data == {"echo": 9}
    assert [c.data for c in [x async for x in reg.stream("obj.gen.v1", {"n": 3})]] == ["a-3", "b-3"]
    kw_chunks = [c async for c in reg.stream("kw.stream.v1", {})]
    assert [c.event for c in kw_chunks] == ["delta", "result"]
    assert reg.call_sync("wrap.iter.v1").data == {"chunks": ["x", "y"]}

    # Sync fault inside running event loop delivers plugin.fault before returning
    def sync_boom(req: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("sync boom")

    reg.register("sync.boom.v1", sync_boom, plugin_id="plugin-obj")
    sync_fail = reg.call_sync("sync.boom.v1", isolate_errors=True)
    assert sync_fail.ok is False and sync_fail.data["error_code"] == ErrorCode.PLUGIN_FAULT
    assert len(faults) == 1 and faults[0].payload["component"] == "capability:sync.boom.v1"


@pytest.mark.asyncio
async def test_streaming_timeout_isolation_and_consumer_pause_safety() -> None:
    bus = EntityBus()
    faults: list[BusEvent] = []
    bus.subscribe(FAULT_TOPIC, lambda evt: faults.append(evt), plugin_id="core")
    reg = CapabilityRegistry(bus=bus)

    # 1. Consumer pausing between async generator chunks must NOT cancel outer caller task
    async def fast_gen(req: dict[str, Any]):
        yield "first"
        yield "second"

    reg.register_streaming("gen.pause.v1", fast_gen, plugin_id="plugin-gen")
    gen = reg.stream("gen.pause.v1", {}, timeout=0.03, isolate_errors=True)
    c1 = await anext(gen)
    assert c1.data == "first"
    await asyncio.sleep(0.06)  # Outer sleep must succeed without stray CancelledError
    c2 = await anext(gen)
    assert c2.event == "error" and c2.done is True
    assert c2.result is not None and c2.result.data["error_code"] == ErrorCode.TIMEOUT

    # 2. Callback stream timeout cancels producer, sets stream.is_cancelled, emits plugin.fault, yields error chunk
    cb_cancelled = False

    async def slow_cb(req: dict[str, Any], stream: CapabilityStream) -> CapabilityResult:
        nonlocal cb_cancelled
        try:
            await stream.emit_delta("part-1")
            await asyncio.sleep(1.0)
            return CapabilityResult.ok({"done": True})
        except asyncio.CancelledError:
            cb_cancelled = stream.is_cancelled
            raise

    reg.register_streaming("cb.slow.v1", slow_cb, plugin_id="plugin-cb")
    chunks = [c async for c in reg.stream("cb.slow.v1", {}, timeout=0.03, isolate_errors=True)]
    assert cb_cancelled is True
    assert [c.event for c in chunks] == ["delta", "error"]
    assert chunks[-1].done is True
    assert chunks[-1].result is not None and chunks[-1].result.data["error_code"] == ErrorCode.TIMEOUT

    with pytest.raises(CapabilityInvocationError) as exc_info:
        async for _ in reg.stream("cb.slow.v1", {}, timeout=0.02, isolate_errors=False):
            pass
    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert any(f.payload["component"] == "capability:cb.slow.v1" for f in faults)


@pytest.mark.asyncio
async def test_sdk_interface_consistency_with_core_registries() -> None:
    bus = EntityBus()
    reg = CapabilityRegistry(bus=bus)
    domains = DomainRegistry()

    class SdkSource(SdkMediaSource):
        source_id = "sdk-media"
        display_name = "SDK Media Display"

    reg_entry = domains.register_media_source(" sdk-media ", SdkSource(), plugin_id="plugin-sdk")
    assert reg_entry.title == "SDK Media Display"
    assert domains.has("media", " sdk-media ")
    assert domains.get("media", " sdk-media ") is reg_entry

    ctx = PluginContext("plugin-sdk", capabilities=reg, bus=bus)
    seen_protocol = False

    async def sdk_cap(req: dict[str, Any]) -> SdkCapabilityResult:
        if req.get("fail"):
            return SdkCapabilityResult.fail("bad cfg", {"hint": "set key"}, error_code="not_configured")
        return SdkCapabilityResult.ok(reply="from-sdk", value=req.get("v", 1))

    async def sdk_stream(req: dict[str, Any], stream: CapabilityStream) -> SdkCapabilityResult:
        nonlocal seen_protocol
        seen_protocol = isinstance(stream, SdkCapabilityStreamProtocol)
        await stream.emit_event("stage", {"step": 1})
        await stream.emit_delta("hi", event="delta", data={"idx": 0})
        await asyncio.to_thread(stream.emit_sync, "thread_evt", {"ok": True})
        return SdkCapabilityResult.ok(done=True)

    ctx.register_capability("sdk.echo.v1", sdk_cap, priority=250, metadata={"tier": "gold"})
    ctx.register_streaming_capability("sdk.stream.v1", sdk_stream)

    entry = reg.resolve("sdk.echo.v1")
    assert entry.priority == 250 and entry.metadata == {"tier": "gold"}

    ok_res = await reg.call("sdk.echo.v1", {"v": 42})
    assert ok_res.ok is True and ok_res.data == {"reply": "from-sdk", "value": 42}
    assert ok_res.provider_plugin_id == "plugin-sdk"

    fail_res = await reg.call("sdk.echo.v1", {"fail": True})
    assert fail_res.ok is False
    assert fail_res.error == "bad cfg"
    assert fail_res.data == {"hint": "set key", "error_code": "not_configured"}
    assert fail_res.provider_plugin_id == "plugin-sdk"

    # Core CapabilityResult.ok/fail also support SDK kwargs & positional data without mutating inputs
    caller_hint = {"k": 1}
    c_ok = CapabilityResult.ok(reply="ok", provider_plugin_id="p1")
    c_fail = CapabilityResult.fail("err", caller_hint, error_code=ErrorCode.NOT_CONFIGURED, extra=2)
    assert c_ok.data == {"reply": "ok"}
    assert c_fail.data == {"k": 1, "extra": 2, "error_code": "not_configured"}
    assert caller_hint == {"k": 1}

    forwarded: list[dict[str, Any]] = []
    stream_res = await reg.call_stream("sdk.stream.v1", {}, on_chunk=forwarded.append)
    assert seen_protocol is True
    assert stream_res.ok is True and stream_res.data == {"done": True}
    assert [c["event"] for c in forwarded] == ["stage", "delta", "thread_evt"]
    assert forwarded[1]["data"] == {"idx": 0, "text": "hi"}

    ctx.cleanup()
    assert not reg.has("sdk.echo.v1")
    assert not reg.has("sdk.stream.v1")


@pytest.mark.asyncio
async def test_bus_revocation_isolation_redaction_and_job_state_edge_cases() -> None:
    # Secret redaction handles overlapping prefix secrets without leaking suffix
    scrubbed = redact_secrets("failed with secr-very-long-value", ["secr", "secr-very-long-value"])
    assert scrubbed == "failed with [REDACTED]"

    bus = EntityBus()
    with pytest.raises(PathSafetyError):
        bus.subscribe("entity.x", lambda e: None, plugin_id="../escape")
    with pytest.raises(InvalidArgumentError):
        bus.unsubscribe_plugin("  ")
    with pytest.raises(InvalidArgumentError):
        bus.history(limit=-1)

    # Revoking plugin-b during an async subscriber on plugin-a skips plugin-b immediately
    called: list[str] = []

    async def sub_a(evt: BusEvent) -> None:
        called.append("a")
        bus.unsubscribe_plugin("plugin-b")

    bus.subscribe("entity.open", sub_a, plugin_id="plugin-a")
    bus.subscribe("entity.open", lambda evt: called.append("b"), plugin_id="plugin-b")
    pub = await bus.publish("entity.open", {"id": 1})
    assert called == ["a"]
    assert pub.delivered == 1

    # JobCenter edge cases: pre-cancel in job.created, self-cancel, explicit progress preservation, wait timeout & cancel_plugin race
    jobs = JobCenter(bus=bus)
    token_check = jobs.get_token((await jobs.create("prep", plugin_id="p1")).job_id)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(token_check.wait(), timeout=0.01)
    assert token_check._async_waiters == []

    # Explicit progress is preserved when subsequent metadata/stage updates occur
    j_step = await jobs.create("steps", plugin_id="p1", total_steps=4)
    await jobs.update(j_step.job_id, completed_steps=2, progress=0.75)
    prog_keep = await jobs.update(j_step.job_id, message="still 75%")
    assert prog_keep.progress == pytest.approx(0.75)

    # Pre-cancelled inside job.created prevents worker execution
    ran_pre_cancelled = False

    async def cancel_on_create(evt: BusEvent) -> None:
        if evt.payload.get("kind") == "pre.cancel":
            await jobs.cancel(evt.payload["job_id"], reason="blocked")

    sub_pre = bus.subscribe(JOB_CREATED_TOPIC, cancel_on_create, plugin_id="core")

    async def should_not_run(ctx: JobContext) -> None:
        nonlocal ran_pre_cancelled
        ran_pre_cancelled = True

    pre_rec = await jobs.submit("pre.cancel", should_not_run, plugin_id="p2")
    bus.unsubscribe(sub_pre)
    assert pre_rec.status == JobStatus.CANCELLED
    assert ran_pre_cancelled is False

    # Worker cancelling itself must not deadlock, and keyword-only optional arg worker runs
    async def self_cancelling_worker(ctx: JobContext) -> None:
        await jobs.cancel(ctx.job_id, reason="self-stop")

    self_rec = await jobs.submit("self.cancel", self_cancelling_worker, plugin_id="p2")
    settled_self = await jobs.wait(self_rec.job_id)
    assert settled_self.status == JobStatus.CANCELLED

    async def kw_only_worker(*, flag: bool = True) -> dict[str, Any]:
        return {"flag": flag}

    kw_rec = await jobs.submit("kw.job", kw_only_worker, plugin_id="p2")
    assert (await jobs.wait(kw_rec.job_id)).result == {"flag": True}

    # wait(timeout=...) raises TimeoutError without cancelling job; cancelling observer wait() also keeps job alive
    gate = asyncio.Event()

    async def gated_worker(ctx: JobContext) -> str:
        await gate.wait()
        return "finished"

    gated_rec = await jobs.submit("gated", gated_worker, plugin_id="p3")
    with pytest.raises(InvalidArgumentError):
        await jobs.wait(gated_rec.job_id, timeout=float("nan"))
    with pytest.raises(TimeoutError):
        await jobs.wait(gated_rec.job_id, timeout=0.01)

    observer = asyncio.create_task(jobs.wait(gated_rec.job_id))
    await asyncio.sleep(0.01)
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert jobs.require(gated_rec.job_id).status == JobStatus.RUNNING

    # cancel_plugin tolerates concurrent job completion without raising JobStateError
    fast_done = await jobs.submit("fast", lambda: "ok", plugin_id="p3")
    await jobs.wait(fast_done.job_id)
    gate.set()
    # Even with an already-settled job and a newly queued pending job, cancel_plugin stays safe
    p3_pending = await jobs.create("pending", plugin_id="p3")
    cancelled_n = await jobs.cancel_plugin("p3")
    assert cancelled_n >= 1
    assert jobs.require(p3_pending.job_id).status == JobStatus.CANCELLED


def test_thread_concurrency_safety_across_core_registries() -> None:
    bus = EntityBus()
    reg = CapabilityRegistry(bus=bus)
    domains = DomainRegistry()

    def worker(idx: int) -> None:
        pid = f"plugin-t{idx}"
        cap = f"thr.op{idx}.v1"
        reg.register(cap, lambda req, i=idx: {"i": i, **req}, plugin_id=pid)
        assert reg.call_sync(cap, {"k": idx}).data == {"i": idx, "k": idx}
        domains.register_media_source(f"src-{idx}", _FakeMediaSource(), plugin_id=pid)
        sub = bus.subscribe("entity.*", lambda evt: None, plugin_id=pid)
        assert reg.list_capabilities(plugin_id=pid)[0].capability_id == cap
        assert domains.has("media", f"src-{idx}")
        assert bus.subscription_count(plugin_id=pid) >= 1
        bus.unsubscribe(sub)
        assert reg.revoke_plugin(pid) == 1
        assert domains.revoke_plugin(pid) == 1
        assert bus.unsubscribe_plugin(pid) == 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker, i) for i in range(40)]
        for fut in futures:
            fut.result()

    assert reg.list_capabilities() == []
    assert domains.counts() == {"media": 0, "text": 0, "book": 0}
    assert bus.subscription_count() == 0
