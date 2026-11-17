"""Core curation engine implementing `recommend.feed.v1` actions."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omnidesk_sdk import BookItem, BookSpread, MediaItem, PluginContext, TextArticle, TextFeedItem, VocabCard

if __package__:
    from .archive import ArchiveRehydrator, resolve_repo_paths, sync_repo
    from .models import (
        ALLOWED_DAILY_PROJECTS,
        ALLOWED_REPEAT_POLICIES,
        DEFAULT_MUSIC_REMOTE,
        DEFAULT_OBSIDIAN_REMOTE,
        CuratorError,
        DateStats,
        HydratedArchive,
        InteractiveSession,
        RepoPaths,
        RepoSyncStatus,
        atomic_write_json,
        current_shanghai_date,
        is_valid_iso_date,
        parse_query_filter,
        read_json_strict,
        safe_child_path,
        safe_identifier,
    )
    from .store import (
        book_identity_key,
        build_provenance_index,
        load_id_list,
        load_interactive_session,
        lookup_provenance_matches,
        match_item_provenance,
        media_identity_key,
        media_matches,
        parse_incoming_book_item,
        parse_incoming_media_item,
        parse_incoming_text_item,
        save_id_list,
        save_interactive_session,
        sync_vocab_to_sm2_idempotent,
        text_identity_key,
        text_matches,
        update_markdown_preserving_notes,
        validate_ledgers_and_dedup,
        validate_publication_evidence,
    )
else:
    from archive import ArchiveRehydrator, resolve_repo_paths, sync_repo  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        ALLOWED_DAILY_PROJECTS,
        ALLOWED_REPEAT_POLICIES,
        DEFAULT_MUSIC_REMOTE,
        DEFAULT_OBSIDIAN_REMOTE,
        CuratorError,
        DateStats,
        HydratedArchive,
        InteractiveSession,
        RepoPaths,
        RepoSyncStatus,
        atomic_write_json,
        current_shanghai_date,
        is_valid_iso_date,
        parse_query_filter,
        read_json_strict,
        safe_child_path,
        safe_identifier,
    )
    from store import (  # type: ignore[no-redef]
        book_identity_key,
        build_provenance_index,
        load_id_list,
        load_interactive_session,
        lookup_provenance_matches,
        match_item_provenance,
        media_identity_key,
        media_matches,
        parse_incoming_book_item,
        parse_incoming_media_item,
        parse_incoming_text_item,
        save_id_list,
        save_interactive_session,
        sync_vocab_to_sm2_idempotent,
        text_identity_key,
        text_matches,
        update_markdown_preserving_notes,
        validate_ledgers_and_dedup,
        validate_publication_evidence,
    )


class CurationEngine:
    """Async-safe engine for archive rehydration and dual-mode writeback."""

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._lock = asyncio.Lock()
        self._state_loaded = False
        self._last_paths: RepoPaths | None = None
        self._obsidian_sync = RepoSyncStatus(name="obsidian_vault", path="")
        self._music_sync = RepoSyncStatus(name="music_daily", path="")
        self.archive = HydratedArchive()
        self.daily_media: list[MediaItem] = []
        self.daily_texts: list[TextFeedItem] = []
        self.daily_articles: dict[str, TextArticle] = {}
        self.daily_books: list[BookItem] = []
        self.daily_vocabs: list[VocabCard] = []
        self.interactive_sessions: list[InteractiveSession] = []
        self.pinned_ids: set[str] = set()
        self.dismissed_ids: set[str] = set()
        self._last_cache_hit = False
        self._last_rehydrate_date = ""

    def _current_paths(self, req: Mapping[str, Any] | None = None) -> RepoPaths:
        paths = resolve_repo_paths(self.ctx, req)
        self._last_paths = paths
        return paths

    def _load_persisted_state(self, curation_root: Path) -> None:
        self.daily_media = []
        self.daily_texts = []
        self.daily_articles = {}
        self.daily_books = []
        self.daily_vocabs = []
        self.interactive_sessions = []
        self.pinned_ids = set()
        self.dismissed_ids = set()

        daily_dir = curation_root / "daily"
        if daily_dir.is_dir():
            for fp in sorted(daily_dir.rglob("*.json")):
                raw = read_json_strict(fp, error_code="corrupted_state")
                if not isinstance(raw, dict):
                    continue
                for mj in raw.get("media_items") or []:
                    if isinstance(mj, Mapping):
                        self.daily_media.append(parse_incoming_media_item(mj, "daily_immutable"))
                for tj in raw.get("text_items") or []:
                    if isinstance(tj, Mapping):
                        fi, art = parse_incoming_text_item(tj, "news", preserve_category=True)
                        self.daily_texts.append(fi)
                        self.daily_articles[art.id] = art
                for bj in raw.get("book_items") or []:
                    if isinstance(bj, Mapping):
                        self.daily_books.append(parse_incoming_book_item(bj))
                for vj in raw.get("vocab_cards") or []:
                    if isinstance(vj, Mapping):
                        self.daily_vocabs.append(VocabCard.model_validate(vj))

        ix_dir = curation_root / "interactive" / "sessions"
        if ix_dir.is_dir():
            for fp in sorted(ix_dir.glob("*.json")):
                self.interactive_sessions.append(load_interactive_session(fp, fp.stem))

        self.pinned_ids = load_id_list(curation_root / "interactive" / "pinned.json")
        self.dismissed_ids = load_id_list(curation_root / "interactive" / "dismissed.json")

    def ensure_loaded(self, req: Mapping[str, Any] | None = None) -> RepoPaths:
        """Lazily load cached index and local persisted daily/interactive state without writing."""
        paths = self._current_paths(req)
        if not self._state_loaded:
            cached = ArchiveRehydrator.load_cached_archive(paths.curation_root)
            if cached is not None:
                self.archive = cached
            self._load_persisted_state(paths.curation_root)
            self._state_loaded = True
        return paths

    async def dispatch_action(self, req: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch `recommend.feed.v1` action, rejecting unknown actions with invalid_argument."""
        action = req.get("action")
        if not isinstance(action, str) or not action.strip():
            raise CuratorError("invalid_argument", "Missing or invalid 'action' field")
        act = action.strip()
        handlers = {
            "bootstrap_and_rehydrate": self.action_bootstrap_and_rehydrate,
            "list_archive_dates": self.action_list_archive_dates,
            "search_archive": self.action_search_archive,
            "lookup_provenance": self.action_lookup_provenance,
            "publish_daily_bundle": self.action_publish_daily_bundle,
            "push_interactive_set": self.action_push_interactive_set,
            "pin_item": self.action_pin_item,
            "dismiss_item": self.action_dismiss_item,
            "clear_interactive": self.action_clear_interactive,
            "status": self.action_status,
            "list_sets": self.action_list_sets,
            "get_set": self.action_get_set,
            "get_date_bundle": self.action_get_date_bundle,
        }
        handler = handlers.get(act)
        if handler is None:
            raise CuratorError("invalid_argument", f"Unsupported recommend.feed.v1 action: {act!r}")
        async with self._lock:
            return await handler(req)

    async def action_bootstrap_and_rehydrate(self, req: Mapping[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        paths = self._current_paths(req)
        cfg_sync = bool(self.ctx.get_config("sync_remote", True))
        pull_remote = bool(req["pull_remote"]) if "pull_remote" in req else cfg_sync
        clone_if_missing = bool(req["clone_if_missing"]) if "clone_if_missing" in req else pull_remote
        force_full = bool(req.get("force_full", False))
        sync_vocab = bool(req.get("sync_vocab", True))

        remote_urls = req.get("remote_urls") if isinstance(req.get("remote_urls"), Mapping) else {}
        obs_url = str(req.get("obsidian_vault_url") or remote_urls.get("obsidian_vault") or DEFAULT_OBSIDIAN_REMOTE)
        mus_url = str(req.get("music_daily_url") or remote_urls.get("music_daily") or DEFAULT_MUSIC_REMOTE)

        obs_sync = await sync_repo(
            self.ctx,
            "obsidian_vault",
            paths.obsidian_vault_dir,
            obs_url,
            pull_remote=pull_remote,
            clone_if_missing=clone_if_missing,
        )
        mus_sync = await sync_repo(
            self.ctx,
            "music_daily",
            paths.music_daily_dir,
            mus_url,
            pull_remote=pull_remote,
            clone_if_missing=clone_if_missing,
        )
        self._obsidian_sync = obs_sync
        self._music_sync = mus_sync

        if obs_sync.status_detail == "git_clone_failed" or mus_sync.status_detail == "git_clone_failed":
            raise CuratorError("git_sync_failed", obs_sync.error or mus_sync.error or "git clone failed")

        archive, rehydrate_stats = ArchiveRehydrator.rehydrate(paths, force_full=force_full)
        self.archive = archive
        self._load_persisted_state(paths.curation_root)
        self._state_loaded = True
        self._last_cache_hit = bool(rehydrate_stats["cache_hit"])
        self._last_rehydrate_date = current_shanghai_date()

        vocab_sync = (
            await sync_vocab_to_sm2_idempotent(self.ctx, archive.vocab_cards, paths.curation_root)
            if sync_vocab
            else {"enabled": False, "added": 0, "skipped": 0}
        )

        written_count = len(archive.media_items) + len(archive.text_items)
        await self.ctx.emit(
            "entity.recommend.updated",
            {
                "mode": "daily_immutable",
                "operation_id": f"rehydrate:{self._last_rehydrate_date}",
                "domains": ["media", "text", "book"],
                "written_count": written_count,
                "dates_indexed": archive.dates_indexed,
                "auto_focus": bool(req.get("auto_focus", False)),
            },
        )

        dirty_any = obs_sync.dirty_worktree_preserved or mus_sync.dirty_worktree_preserved
        return {
            "obsidian_vault_dir": str(paths.obsidian_vault_dir),
            "music_daily_dir": str(paths.music_daily_dir),
            "curation_dir": str(paths.curation_root),
            "dirty": dirty_any,
            "dirty_worktree_preserved": dirty_any,
            "obsidian_dirty": obs_sync.dirty_worktree_preserved,
            "music_daily_dirty": mus_sync.dirty_worktree_preserved,
            "git_sync": {"obsidian_vault": obs_sync.to_dict(), "music_daily": mus_sync.to_dict()},
            "dates_indexed": archive.dates_indexed,
            "music_tracks_hydrated": archive.music_tracks_hydrated,
            "youtube_videos_hydrated": archive.youtube_videos_hydrated,
            "news_days_hydrated": archive.news_days_hydrated,
            "news_stories_hydrated": archive.news_stories_hydrated,
            "reddit_days_hydrated": archive.reddit_days_hydrated,
            "reddit_posts_hydrated": archive.reddit_posts_hydrated,
            "journal_days_hydrated": archive.journal_days_hydrated,
            "journal_papers_hydrated": archive.journal_papers_hydrated,
            "english_notes_hydrated": archive.english_notes_hydrated,
            "vocab_cards_hydrated": archive.vocab_cards_hydrated,
            "history_anomalies": list(archive.history_anomalies),
            "vocab_sync": vocab_sync,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            **rehydrate_stats,
        }

    async def action_list_archive_dates(self, req: Mapping[str, Any]) -> dict[str, Any]:
        self.ensure_loaded(req)
        limit = int(req.get("limit") or 0)
        domain = str(req.get("domain") or "").strip().lower()

        merged: dict[str, DateStats] = {
            d: DateStats(s.music, s.youtube, s.news, s.reddit, s.journal, s.english)
            for d, s in self.archive.date_stats.items()
        }
        seen_media = {m.id for m in self.archive.media_items}
        seen_text = {t.id for t in self.archive.text_items}

        for m in self.daily_media:
            if m.id in self.dismissed_ids or m.id in seen_media:
                continue
            seen_media.add(m.id)
            bdate = str(m.business_date or m.extra.get("business_date") or "")
            if is_valid_iso_date(bdate):
                st = merged.setdefault(bdate, DateStats())
                if m.kind == "music":
                    st.music += 1
                else:
                    st.youtube += 1

        for t in self.daily_texts:
            if t.id in self.dismissed_ids or t.id in seen_text:
                continue
            seen_text.add(t.id)
            if is_valid_iso_date(t.published_at):
                st = merged.setdefault(t.published_at, DateStats())
                if t.category in {"news", "reddit", "journal", "english"}:
                    setattr(st, t.category, getattr(st, t.category) + 1)

        anomalies_by_date: dict[str, list[dict[str, Any]]] = {}
        for anom in self.archive.history_anomalies:
            adate = str(anom.get("date") or "")
            if adate:
                anomalies_by_date.setdefault(adate, []).append(anom)

        dates_out: list[dict[str, Any]] = []
        for d in sorted(merged.keys(), reverse=True):
            st = merged[d]
            if domain:
                if domain == "music" and st.music == 0:
                    continue
                if domain in {"youtube", "video"} and st.youtube == 0:
                    continue
                if domain in {"news", "reddit", "journal", "english"} and getattr(st, domain) == 0:
                    continue
            row: dict[str, Any] = {"date": d, **st.to_dict()}
            if d in anomalies_by_date:
                row["anomalies"] = anomalies_by_date[d]
            dates_out.append(row)
            if limit > 0 and len(dates_out) >= limit:
                break

        return {
            "total_dates": len(merged),
            "dates": dates_out,
            "history_anomalies": list(self.archive.history_anomalies),
        }

    async def action_search_archive(self, req: Mapping[str, Any]) -> dict[str, Any]:
        self.ensure_loaded(req)
        raw_query = str(req.get("query") or "")
        date_filter = str(req.get("date") or "").strip()
        q_date, kw = parse_query_filter(raw_query)
        if not date_filter:
            date_filter = q_date
        domain = str(req.get("domain") or req.get("kind") or "all").strip().lower() or "all"
        limit = max(1, int(req.get("limit") or 50))

        media_out: list[dict[str, Any]] = []
        text_out: list[dict[str, Any]] = []
        combined: list[dict[str, Any]] = []

        if domain in {"all", "music", "video", "youtube"}:
            seen_m: set[str] = set()
            for m in (*self.daily_media, *self.archive.media_items):
                if len(combined) >= limit:
                    break
                if m.id in self.dismissed_ids or m.id in seen_m:
                    continue
                if domain == "music" and m.kind != "music":
                    continue
                if domain in {"video", "youtube"} and m.kind != "video":
                    continue
                if not media_matches(m, date_filter, kw):
                    continue
                seen_m.add(m.id)
                mj = m.model_dump()
                media_out.append(mj)
                combined.append(
                    {
                        "domain": str(m.extra.get("domain") or m.kind),
                        "id": m.id,
                        "source_id": m.source_id,
                        "title": m.title,
                        "subtitle": m.subtitle or m.creator,
                        "business_date": str(m.business_date or m.extra.get("business_date") or ""),
                        "url": m.play_url,
                        "item": mj,
                    }
                )

        if domain in {"all", "news", "reddit", "journal", "english", "text"}:
            seen_t: set[str] = set()
            for t in (*self.daily_texts, *self.archive.text_items):
                if len(combined) >= limit:
                    break
                if t.id in self.dismissed_ids or t.id in seen_t:
                    continue
                if domain not in {"all", "text"} and t.category != domain:
                    continue
                art = self.daily_articles.get(t.id) or self.archive.articles.get(t.id)
                if not text_matches(t, art, date_filter, kw):
                    continue
                seen_t.add(t.id)
                tj = t.model_dump()
                tj["business_date"] = t.published_at
                if art and art.doi and not tj.get("doi"):
                    tj["doi"] = art.doi
                text_out.append(tj)
                combined.append(
                    {
                        "domain": t.category,
                        "id": t.id,
                        "source_id": t.source_id,
                        "title": t.title,
                        "subtitle": f"{t.source_name} · {t.published_at}",
                        "business_date": t.published_at,
                        "url": t.url,
                        "item": tj,
                    }
                )

        return {
            "query": raw_query,
            "total": len(combined),
            "media_items": media_out,
            "text_items": text_out,
            "items": combined,
        }

    async def action_lookup_provenance(self, req: Mapping[str, Any]) -> dict[str, Any]:
        items = req.get("items")
        if not isinstance(items, list):
            raise CuratorError("invalid_argument", "lookup_provenance requires an 'items' list")
        self.ensure_loaded(req)
        prov = build_provenance_index(self.archive, self.daily_media, self.daily_texts, self.daily_articles)
        matches = lookup_provenance_matches(prov, items)
        return {"read_only": True, "matches": matches}

    async def action_publish_daily_bundle(self, req: Mapping[str, Any]) -> dict[str, Any]:
        if req.get("mode") != "daily_immutable":
            raise CuratorError("invalid_curation_mode", 'publish_daily_bundle requires mode == "daily_immutable"')
        if req.get("qa_verified") is not True:
            raise CuratorError("qa_not_verified", "publish_daily_bundle requires qa_verified == true")

        today = current_shanghai_date()
        business_date = str(req.get("business_date") or today).strip()
        if not is_valid_iso_date(business_date):
            raise CuratorError("invalid_argument", "business_date must be valid YYYY-MM-DD")
        if business_date < today and not bool(req.get("allow_historical_recovery", False)):
            raise CuratorError(
                "historical_backfill_forbidden",
                f"Silent backfill of past business_date ({business_date}) is forbidden",
            )
        if business_date > today:
            raise CuratorError("invalid_argument", f"Future business_date ({business_date}) is forbidden")

        raw_ev = req.get("publication_evidence")
        ev_proj = str(raw_ev.get("project") or "") if isinstance(raw_ev, Mapping) else ""
        op_id = str(req.get("operation_id") or "").strip()
        project = str(req.get("project") or ev_proj or (op_id.split(":", 1)[0] if ":" in op_id else "daily")).strip()
        if project not in ALLOWED_DAILY_PROJECTS or not safe_identifier(project):
            raise CuratorError("needs_verified_bundle", f"Unsupported project {project!r} for daily bundle")
        if not op_id:
            op_id = f"{project}:{business_date}"

        paths = self.ensure_loaded(req)
        evidence = validate_publication_evidence(
            req,
            operation_id=op_id,
            business_date=business_date,
            project=project,
            paths=paths,
        )

        try:
            media_items = [
                parse_incoming_media_item(m, "daily_immutable")
                for m in (req.get("media_items") or [])
                if isinstance(m, Mapping)
            ]
            for m in media_items:
                if not m.source_badge:
                    m.source_badge = f"每日精选 · {business_date}"
                m.curation_mode = "daily_immutable"
                m.business_date = business_date
                m.extra.setdefault("business_date", business_date)

            text_items: list[TextFeedItem] = []
            text_articles: list[TextArticle] = []
            for tj in req.get("text_items") or []:
                if not isinstance(tj, Mapping):
                    continue
                fi, art = parse_incoming_text_item(tj, "news", preserve_category=True)
                fi.published_at = fi.published_at or business_date
                fi.curation_mode = "daily_immutable"
                fi.business_date = business_date
                art.published_at = art.published_at or business_date
                art.curation_mode = "daily_immutable"
                art.business_date = business_date
                text_items.append(fi)
                text_articles.append(art)

            book_items = [
                parse_incoming_book_item(b) for b in (req.get("book_items") or []) if isinstance(b, Mapping)
            ]
            vocab_cards = [
                VocabCard.model_validate(v) for v in (req.get("vocab_cards") or []) if isinstance(v, Mapping)
            ]
        except ValidationError as exc:
            raise CuratorError("invalid_argument", f"Invalid bundle item schema: {exc}") from exc

        obs_explicit = bool(str(req.get("obsidian_vault_dir") or self.ctx.get_config("obsidian_vault_dir", "") or "").strip())
        mus_explicit = bool(str(req.get("music_daily_dir") or self.ctx.get_config("music_daily_dir", "") or "").strip())
        validate_ledgers_and_dedup(
            paths,
            self.archive,
            op_id,
            media_items,
            text_items,
            text_articles,
            obsidian_explicit=obs_explicit,
            music_explicit=mus_explicit,
        )

        for upd in req.get("markdown_updates") or []:
            if not isinstance(upd, Mapping):
                continue
            mpath = str(upd.get("path") or "").strip()
            mauto = str(upd.get("auto_block") or "").strip()
            if mpath and mauto:
                if not paths.obsidian_vault_dir.is_dir():
                    raise CuratorError("invalid_markdown_path", "obsidian_vault_dir does not exist")
                update_markdown_preserving_notes(Path(mpath), mauto, paths.obsidian_vault_dir)

        bundle_path = safe_child_path(paths.curation_root, "daily", business_date, f"{project}.json")
        # Persist strictly WITHOUT ephemeral_chat_note (今日饮食 never touches disk).
        atomic_write_json(
            bundle_path,
            {
                "schema_version": 2,
                "mode": "daily_immutable",
                "project": project,
                "operation_id": op_id,
                "business_date": business_date,
                "qa_verified": True,
                "publication_evidence": evidence,
                "media_items": [m.model_dump() for m in media_items],
                "text_items": [t.model_dump() for t in text_items],
                "book_items": [b.model_dump() for b in book_items],
                "vocab_cards": [v.model_dump() for v in vocab_cards],
            },
        )

        self._merge_daily_in_memory(media_items, text_items, text_articles, book_items, vocab_cards)
        vocab_sync = await sync_vocab_to_sm2_idempotent(self.ctx, vocab_cards, paths.curation_root)

        ephemeral_note = str(req.get("ephemeral_chat_note") or "").strip()
        if ephemeral_note:
            await self.ctx.emit(
                "entity.recommend.ephemeral_note",
                {"business_date": business_date, "operation_id": op_id, "note": ephemeral_note},
            )

        domains = [
            d
            for d, cnt in (("media", len(media_items)), ("text", len(text_items)), ("book", len(book_items) + len(vocab_cards)))
            if cnt > 0
        ] or ["core"]
        written_count = len(media_items) + len(text_items) + len(book_items)
        await self.ctx.emit(
            "entity.recommend.updated",
            {
                "mode": "daily_immutable",
                "operation_id": op_id,
                "business_date": business_date,
                "domains": domains,
                "written_count": written_count,
                "auto_focus": bool(req.get("auto_focus", False)),
            },
        )
        return {
            "mode": "daily_immutable",
            "project": project,
            "operation_id": op_id,
            "business_date": business_date,
            "bundle_path": str(bundle_path),
            "publication_evidence": evidence,
            "media_count": len(media_items),
            "text_count": len(text_items),
            "book_count": len(book_items),
            "vocab_count": len(vocab_cards),
            "vocab_sync": vocab_sync,
            "ephemeral_note_emitted": bool(ephemeral_note),
        }

    def _merge_daily_in_memory(
        self,
        media: list[MediaItem],
        texts: list[TextFeedItem],
        articles: list[TextArticle],
        books: list[BookItem],
        vocabs: list[VocabCard],
    ) -> None:
        m_idx = {m.id: i for i, m in enumerate(self.daily_media)}
        for m in media:
            if m.id in m_idx:
                self.daily_media[m_idx[m.id]] = m
            else:
                m_idx[m.id] = len(self.daily_media)
                self.daily_media.append(m)
        t_idx = {t.id: i for i, t in enumerate(self.daily_texts)}
        for t, art in zip(texts, articles):
            if t.id in t_idx:
                self.daily_texts[t_idx[t.id]] = t
            else:
                t_idx[t.id] = len(self.daily_texts)
                self.daily_texts.append(t)
            self.daily_articles[art.id] = art
        self.daily_books.extend(books)
        self.daily_vocabs.extend(vocabs)

    async def action_push_interactive_set(self, req: Mapping[str, Any]) -> dict[str, Any]:
        if req.get("mode") != "interactive_ephemeral":
            raise CuratorError("invalid_curation_mode", 'push_interactive_set requires mode == "interactive_ephemeral"')
        if req.get("record_permanent_history") is not False:
            raise CuratorError(
                "ledger_pollution_forbidden",
                "Interactive requests must explicitly set record_permanent_history=false",
            )
        session_id = str(req.get("session_id") or "").strip()
        if not safe_identifier(session_id):
            raise CuratorError("invalid_session_id", "session_id must be safe identifier (1-96 chars)")
        repeat_policy = str(req.get("repeat_policy") or "allow_any").strip()
        if repeat_policy not in ALLOWED_REPEAT_POLICIES:
            raise CuratorError("invalid_argument", f"Unsupported repeat_policy: {repeat_policy!r}")

        paths = self.ensure_loaded(req)
        prov = build_provenance_index(self.archive, self.daily_media, self.daily_texts, self.daily_articles)
        revisit_matches = 0

        try:
            in_media: list[MediaItem] = []
            for mj in req.get("media_items") or []:
                if not isinstance(mj, Mapping):
                    continue
                item = parse_incoming_media_item(mj, "interactive_ephemeral")
                item.curation_mode = "interactive_ephemeral"
                item.extra["curation_mode"] = "interactive_ephemeral"
                if not item.source_badge:
                    item.source_badge = "AI 灵感推荐 · 可重温"
                if prev := match_item_provenance(prov, mj):
                    item.previously_recommended_on = prev  # type: ignore[assignment]
                    item.extra["previously_recommended_on"] = prev
                    item.extra["revisit_badge"] = f"经典重温 · {prev}"
                    revisit_matches += 1
                in_media.append(item)

            in_texts: list[TextFeedItem] = []
            in_articles: dict[str, TextArticle] = {}
            for tj in req.get("text_items") or []:
                if not isinstance(tj, Mapping):
                    continue
                fi, art = parse_incoming_text_item(tj, "interactive", preserve_category=True)
                fi.channel_id = "interactive"
                fi.curation_mode = "interactive_ephemeral"
                fi.extra["curation_mode"] = "interactive_ephemeral"
                art.channel_id = "interactive"
                art.curation_mode = "interactive_ephemeral"
                if prev := match_item_provenance(prov, tj):
                    fi.previously_recommended_on = prev  # type: ignore[assignment]
                    fi.tags = [*fi.tags, f"经典重温 · {prev}"]
                    art.previously_recommended_on = prev  # type: ignore[assignment]
                    revisit_matches += 1
                in_texts.append(fi)
                in_articles[art.id] = art

            in_books = [parse_incoming_book_item(b) for b in (req.get("book_items") or []) if isinstance(b, Mapping)]
            in_spreads = {
                (sp := BookSpread.model_validate({**sp_raw, "book_id": sp_raw.get("book_id") or bid})).book_id: sp
                for bid, sp_raw in (req.get("book_spreads") or {}).items()
                if isinstance(sp_raw, Mapping)
            }
        except ValidationError as exc:
            raise CuratorError("invalid_argument", f"Invalid interactive item schema: {exc}") from exc

        sess_path = safe_child_path(paths.curation_root, "interactive", "sessions", f"{session_id}.json")
        merged = load_interactive_session(sess_path, session_id) if sess_path.exists() else InteractiveSession(session_id=session_id)
        replace = bool(req.get("replace_session_items", False))
        skipped_dups = 0
        added_media: list[MediaItem] = []
        added_texts: list[TextFeedItem] = []
        added_books: list[BookItem] = []

        if replace:
            merged.media_items = in_media
            merged.text_items = in_texts
            merged.articles = in_articles
            merged.book_items = in_books
            merged.book_spreads = in_spreads
            added_media, added_texts, added_books = in_media, in_texts, in_books
        else:
            seen_m_ids = {m.id for m in merged.media_items}
            seen_m_keys = {media_identity_key(m) for m in merged.media_items}
            for item in in_media:
                mkey = media_identity_key(item)
                if item.id in seen_m_ids or (repeat_policy == "session_dedup_only" and mkey in seen_m_keys):
                    if repeat_policy == "session_dedup_only":
                        skipped_dups += 1
                    continue
                seen_m_ids.add(item.id)
                seen_m_keys.add(mkey)
                merged.media_items.append(item)
                added_media.append(item)

            seen_t_ids = {t.id for t in merged.text_items}
            seen_t_keys = {text_identity_key(t, merged.articles.get(t.id)) for t in merged.text_items}
            for fi in in_texts:
                art = in_articles.get(fi.id)
                tkey = text_identity_key(fi, art)
                if fi.id in seen_t_ids or (repeat_policy == "session_dedup_only" and tkey in seen_t_keys):
                    if repeat_policy == "session_dedup_only":
                        skipped_dups += 1
                    continue
                seen_t_ids.add(fi.id)
                seen_t_keys.add(tkey)
                merged.text_items.append(fi)
                added_texts.append(fi)
                if art:
                    merged.articles[art.id] = art

            seen_b_ids = {b.id for b in merged.book_items}
            seen_b_keys = {book_identity_key(b) for b in merged.book_items}
            for b in in_books:
                bkey = book_identity_key(b)
                if b.id in seen_b_ids or (repeat_policy == "session_dedup_only" and bkey in seen_b_keys):
                    if repeat_policy == "session_dedup_only":
                        skipped_dups += 1
                    continue
                seen_b_ids.add(b.id)
                seen_b_keys.add(bkey)
                merged.book_items.append(b)
                added_books.append(b)
            merged.book_spreads.update(in_spreads)

        if "title" in req:
            merged.title = str(req.get("title") or "").strip() or "AI 灵感推荐"
        if "user_prompt" in req:
            merged.user_prompt = str(req.get("user_prompt") or "")
        if "repeat_policy" in req:
            merged.repeat_policy = repeat_policy

        save_interactive_session(paths.curation_root, merged)
        self.interactive_sessions = [s for s in self.interactive_sessions if s.session_id != session_id]
        self.interactive_sessions.append(merged)

        domains = [
            d
            for d, cnt in (("media", len(added_media)), ("text", len(added_texts)), ("book", len(added_books)))
            if cnt > 0
        ] or ["core"]
        await self.ctx.emit(
            "entity.recommend.updated",
            {
                "mode": "interactive_ephemeral",
                "set_id": session_id,
                "domains": domains,
                "written_count": len(added_media) + len(added_texts) + len(added_books),
                "auto_focus": bool(req.get("auto_focus", False)),
            },
        )
        return {
            "mode": "interactive_ephemeral",
            "session_id": session_id,
            "session_path": str(sess_path),
            "record_permanent_history": False,
            "media_count": len(added_media),
            "text_count": len(added_texts),
            "book_count": len(added_books),
            "skipped_session_duplicates": skipped_dups,
            "revisit_matches": revisit_matches,
            "media_items": [m.model_dump() for m in added_media],
            "text_items": [t.model_dump() for t in added_texts],
            "book_items": [b.model_dump() for b in added_books],
        }

    async def action_pin_item(self, req: Mapping[str, Any]) -> dict[str, Any]:
        item_id = str(req.get("item_id") or req.get("id") or "").strip()
        if not item_id:
            raise CuratorError("invalid_argument", "pin_item requires non-empty item_id")
        paths = self.ensure_loaded(req)
        self.pinned_ids.add(item_id)
        self.dismissed_ids.discard(item_id)
        save_id_list(paths.curation_root / "interactive" / "pinned.json", self.pinned_ids)
        if (paths.curation_root / "interactive" / "dismissed.json").exists():
            save_id_list(paths.curation_root / "interactive" / "dismissed.json", self.dismissed_ids)
        await self.ctx.emit(
            "entity.recommend.updated",
            {"action": "pin_item", "item_id": item_id, "mode": "interactive_ephemeral", "domains": ["core"], "written_count": 1},
        )
        return {"pinned": True, "item_id": item_id, "pinned_count": len(self.pinned_ids)}

    async def action_dismiss_item(self, req: Mapping[str, Any]) -> dict[str, Any]:
        item_id = str(req.get("item_id") or req.get("id") or "").strip()
        if not item_id:
            raise CuratorError("invalid_argument", "dismiss_item requires non-empty item_id")
        paths = self.ensure_loaded(req)
        self.dismissed_ids.add(item_id)
        self.pinned_ids.discard(item_id)
        save_id_list(paths.curation_root / "interactive" / "dismissed.json", self.dismissed_ids)
        if (paths.curation_root / "interactive" / "pinned.json").exists():
            save_id_list(paths.curation_root / "interactive" / "pinned.json", self.pinned_ids)
        await self.ctx.emit(
            "entity.recommend.updated",
            {"action": "dismiss_item", "item_id": item_id, "mode": "interactive_ephemeral", "domains": ["core"], "written_count": 0},
        )
        return {"dismissed": True, "item_id": item_id, "dismissed_count": len(self.dismissed_ids)}

    async def action_clear_interactive(self, req: Mapping[str, Any]) -> dict[str, Any]:
        session_id = str(req.get("session_id") or "").strip()
        paths = self.ensure_loaded(req)
        ix_dir = paths.curation_root / "interactive" / "sessions"
        removed = 0
        if session_id:
            if not safe_identifier(session_id):
                raise CuratorError("invalid_session_id", "Unsafe session_id")
            target = safe_child_path(paths.curation_root, "interactive", "sessions", f"{session_id}.json")
            if target.exists():
                target.unlink()
                removed = 1
            self.interactive_sessions = [s for s in self.interactive_sessions if s.session_id != session_id]
        else:
            if ix_dir.is_dir():
                for fp in ix_dir.glob("*.json"):
                    fp.unlink()
                    removed += 1
            self.interactive_sessions.clear()
        await self.ctx.emit(
            "entity.recommend.updated",
            {"action": "clear_interactive", "mode": "interactive_ephemeral", "domains": ["core"], "written_count": 0},
        )
        return {"cleared": True, "removed_sessions": removed}

    async def action_status(self, req: Mapping[str, Any]) -> dict[str, Any]:
        paths = self.ensure_loaded(req)
        return self.status_snapshot(paths)

    async def action_list_sets(self, req: Mapping[str, Any]) -> dict[str, Any]:
        self.ensure_loaded(req)
        return {
            "sets": [
                {
                    "session_id": s.session_id,
                    "title": s.title,
                    "repeat_policy": s.repeat_policy,
                    "media_count": len(s.media_items),
                    "text_count": len(s.text_items),
                    "book_count": len(s.book_items),
                }
                for s in self.interactive_sessions
            ]
        }

    async def action_get_set(self, req: Mapping[str, Any]) -> dict[str, Any]:
        session_id = str(req.get("session_id") or "").strip()
        if not session_id:
            raise CuratorError("invalid_argument", "get_set requires session_id")
        self.ensure_loaded(req)
        for s in self.interactive_sessions:
            if s.session_id == session_id:
                return {
                    "session_id": s.session_id,
                    "title": s.title,
                    "repeat_policy": s.repeat_policy,
                    "media_items": [m.model_dump() for m in s.media_items],
                    "text_items": [t.model_dump() for t in s.text_items],
                    "book_items": [b.model_dump() for b in s.book_items],
                }
        raise CuratorError("not_found", f"Interactive set not found: {session_id}")

    async def action_get_date_bundle(self, req: Mapping[str, Any]) -> dict[str, Any]:
        date = str(req.get("business_date") or req.get("date") or "").strip()
        if not is_valid_iso_date(date):
            raise CuratorError("invalid_argument", "Valid YYYY-MM-DD business_date is required")
        self.ensure_loaded(req)
        media = [
            m.model_dump()
            for m in (*self.daily_media, *self.archive.media_items)
            if m.id not in self.dismissed_ids and str(m.business_date or m.extra.get("business_date") or "") == date
        ]
        texts = [
            t.model_dump()
            for t in (*self.daily_texts, *self.archive.text_items)
            if t.id not in self.dismissed_ids and t.published_at == date
        ]
        return {"business_date": date, "media_items": media, "text_items": texts}

    def status_snapshot(self, paths: RepoPaths | None = None) -> dict[str, Any]:
        resolved = paths or self._last_paths or resolve_repo_paths(self.ctx)
        dirty = self._obsidian_sync.dirty_worktree_preserved or self._music_sync.dirty_worktree_preserved
        return {
            "ok": True,
            "obsidian_vault_dir": str(resolved.obsidian_vault_dir),
            "music_daily_dir": str(resolved.music_daily_dir),
            "curation_dir": str(resolved.curation_root),
            "obsidian_exists": resolved.obsidian_vault_dir.is_dir(),
            "music_daily_exists": resolved.music_daily_dir.is_dir(),
            "dirty": dirty,
            "dirty_worktree_preserved": dirty,
            "git_sync": {"obsidian_vault": self._obsidian_sync.to_dict(), "music_daily": self._music_sync.to_dict()},
            "dates_indexed": self.archive.dates_indexed,
            "music_tracks_hydrated": self.archive.music_tracks_hydrated,
            "youtube_videos_hydrated": self.archive.youtube_videos_hydrated,
            "news_days_hydrated": self.archive.news_days_hydrated,
            "news_stories_hydrated": self.archive.news_stories_hydrated,
            "reddit_days_hydrated": self.archive.reddit_days_hydrated,
            "reddit_posts_hydrated": self.archive.reddit_posts_hydrated,
            "journal_days_hydrated": self.archive.journal_days_hydrated,
            "journal_papers_hydrated": self.archive.journal_papers_hydrated,
            "english_notes_hydrated": self.archive.english_notes_hydrated,
            "vocab_cards_hydrated": self.archive.vocab_cards_hydrated,
            "history_anomalies": list(self.archive.history_anomalies),
            "daily_media_count": len(self.daily_media),
            "daily_text_count": len(self.daily_texts),
            "interactive_sessions_count": len(self.interactive_sessions),
            "pinned_count": len(self.pinned_ids),
            "dismissed_count": len(self.dismissed_ids),
            "cache_hit": self._last_cache_hit,
            "last_rehydrate_date": self._last_rehydrate_date,
        }
