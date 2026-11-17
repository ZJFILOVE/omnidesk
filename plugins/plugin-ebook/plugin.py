"""OmniDesk v2 plugin-ebook: EPUB/MOBI/AZW3/TXT reader, companion actions, and SM-2 vocab."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import BasePlugin, CapabilityResult, DockWidgetSpec, PluginContext, WorkspaceTabSpec

if __package__:
    from .ebook_lib.companion import explain_sentence, lookup_term, search_media_clips, sync_with_remote
    from .ebook_lib.errors import EbookPathSafetyError, EbookPluginError, InvalidEbookArgError
    from .ebook_lib.parsers import probe_file_format, resolve_calibre_bin
    from .ebook_lib.security import parse_library_roots, resolve_omnidesk_vocab_path, resolve_safe_under_root, validate_library_root
    from .ebook_lib.ui_specs import build_dock_schema, build_workspace_schema
    from .ebook_lib.vocab import VocabStore
    from .sources import EbookBookSource
else:
    _PLUGIN_ROOT = str(Path(__file__).resolve().parent)
    if _PLUGIN_ROOT not in sys.path:
        sys.path.insert(0, _PLUGIN_ROOT)
    from ebook_lib.companion import explain_sentence, lookup_term, search_media_clips, sync_with_remote
    from ebook_lib.errors import EbookPathSafetyError, EbookPluginError, InvalidEbookArgError
    from ebook_lib.parsers import probe_file_format, resolve_calibre_bin
    from ebook_lib.security import parse_library_roots, resolve_omnidesk_vocab_path, resolve_safe_under_root, validate_library_root
    from ebook_lib.ui_specs import build_dock_schema, build_workspace_schema
    from ebook_lib.vocab import VocabStore
    from sources import EbookBookSource

_BOOK_ACTIONS = (
    "open",
    "list_books", "fetch_spread", "get_spread", "get_book", "book_info", "list_vocab",
    "update_progress", "set_progress", "probe_format", "lookup_term", "explain_sentence",
    "search_clips", "clip_jump", "sync",
)
_VOCAB_ACTIONS = (
    "add", "list", "get", "review", "due", "import", "export", "delete", "remove",
    "stats", "sync", "lookup_term", "explain_sentence",
)


class EbookPlugin(BasePlugin):
    """Hybrid domain=book plugin providing `book.source.v1` and `vocab.sm2.v1`."""

    def __init__(self) -> None:
        self._vocab_store: VocabStore | None = None
        self._book_source: EbookBookSource | None = None

    def _ensure_stores(self, ctx: PluginContext | None = None) -> tuple[VocabStore, EbookBookSource]:
        active_ctx = ctx or self.ctx
        if self._vocab_store is None:
            pid = active_ctx.plugin_id if active_ctx is not None else "plugin-ebook"
            self._vocab_store = VocabStore(lambda: resolve_omnidesk_vocab_path(self.ctx or active_ctx, pid), plugin_id=pid)
        if self._book_source is None:
            self._book_source = EbookBookSource(active_ctx, self._vocab_store)
        else:
            self._book_source.bind_context(active_ctx)
        return self._vocab_store, self._book_source

    @property
    def book_source(self) -> EbookBookSource:
        return self._ensure_stores()[1]

    @property
    def vocab_store(self) -> VocabStore:
        return self._ensure_stores()[0]

    def _cfg(self, key: str, default: Any = None) -> Any:
        return default if self.ctx is None else self.ctx.get_config(key, default)

    def _sync_enabled(self) -> bool:
        return bool(self._cfg("sync_enabled", True))

    def _target_language(self) -> str:
        return str(self._cfg("target_language", "zh-CN") or "zh-CN").strip()

    def _auto_jump_nas(self) -> bool:
        return bool(self._cfg("auto_jump_nas", True))

    async def on_load(self, ctx: PluginContext) -> None:
        """Register BookSource, capabilities, declarative UI specs, and bus handlers quickly."""
        await super().on_load(ctx)
        _, source = self._ensure_stores(ctx)
        ctx.register_book_source(source)
        ctx.register_capability("book.source.v1", self.handle_book_source)
        ctx.register_capability("book.ebook.v1", self.handle_book_source)
        ctx.register_capability("vocab.sm2.v1", self.handle_vocab_sm2)
        ctx.subscribe("entity.vocab.add", self._on_entity_vocab_add)
        ctx.register_workspace_tab(
            WorkspaceTabSpec(
                tab_id="ebook-studio",
                title="Ebook & SM-2",
                domain="book",
                badge="EBOOK",
                ui_schema=self._render_workspace(),
                render=self._render_workspace,
                on_action=self.handle_ui_action,
            )
        )
        ctx.register_dock_widget(
            DockWidgetSpec(
                widget_id="ebook-companion",
                title="划词词源与长难句伴读",
                supported_domains=["book", "text"],
                ui_schema=build_dock_schema(),
                render=build_dock_schema,
                on_action=self.handle_ui_action,
            )
        )

    async def on_unload(self) -> None:
        if self._book_source is not None:
            self._book_source.clear_cache()
            self._book_source.bind_context(None)

    async def on_health_check(self) -> dict[str, Any]:
        plugin_id = self.ctx.plugin_id if self.ctx is not None else "plugin-ebook"
        raw_roots = parse_library_roots(self._cfg("library_roots", ""))
        calibre_raw = str(self._cfg("calibre_bin", "") or "").strip()
        vocab_path = resolve_omnidesk_vocab_path(self.ctx, plugin_id)
        vocab_count = len(self.vocab_store.known_words()) if vocab_path.exists() and not vocab_path.is_symlink() else 0
        caps = self.ctx.capabilities if self.ctx is not None else None
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "provides": ["book.source.v1", "vocab.sm2.v1"],
            "library_roots_configured": len(raw_roots),
            "calibre_configured": bool(calibre_raw),
            "calibre_available": bool(calibre_raw and resolve_calibre_bin(calibre_raw)),
            "vocab_count": vocab_count,
            "optional_capabilities": {
                "llm_chat": bool(caps and caps.has("llm.chat.v1")),
                "llm_translate": bool(caps and caps.has("llm.translate.v1")),
                "media_clip_jump": bool(caps and caps.has("media.clip_jump.v1")),
                "book_sync": bool(caps and caps.has("book.sync.v1")),
            },
        }

    def _on_entity_vocab_add(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            return
        raw_word = payload.get("word")
        if not isinstance(raw_word, str) or not raw_word.strip():
            return
        try:
            self.vocab_store.add_card(
                {
                    "word": raw_word,
                    "context_sentence": str(payload.get("context_sentence") or ""),
                    "source_book": str(payload.get("source_title") or payload.get("source_book") or ""),
                    "meaning": str(payload.get("meaning") or ""),
                    "etymology": str(payload.get("etymology") or ""),
                }
            )
        except EbookPluginError:
            pass

    async def _resolve_lookup_payload(self, payload: dict[str, Any], source: EbookBookSource) -> dict[str, Any]:
        if payload.get("book_id") and not (payload.get("source_book") or payload.get("book_title")):
            try:
                book = await source.get_book(str(payload["book_id"]))
                payload["source_book"] = book.title
            except EbookPluginError:
                pass
        return payload

    async def handle_book_source(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Dispatch `book.source.v1` actions with fail-closed safety guarantees."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "list_books").strip()
        vocab, source = self._ensure_stores()
        try:
            if action == "open":
                raw_path = str(payload.get("file_path") or "").strip()
                if not raw_path:
                    return CapabilityResult.fail("open 需要下载回执的 file_path", error_code="invalid_argument")
                book = await source.open_file(raw_path)
                await self.ctx.emit("entity.book.open", {"book_id": book.id, "source_id": "plugin-ebook", "domains": ["book"]})
                return CapabilityResult.ok({"book_id": book.id, "source_id": "plugin-ebook", "book": book.model_dump()})
            if action == "list_books":
                books = await source.list_books(
                    format_filter=str(payload.get("format_filter") or payload.get("format") or ""),
                    query=str(payload.get("query") or ""),
                )
                return CapabilityResult.ok(
                    {
                        "books": [b.model_dump() for b in books],
                        "count": len(books),
                        "configured": bool(parse_library_roots(self._cfg("library_roots", ""))),
                    }
                )
            if action in ("fetch_spread", "get_spread"):
                spread = await source.fetch_spread(
                    str(payload.get("book_id") or ""),
                    payload.get("page_number", payload.get("page", 1)),  # type: ignore[arg-type]
                    translate=bool(payload.get("translate", False)),
                    record_progress=bool(payload.get("record_progress", True)),
                )
                return CapabilityResult.ok(spread.model_dump())
            if action in ("get_book", "book_info"):
                return CapabilityResult.ok((await source.get_book(str(payload.get("book_id") or ""))).model_dump())
            if action == "list_vocab":
                cards = await source.list_vocab()
                return CapabilityResult.ok({"cards": [c.model_dump() for c in cards], "count": len(cards)})
            if action in ("update_progress", "set_progress"):
                page = payload.get("current_page", payload.get("page_number", payload.get("page", 1)))
                return CapabilityResult.ok(await source.update_progress(str(payload.get("book_id") or ""), page))  # type: ignore[arg-type]
            if action == "probe_format":
                return await self._handle_probe_format(payload, source)
            if action == "lookup_term":
                enriched = await self._resolve_lookup_payload(payload, source)
                return await lookup_term(
                    self.ctx,
                    vocab,
                    enriched,
                    target_language=self._target_language(),
                    auto_jump_nas=self._auto_jump_nas(),
                    sync_enabled=self._sync_enabled(),
                )
            if action == "explain_sentence":
                return await explain_sentence(self.ctx, payload, target_language=self._target_language())
            if action in ("search_clips", "clip_jump"):
                query = str(payload.get("query") or payload.get("word") or "").strip()
                if not query:
                    return CapabilityResult.fail("query must be a non-empty string", error_code="invalid_argument")
                return CapabilityResult.ok(
                    await search_media_clips(
                        self.ctx,
                        query,
                        limit=int(payload.get("limit", 5) or 5),
                        auto_open_player=bool(payload.get("auto_open_player", False)),
                    )
                )
            if action == "sync":
                return CapabilityResult.ok(
                    await sync_with_remote(
                        self.ctx,
                        sync_enabled=self._sync_enabled(),
                        action="sync_snapshot",
                        payload={"plugin_id": source.source_id, "vocab_count": len(vocab.known_words())},
                    )
                )
            return CapabilityResult.fail(
                f"Unsupported book.source.v1 action: {action!r}",
                data={"supported_actions": list(_BOOK_ACTIONS)},
                error_code="invalid_argument",
            )
        except EbookPluginError as exc:
            return CapabilityResult.fail(str(exc), error_code=exc.code)

    async def _handle_probe_format(self, payload: Mapping[str, Any], source: EbookBookSource) -> CapabilityResult:
        if payload.get("book_id"):
            _, file_path, _ = source._resolve_book_entry(str(payload["book_id"]))
            return CapabilityResult.ok(probe_file_format(file_path))
        raw_path = str(payload.get("file_path") or "").strip()
        if not raw_path:
            raise InvalidEbookArgError("probe_format requires book_id or file_path")
        valid_roots = [r for r in (validate_library_root(x) for x in parse_library_roots(self._cfg("library_roots", ""))) if r]
        for root in valid_roots:
            try:
                safe = resolve_safe_under_root(root, raw_path)
                if safe.exists():
                    return CapabilityResult.ok(probe_file_format(safe))
            except EbookPathSafetyError:
                continue
        raise EbookPathSafetyError(f"Path {raw_path!r} escapes configured library_roots or does not exist")

    async def handle_vocab_sm2(self, req: Mapping[str, Any] | None) -> CapabilityResult:
        """Dispatch `vocab.sm2.v1` actions (`add`, `list`, `review`, `due`, `import`, `export`)."""
        payload = dict(req) if req is not None else {}
        action = str(payload.get("action") or "").strip()
        vocab, source = self._ensure_stores()
        try:
            if action == "add":
                card_in = payload.get("card") if isinstance(payload.get("card"), Mapping) else payload
                card, created = vocab.add_card(card_in)
                sync_res = await sync_with_remote(
                    self.ctx,
                    sync_enabled=self._sync_enabled(),
                    action="sync_vocab",
                    payload={"plugin_id": vocab._plugin_id, "cards": [card]},
                )
                return CapabilityResult.ok({"card": card, "created": created, "already_exists": not created, "sync_status": sync_res})
            if action == "list":
                cards = vocab.list_cards(
                    query=str(payload.get("query") or ""),
                    source_book=str(payload.get("source_book") or ""),
                    due_only=bool(payload.get("due_only", False)),
                )
                return CapabilityResult.ok({"cards": cards, "count": len(cards)})
            if action == "get":
                target = str(payload.get("word") or payload.get("id") or "")
                if (card := vocab.get_card(target)) is None:
                    return CapabilityResult.fail(f"Vocabulary card not found: {target!r}", error_code="not_found")
                return CapabilityResult.ok({"card": card})
            if action == "review":
                target = str(payload.get("word") or payload.get("id") or "")
                quality = payload.get("quality", payload.get("grade"))
                card = vocab.review_card(target, quality)
                sync_res = await sync_with_remote(
                    self.ctx,
                    sync_enabled=self._sync_enabled(),
                    action="sync_vocab",
                    payload={"plugin_id": vocab._plugin_id, "cards": [card]},
                )
                return CapabilityResult.ok({"card": card, "quality": quality, "sync_status": sync_res})
            if action == "due":
                raw_limit = payload.get("limit")
                due = vocab.due_cards(as_of=payload.get("as_of") or payload.get("now"), limit=int(raw_limit) if raw_limit is not None else None)
                return CapabilityResult.ok({"cards": due, "count": len(due), "total_cards": len(vocab.known_words())})
            if action == "import":
                summary = vocab.import_cards(payload.get("cards"))  # type: ignore[arg-type]
                if summary["imported"] > 0:
                    summary["sync_status"] = await sync_with_remote(
                        self.ctx,
                        sync_enabled=self._sync_enabled(),
                        action="sync_vocab",
                        payload={"plugin_id": vocab._plugin_id, "cards": summary["cards"]},
                    )
                return CapabilityResult.ok(summary)
            if action == "export":
                return CapabilityResult.ok(vocab.export_cards())
            if action in ("delete", "remove"):
                target = str(payload.get("word") or payload.get("id") or "")
                if not vocab.delete_card(target):
                    return CapabilityResult.fail(f"Vocabulary card not found: {target!r}", error_code="not_found")
                return CapabilityResult.ok({"deleted": True, "word": target})
            if action == "stats":
                return CapabilityResult.ok(
                    {
                        "total_cards": len(vocab.list_cards()),
                        "due_count": len(vocab.due_cards()),
                        "daily_review_limit": int(self._cfg("daily_review_limit", 40) or 40),
                        "storage_path": str(vocab.storage_path),
                    }
                )
            if action == "sync":
                exported = vocab.export_cards()
                return CapabilityResult.ok(
                    await sync_with_remote(
                        self.ctx,
                        sync_enabled=self._sync_enabled(),
                        action="sync_vocab",
                        payload={"plugin_id": vocab._plugin_id, "cards": exported["cards"]},
                    )
                )
            if action == "lookup_term":
                enriched = await self._resolve_lookup_payload(payload, source)
                return await lookup_term(
                    self.ctx, vocab, enriched, target_language=self._target_language(),
                    auto_jump_nas=self._auto_jump_nas(), sync_enabled=self._sync_enabled(),
                )
            if action == "explain_sentence":
                return await explain_sentence(self.ctx, payload, target_language=self._target_language())
            return CapabilityResult.fail(
                f"Unsupported vocab.sm2.v1 action: {action!r}",
                data={"supported_actions": list(_VOCAB_ACTIONS)},
                error_code="invalid_argument",
            )
        except EbookPluginError as exc:
            return CapabilityResult.fail(str(exc), error_code=exc.code)

    def _render_workspace(self) -> dict[str, Any]:
        roots = parse_library_roots(self._cfg("library_roots", ""))
        due_count = len(self.vocab_store.due_cards()) if self.vocab_store.storage_path.exists() else 0
        return build_workspace_schema(roots_count=len(roots), due_count=due_count, target_language=self._target_language())

    async def handle_ui_action(self, action_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        state_map = dict(state) if state is not None else {}
        if action_id in ("lookup_term", "explain_sentence"):
            res = await self.handle_book_source({"action": action_id, **state_map})
            return {"ok": res.ok, "result": res.data, "error": res.error}
        return {"ok": False, "error": f"Unsupported action: {action_id}"}


def create_plugin() -> BasePlugin:
    """Manifest entrypoint returning an `EbookPlugin` instance."""
    return EbookPlugin()
