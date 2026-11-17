"""Daily immutable pipeline handler for `ai.curation.v1`."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from omnidesk_sdk import CapabilityResult, PluginContext

try:
    from .magpie_common import PROTECTED_DAILY_PLAYLIST_ID, fail_result, redact_secret
    from .magpie_curation_helpers import (
        check_curation_skill,
        emit_progress,
        extract_song_id,
        validate_publication_evidence,
    )
except ImportError:
    from magpie_common import PROTECTED_DAILY_PLAYLIST_ID, fail_result, redact_secret  # type: ignore[no-redef]
    from magpie_curation_helpers import (  # type: ignore[no-redef]
        check_curation_skill,
        emit_progress,
        extract_song_id,
        validate_publication_evidence,
    )

_VALID_DAILY_PROJECTS = frozenset({"news-youtube-reddit", "music", "english", "journal"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


async def handle_run_daily_pipeline(
    req: Mapping[str, Any],
    *,
    ctx: PluginContext,
    secret: str | None,
) -> CapabilityResult:
    """Execute `daily_immutable` curation: dry_run transaction verification -> feed publish -> playlist sync."""
    skill_dir, missing = check_curation_skill(ctx)
    if skill_dir is None:
        return fail_result(
            f"缺少 omnidesk-curation-writeback Skill 或参考文档: {', '.join(missing)}",
            error_code="skill_unavailable",
            secret=secret,
            data={"missing_files": missing, "feed_published": False, "partial_success": False},
        )
    if "mode" in req and req["mode"] != "daily_immutable":
        return fail_result(
            "run_daily_pipeline 仅接受 mode='daily_immutable'",
            error_code="invalid_argument",
            secret=secret,
            data={"feed_published": False, "partial_success": False},
        )
    project = str(req.get("project") or "").strip()
    if project not in _VALID_DAILY_PROJECTS:
        return fail_result(
            f"run_daily_pipeline 需要有效 project（{', '.join(sorted(_VALID_DAILY_PROJECTS))}）",
            error_code="invalid_argument",
            secret=secret,
            data={"feed_published": False, "partial_success": False},
        )

    business_date = str(req.get("business_date") or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")).strip()
    if not _ISO_DATE_RE.match(business_date):
        return fail_result("business_date 必须为 YYYY-MM-DD", error_code="invalid_argument", secret=secret)
    operation_id = str(req.get("operation_id") or f"{project}:{business_date}").strip()

    evidence, ev_err = validate_publication_evidence(req, operation_id, project, business_date)
    if ev_err or evidence is None:
        return fail_result(
            ev_err or "缺少已提交的上游发布事务与收据",
            error_code="needs_verified_bundle",
            secret=secret,
            data={"feed_published": False, "partial_success": False, "playlist_synced": False},
        )

    media_items = [dict(x) for x in (req.get("media_items") or []) if isinstance(x, Mapping)]
    track_ids = [str(x).strip() for x in (req.get("track_ids") or []) if str(x).strip()]
    if not track_ids and media_items:
        track_ids = [sid for m in media_items if (sid := extract_song_id(m))]
    needs_playlist = project == "music" or str(req.get("sync_apple_music") or "") == "daily_authorized"
    cum_id = str(req.get("cumulative_playlist_id") or PROTECTED_DAILY_PLAYLIST_ID).strip() or PROTECTED_DAILY_PLAYLIST_ID

    await emit_progress(ctx, operation_id, "daily_immutable", "verifying", "正在执行 PUBLISHED 事务 dry_run 校验")
    if needs_playlist:
        if not ctx.capabilities.has("media.playlist.v1"):
            return fail_result(
                "media.playlist.v1 不可用，无法执行每日歌单 dry_run 校验",
                error_code="capability_not_found",
                secret=secret,
                data={"feed_published": False, "partial_success": False, "playlist_synced": False},
            )
        dry_payload: dict[str, Any] = {
            "action": "sync_daily_authorized",
            "mode": "daily_immutable",
            "business_date": business_date,
            "operation_id": operation_id,
            "qa_verified": True,
            "publication_evidence": evidence,
            "dry_run": True,
            "cumulative_playlist_id": cum_id,
        }
        if track_ids:
            dry_payload["track_ids"] = track_ids
        dry_res = await ctx.capabilities.call("media.playlist.v1", dry_payload)
        if not dry_res.ok:
            return fail_result(
                redact_secret(dry_res.error or "每日歌单 dry_run 事务校验失败", secret),
                error_code=str(dry_res.data.get("error_code") or "needs_verified_bundle"),
                secret=secret,
                data={"feed_published": False, "partial_success": False, "playlist_synced": False},
            )

    if req.get("dry_run") is True:
        return CapabilityResult.ok({
            "mode": "daily_immutable",
            "project": project,
            "business_date": business_date,
            "operation_id": operation_id,
            "dry_run": True,
            "verified": True,
            "feed_published": False,
            "partial_success": False,
        })
    if not ctx.capabilities.has("recommend.feed.v1"):
        return fail_result(
            "recommend.feed.v1 不可用，无法发布每日精选",
            error_code="capability_not_found",
            secret=secret,
            data={"feed_published": False, "partial_success": False, "playlist_synced": False},
        )

    await emit_progress(ctx, operation_id, "daily_immutable", "writing_feed", "正在发布每日精选至前端工作台")
    pub_payload: dict[str, Any] = {
        "action": "publish_daily_bundle",
        "mode": "daily_immutable",
        "operation_id": operation_id,
        "project": project,
        "business_date": business_date,
        "qa_verified": True,
        "publication_evidence": evidence,
        "media_items": media_items,
        "text_items": [dict(x) for x in (req.get("text_items") or []) if isinstance(x, Mapping)],
        "book_items": [dict(x) for x in (req.get("book_items") or []) if isinstance(x, Mapping)],
        "vocab_cards": [dict(x) for x in (req.get("vocab_cards") or []) if isinstance(x, Mapping)],
    }
    if project == "english" and isinstance(req.get("ephemeral_chat_note"), str) and req["ephemeral_chat_note"].strip():
        pub_payload["ephemeral_chat_note"] = req["ephemeral_chat_note"].strip()

    pub_res = await ctx.capabilities.call("recommend.feed.v1", pub_payload)
    if not pub_res.ok:
        return fail_result(
            redact_secret(pub_res.error or "发布每日精选失败", secret),
            error_code=str(pub_res.data.get("error_code") or "publish_failed"),
            secret=secret,
            data={"feed_published": False, "partial_success": False, "playlist_synced": False},
        )

    playlist_synced, playlist_result = False, {}
    if needs_playlist:
        await emit_progress(ctx, operation_id, "daily_immutable", "syncing_playlist", "正在同步每日累计歌单")
        sync_payload: dict[str, Any] = {
            "action": "sync_daily_authorized",
            "mode": "daily_immutable",
            "business_date": business_date,
            "operation_id": operation_id,
            "qa_verified": True,
            "publication_evidence": evidence,
            "dry_run": False,
            "cumulative_playlist_id": cum_id,
            "create_dated_playlist": bool(req.get("create_dated_playlist", True)),
        }
        if track_ids:
            sync_payload["track_ids"] = track_ids
        sync_res = await ctx.capabilities.call("media.playlist.v1", sync_payload)
        if not sync_res.ok:
            pl_err = redact_secret(sync_res.error or "每日歌单同步失败", secret)
            return fail_result(
                pl_err,
                error_code=str(sync_res.data.get("error_code") or "partial_success"),
                secret=secret,
                data={
                    "mode": "daily_immutable",
                    "project": project,
                    "business_date": business_date,
                    "operation_id": operation_id,
                    "feed_published": True,
                    "partial_success": True,
                    "playlist_synced": False,
                    "playlist_error": pl_err,
                    "feed_result": dict(pub_res.data),
                },
            )
        playlist_synced, playlist_result = True, dict(sync_res.data)

    await emit_progress(ctx, operation_id, "daily_immutable", "done", "每日不重复推荐发布完成")
    out_data: dict[str, Any] = {
        "mode": "daily_immutable",
        "project": project,
        "business_date": business_date,
        "operation_id": operation_id,
        "feed_published": True,
        "partial_success": False,
        "playlist_synced": playlist_synced,
        "feed_result": dict(pub_res.data),
        "playlist_result": playlist_result,
    }
    if "ephemeral_chat_note" in pub_payload:
        out_data["ephemeral_chat_note"] = pub_payload["ephemeral_chat_note"]
    return CapabilityResult.ok(out_data)
