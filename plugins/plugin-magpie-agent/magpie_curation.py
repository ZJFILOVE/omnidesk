"""Skill-backed orchestration for `ai.curation.v1` (`status`, `curate_interactive`, `run_daily_pipeline`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

try:
    from .magpie_common import fail_result, redact_secret
    from .magpie_curation_daily import handle_run_daily_pipeline
    from .magpie_curation_helpers import (
        ChatCallable,
        build_auth_index,
        check_curation_skill,
        collect_archive_buckets,
        emit_progress,
        extract_song_id,
        is_protected_daily_playlist,
        item_identity,
        llm_select_interactive,
        match_authentic,
    )
except ImportError:
    from magpie_common import fail_result, redact_secret  # type: ignore[no-redef]
    from magpie_curation_daily import handle_run_daily_pipeline  # type: ignore[no-redef]
    from magpie_curation_helpers import (  # type: ignore[no-redef]
        ChatCallable,
        build_auth_index,
        check_curation_skill,
        collect_archive_buckets,
        emit_progress,
        extract_song_id,
        is_protected_daily_playlist,
        item_identity,
        llm_select_interactive,
        match_authentic,
    )

_VALID_REPEAT_POLICIES = frozenset({"allow_any", "prefer_familiar", "session_dedup_only"})


async def handle_curation(
    req: Mapping[str, Any],
    *,
    ctx: PluginContext,
    call_chat: ChatCallable,
    session_seen: dict[str, set[str]],
    secret: str | None,
) -> CapabilityResult:
    """Dispatch `ai.curation.v1` actions (`status`, `curate_interactive`, `run_daily_pipeline`)."""
    if not isinstance(req, Mapping):
        return fail_result("ai.curation.v1 请求必须为 JSON 对象", error_code="invalid_argument", secret=secret)
    action = str(req.get("action") or "status").strip()
    if action == "status":
        skill_dir, missing = check_curation_skill(ctx)
        return CapabilityResult.ok({
            "skill_available": skill_dir is not None,
            "skill_dir": str(skill_dir) if skill_dir else "",
            "missing_skill_files": missing,
            "recommend_feed_available": ctx.capabilities.has("recommend.feed.v1"),
            "media_playlist_available": ctx.capabilities.has("media.playlist.v1"),
        })
    if action == "curate_interactive":
        return await _handle_curate_interactive(req, ctx=ctx, call_chat=call_chat, session_seen=session_seen, secret=secret)
    if action == "run_daily_pipeline":
        return await handle_run_daily_pipeline(req, ctx=ctx, secret=secret)
    return fail_result(f"不支持的 ai.curation.v1 动作: {action!r}", error_code="invalid_argument", secret=secret)


async def _verify_music_candidates(
    ctx: PluginContext,
    cand_media: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    music_cands = [m for m in cand_media if (m.get("kind") == "music" or extract_song_id(m))]
    other_media = [m for m in cand_media if m not in music_cands]
    verified_music: list[dict[str, Any]] = []
    verified_ids: list[str] = []
    if music_cands:
        song_ids = [sid for m in music_cands if (sid := extract_song_id(m))]
        if song_ids and ctx.capabilities.has("media.playlist.v1"):
            pre_res = await ctx.capabilities.call(
                "media.playlist.v1",
                {"action": "preflight_cn_catalog", "storefront": "cn", "track_ids": list(dict.fromkeys(song_ids))},
            )
            if pre_res.ok:
                vset = {str(v).strip() for v in (pre_res.data.get("verified_ids") or []) if str(v).strip()}
                meta_by_id = {
                    str(s.get("id") or s.get("song_id") or "").strip(): s
                    for s in (pre_res.data.get("songs") or pre_res.data.get("tracks") or [])
                    if isinstance(s, Mapping)
                }
                for m in music_cands:
                    sid = extract_song_id(m)
                    if sid and sid in vset:
                        item = dict(m)
                        if meta := meta_by_id.get(sid):
                            if meta.get("title"):
                                item["title"] = str(meta["title"])
                            if meta.get("artist") or meta.get("creator"):
                                item["creator"] = str(meta.get("artist") or meta.get("creator"))
                        extra = dict(item.get("extra")) if isinstance(item.get("extra"), Mapping) else {}
                        extra.update({"curation_mode": "interactive_ephemeral", "apple_music_song_id": sid, "storefront": "cn"})
                        item["extra"] = extra
                        item.setdefault("kind", "music")
                        item.setdefault("play_url", f"https://music.apple.com/cn/song/{sid}")
                        verified_music.append(item)
                        verified_ids.append(sid)
    return verified_music + other_media, verified_ids


async def _attach_provenance(ctx: PluginContext, items: list[dict[str, Any]]) -> None:
    if not items or not ctx.capabilities.has("recommend.feed.v1"):
        return
    prov_res = await ctx.capabilities.call("recommend.feed.v1", {"action": "lookup_provenance", "items": items})
    if prov_res.ok and isinstance(prov_res.data.get("matches"), list):
        by_id = {
            str(m.get("id") or ""): str(m.get("previously_recommended_on") or "")
            for m in prov_res.data["matches"] if isinstance(m, Mapping) and m.get("previously_recommended_on")
        }
        for item in items:
            if prev_date := by_id.get(str(item.get("id") or "")):
                extra = dict(item.get("extra")) if isinstance(item.get("extra"), Mapping) else {}
                extra["previously_recommended_on"] = prev_date
                extra["revisit_badge"] = f"经典重温 · {prev_date}"
                item["extra"] = extra


async def _sync_interactive_playlist(
    ctx: PluginContext,
    req: Mapping[str, Any],
    *,
    session_id: str,
    final_track_ids: list[str],
    final_media: list[dict[str, Any]],
    final_texts: list[dict[str, Any]],
    final_books: list[dict[str, Any]],
    skipped_dups: int,
    playlist_name: str,
    sync_apple_music: str,
    secret: str | None,
) -> tuple[bool, dict[str, Any], CapabilityResult | None]:
    want_queue = sync_apple_music == "queue" or (bool(req.get("enqueue_now", False)) and req.get("sync_playlist") is False)
    want_playlist = sync_apple_music == "ephemeral_playlist" or (
        bool(req.get("sync_playlist", True)) and sync_apple_music != "none" and bool(final_track_ids)
    )
    if not (final_track_ids and (want_queue or want_playlist)):
        return False, {}, None
    if not ctx.capabilities.has("media.playlist.v1"):
        if sync_apple_music in {"queue", "ephemeral_playlist"}:
            err_res = fail_result(
                "media.playlist.v1 不可用，前端已反写但未能写回歌单",
                error_code="partial_success",
                secret=secret,
                data={
                    "mode": "interactive_ephemeral",
                    "session_id": session_id,
                    "feed_written": True,
                    "playlist_synced": False,
                    "partial_success": True,
                    "playlist_error": "media.playlist.v1 不可用",
                    "skipped_session_duplicates": skipped_dups,
                    "verified_ids": final_track_ids,
                },
            )
            return False, {}, err_res
        return False, {}, None

    await emit_progress(ctx, session_id, "interactive_ephemeral", "syncing_playlist", "正在写回灵感歌单/播放队列")
    pl_payload: dict[str, Any] = (
        {
            "action": "enqueue_playback",
            "mode": "interactive_ephemeral",
            "record_permanent_history": False,
            "track_ids": final_track_ids,
            "verified_ids": final_track_ids,
        }
        if want_queue
        else {
            "action": "write_interactive_playlist",
            "mode": "interactive_ephemeral",
            "record_permanent_history": False,
            "playlist_name": playlist_name or "OmniDesk AI 灵感电台",
            "write_strategy": str(req.get("write_strategy") or "append"),
            "enqueue_now": bool(req.get("enqueue_now", False)),
            "track_ids": final_track_ids,
            "verified_ids": final_track_ids,
        }
    )
    pl_res = await ctx.capabilities.call("media.playlist.v1", pl_payload)
    if not pl_res.ok:
        pl_err = redact_secret(pl_res.error or "灵感歌单写回失败", secret)
        return False, {}, fail_result(
            pl_err,
            error_code=str(pl_res.data.get("error_code") or "partial_success"),
            secret=secret,
            data={
                "mode": "interactive_ephemeral",
                "session_id": session_id,
                "feed_written": True,
                "playlist_synced": False,
                "partial_success": True,
                "playlist_error": pl_err,
                "skipped_session_duplicates": skipped_dups,
                "verified_ids": final_track_ids,
                "media_items": final_media,
                "text_items": final_texts,
                "book_items": final_books,
            },
        )
    return True, dict(pl_res.data), None


async def _handle_curate_interactive(
    req: Mapping[str, Any],
    *,
    ctx: PluginContext,
    call_chat: ChatCallable,
    session_seen: dict[str, set[str]],
    secret: str | None,
) -> CapabilityResult:
    skill_dir, missing = check_curation_skill(ctx)
    if skill_dir is None:
        return fail_result(
            f"缺少 omnidesk-curation-writeback Skill 或参考文档: {', '.join(missing)}",
            error_code="skill_unavailable",
            secret=secret,
            data={"missing_files": missing},
        )
    if "mode" in req and req["mode"] not in (None, "", "interactive_ephemeral"):
        return fail_result("curate_interactive 仅支持 mode='interactive_ephemeral'", error_code="invalid_argument", secret=secret)
    repeat_policy = str(req.get("repeat_policy") or "allow_any").strip() or "allow_any"
    if repeat_policy not in _VALID_REPEAT_POLICIES:
        return fail_result(f"不支持的 repeat_policy: {repeat_policy!r}", error_code="invalid_argument", secret=secret)

    sync_apple_music = str(req.get("sync_apple_music") or "").strip()
    playlist_id = str(req.get("playlist_id") or "").strip()
    playlist_name = str(req.get("playlist_name") or "").strip()
    if sync_apple_music == "daily_authorized" or is_protected_daily_playlist(playlist_id, playlist_name):
        return fail_result(
            "interactive_ephemeral 模式严禁修改每日累计歌单 p.YJXV7dEIeb3GzZX (ChatGPT Daily 5)",
            error_code="protected_daily_playlist",
            secret=secret,
        )

    prompt = str(req.get("prompt") or req.get("query") or "").strip()
    req_media = [dict(x) for x in (req.get("media_items") or []) if isinstance(x, Mapping)]
    req_texts = [dict(x) for x in (req.get("text_items") or []) if isinstance(x, Mapping)]
    req_books = [dict(x) for x in (req.get("book_items") or []) if isinstance(x, Mapping)]
    if not prompt and not (req_media or req_texts or req_books):
        return fail_result("curate_interactive 需要非空的 prompt 或候选条目", error_code="invalid_argument", secret=secret)

    session_id = str(req.get("session_id") or "ix-default").strip() or "ix-default"
    await emit_progress(ctx, session_id, "interactive_ephemeral", "searching", "正在检索本地归档与候选条目")

    archive_media: list[dict[str, Any]] = []
    archive_texts: list[dict[str, Any]] = []
    archive_books: list[dict[str, Any]] = []
    if ctx.capabilities.has("recommend.feed.v1"):
        search_res = await ctx.capabilities.call(
            "recommend.feed.v1",
            {
                "action": "search_archive",
                "query": str(req.get("query") or prompt),
                "domains": list(req.get("domains") or []),
                "limit": int(req.get("limit") or 20),
            },
        )
        if search_res.ok:
            archive_media, archive_texts, archive_books = collect_archive_buckets(search_res.data)

    pool_media, pool_texts, pool_books = req_media + archive_media, req_texts + archive_texts, req_books + archive_books
    auth_m, auth_t, auth_b = build_auth_index(pool_media), build_auth_index(pool_texts), build_auth_index(pool_books)
    llm_ok, llm_m, llm_t, llm_b, llm_title = await llm_select_interactive(
        skill_dir, prompt, repeat_policy, pool_media, pool_texts, pool_books, req.get("active_entity"), call_chat
    )
    if llm_ok:
        cand_media = [
            matched if (matched := match_authentic(m, auth_m)) is not None else dict(m)
            for m in llm_m if match_authentic(m, auth_m) is not None or extract_song_id(m)
        ]
        cand_texts = [matched for t in llm_t if (matched := match_authentic(t, auth_t)) is not None]
        cand_books = [matched for b in llm_b if (matched := match_authentic(b, auth_b)) is not None]
    else:
        cand_media, cand_texts, cand_books = list(pool_media), list(pool_texts), list(pool_books)

    await emit_progress(ctx, session_id, "interactive_ephemeral", "verifying", "正在核验国区可播性与归档来源")
    final_media, _ = await _verify_music_candidates(ctx, cand_media)
    final_texts, final_books = cand_texts, cand_books

    allowed_domains = {str(d).lower() for d in (req.get("domains") or []) if isinstance(d, str)}
    if allowed_domains:
        final_media = final_media if "media" in allowed_domains else []
        final_texts = final_texts if "text" in allowed_domains else []
        final_books = final_books if "book" in allowed_domains else []

    await _attach_provenance(ctx, final_media + final_texts + final_books)

    skipped_dups = 0
    if repeat_policy == "session_dedup_only":
        seen = session_seen.setdefault(session_id, set())
        batch_seen: set[str] = set()

        def _dedup(items: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
            nonlocal skipped_dups
            kept: list[dict[str, Any]] = []
            for item in items:
                ident = item_identity(item, hint)
                if ident in seen or ident in batch_seen:
                    skipped_dups += 1
                    continue
                batch_seen.add(ident)
                kept.append(item)
            return kept

        final_media, final_texts, final_books = _dedup(final_media, "media"), _dedup(final_texts, "text"), _dedup(final_books, "book")

    if not (final_media or final_texts or final_books):
        return fail_result(
            "未找到经过核验的真实候选条目（禁止生成占位推荐）",
            error_code="no_verified_candidates",
            secret=secret,
            data={"mode": "interactive_ephemeral", "skipped_session_duplicates": skipped_dups, "feed_written": False, "playlist_synced": False},
        )
    if not ctx.capabilities.has("recommend.feed.v1"):
        return fail_result(
            "recommend.feed.v1 不可用，无法执行前端反写",
            error_code="capability_not_found",
            secret=secret,
            data={"feed_written": False, "playlist_synced": False, "partial_success": False},
        )

    await emit_progress(ctx, session_id, "interactive_ephemeral", "writing_feed", "正在写入前端交互推荐池")
    push_res = await ctx.capabilities.call(
        "recommend.feed.v1",
        {
            "action": "push_interactive_set",
            "mode": "interactive_ephemeral",
            "record_permanent_history": False,
            "session_id": session_id,
            "title": str(req.get("title") or llm_title or f"AI 灵感推荐 · {prompt[:24]}"),
            "user_prompt": prompt,
            "repeat_policy": repeat_policy,
            "replace_session_items": bool(req.get("replace_session_items", False)),
            "auto_focus": bool(req.get("auto_focus", True)),
            "media_items": final_media,
            "text_items": final_texts,
            "book_items": final_books,
            "vocab_cards": list(req.get("vocab_cards") or []),
        },
    )
    if not push_res.ok:
        return fail_result(
            push_res.error or "写入前端交互推荐池失败",
            error_code=str(push_res.data.get("error_code") or "feed_write_failed"),
            secret=secret,
            data={"feed_written": False, "playlist_synced": False, "partial_success": False},
        )

    seen_bucket = session_seen.setdefault(session_id, set())
    for hint, bucket in (("media", final_media), ("text", final_texts), ("book", final_books)):
        for item in bucket:
            seen_bucket.add(item_identity(item, hint))

    final_track_ids = [sid for m in final_media if (sid := extract_song_id(m))]
    playlist_synced, playlist_result, partial_err = await _sync_interactive_playlist(
        ctx,
        req,
        session_id=session_id,
        final_track_ids=final_track_ids,
        final_media=final_media,
        final_texts=final_texts,
        final_books=final_books,
        skipped_dups=skipped_dups,
        playlist_name=playlist_name,
        sync_apple_music=sync_apple_music,
        secret=secret,
    )
    if partial_err is not None:
        return partial_err

    await emit_progress(ctx, session_id, "interactive_ephemeral", "done", "交互推荐反写完成")
    return CapabilityResult.ok({
        "mode": "interactive_ephemeral",
        "session_id": session_id,
        "repeat_policy": repeat_policy,
        "record_permanent_history": False,
        "feed_written": True,
        "playlist_synced": playlist_synced,
        "partial_success": False,
        "skipped_session_duplicates": skipped_dups,
        "verified_ids": final_track_ids,
        "media_items": final_media,
        "text_items": final_texts,
        "book_items": final_books,
        "feed_result": dict(push_res.data),
        "playlist_result": playlist_result,
    })
