"""Danmaku, subtitle, ASR/LLM translation, and SponsorBlock timeline handlers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omnidesk_sdk import CapabilityResult, ChapterSegment

from .parsers import (
    cues_to_lyric_lines,
    decompress_danmaku_bytes,
    normalize_media_url,
    parse_bilibili_target,
    parse_danmaku_xml,
    parse_subtitle_json,
)
from .streams import resolve_target_cid

if TYPE_CHECKING:
    from plugin import BilibiliPlugin


async def fetch_danmaku_action(plugin: BilibiliPlugin, req: Mapping[str, Any]) -> CapabilityResult:
    """Fetch and parse Bilibili XML danmaku into structured entries."""
    client = plugin._build_client()
    cookie, _ = await plugin.resolve_cookie()
    target = parse_bilibili_target(req, allowed_loopback_origins=client.allowed_loopback_origins)
    cid, _ = await resolve_target_cid(client, target, cookie)
    raw = await client.fetch_danmaku_raw(cid, cookie=cookie)
    items = parse_danmaku_xml(decompress_danmaku_bytes(raw))
    return CapabilityResult.ok({"cid": cid, "count": len(items), "danmaku": items})


async def fetch_subtitles_action(plugin: BilibiliPlugin, req: Mapping[str, Any]) -> CapabilityResult:
    """Fetch Bilibili JSON subtitles, falling back to `speech.asr.v1` and `llm.translate.v1`."""
    assert plugin.ctx is not None
    client = plugin._build_client()
    cookie, _ = await plugin.resolve_cookie()
    target = parse_bilibili_target(req, allowed_loopback_origins=client.allowed_loopback_origins)
    cid, _ = await resolve_target_cid(client, target, cookie)
    player_info = await client.fetch_player_v2(
        bvid=target.get("bvid", ""), aid=target.get("aid"), cid=cid, cookie=cookie
    )
    sub_block = player_info.get("subtitle") if isinstance(player_info.get("subtitle"), Mapping) else {}
    tracks = [t for t in (sub_block.get("subtitles") or []) if isinstance(t, Mapping)]

    degraded_caps: list[str] = []
    cues: list[dict[str, Any]] = []
    source, selected_lang = "none", ""
    preferred_lang = str(req.get("lang") or "").strip().lower()

    if tracks:
        chosen = next(
            (t for t in tracks if preferred_lang and str(t.get("lan") or "").lower() == preferred_lang),
            tracks[0],
        )
        selected_lang = str(chosen.get("lan") or "")
        raw_sub_url = str(chosen.get("subtitle_url") or "").strip()
        if raw_sub_url:
            sub_url = normalize_media_url(raw_sub_url, api_base=client.api_base)
            cues = parse_subtitle_json(await client.fetch_subtitle_json(sub_url, cookie=cookie))
            source = "bilibili"

    if not cues and bool(req.get("use_asr", True)):
        media_ref = str(req.get("media_path") or req.get("audio_path") or req.get("play_url") or target.get("url") or "")
        if plugin.ctx.capabilities.has("speech.asr.v1") and media_ref:
            asr_res = await plugin.ctx.capabilities.call(
                "speech.asr.v1",
                {"action": "transcribe_media", "media_path": media_ref, "bvid": target.get("bvid", ""), "cid": cid},
            )
            if asr_res.ok:
                raw_cues = asr_res.data.get("cues") or asr_res.data.get("segments") or []
                cues = [
                    {
                        "index": idx,
                        "start_sec": float(c.get("start_sec") if "start_sec" in c else (c.get("start_ms", 0) / 1000.0)),
                        "end_sec": float(c.get("end_sec") if "end_sec" in c else (c.get("end_ms", 0) / 1000.0)),
                        "original": str(c.get("text") or c.get("original") or "").strip(),
                        "translation": str(c.get("translation") or "").strip(),
                        "note": "asr",
                    }
                    for idx, c in enumerate(raw_cues, start=1)
                    if isinstance(c, Mapping) and str(c.get("text") or c.get("original") or "").strip()
                ]
                if cues:
                    source = "asr"
            else:
                degraded_caps.append("speech.asr.v1")
        else:
            degraded_caps.append("speech.asr.v1")

    if cues and bool(req.get("bilingual", True)) and any(not c.get("translation") for c in cues):
        if plugin.ctx.capabilities.has("llm.translate.v1"):
            tr_res = await plugin.ctx.capabilities.call(
                "llm.translate.v1",
                {
                    "action": "translate_batch",
                    "domain": "media",
                    "target_lang": str(req.get("target_lang") or "zh-CN"),
                    "texts": [c["original"] for c in cues],
                },
            )
            if tr_res.ok:
                translated = tr_res.data.get("translations") or tr_res.data.get("translated") or tr_res.data.get("items")
                if isinstance(translated, list):
                    for cue, tr in zip(cues, translated):
                        if not cue.get("translation"):
                            cue["translation"] = str(tr.get("translation") if isinstance(tr, Mapping) else tr or "").strip()
                elif isinstance(tr_res.data.get("reply"), str) and len(cues) == 1:
                    cues[0]["translation"] = tr_res.data["reply"].strip()
            else:
                degraded_caps.append("llm.translate.v1")
        else:
            degraded_caps.append("llm.translate.v1")

    return CapabilityResult.ok(
        {
            "cid": cid,
            "bvid": target.get("bvid", ""),
            "language": selected_lang,
            "source": source,
            "count": len(cues),
            "subtitles": cues,
            "lyrics": [line.model_dump() for line in cues_to_lyric_lines(cues)],
            "degraded_capabilities": degraded_caps,
            "degraded": bool(degraded_caps),
        }
    )


async def fetch_chapters_impl(plugin: BilibiliPlugin, item_id: str) -> list[ChapterSegment]:
    """Fetch real multi-P/view_points chapters merged with optional `player.timeline.v1`."""
    client = plugin._build_client()
    cookie, _ = await plugin.resolve_cookie()
    target = parse_bilibili_target(item_id, allowed_loopback_origins=client.allowed_loopback_origins)
    bvid, aid = target.get("bvid", ""), target.get("aid")
    if not bvid and not aid:
        return []

    pages = await client.fetch_pagelist(bvid=bvid, aid=aid, cookie=cookie)
    chapters: list[ChapterSegment] = []
    if len(pages) > 1:
        offset = 0.0
        for p in pages:
            dur = max(0.0, float(p.get("duration") or 0.0))
            chapters.append(
                ChapterSegment(
                    id=f"p{p.get('page', len(chapters) + 1)}",
                    start_sec=offset,
                    end_sec=offset + dur if dur > 0 else None,
                    label=f"P{p.get('page', len(chapters) + 1)} {p.get('part') or ''}".strip(),
                    kind="content",
                )
            )
            offset += dur
    elif pages and pages[0].get("cid"):
        info = await client.fetch_player_v2(bvid=bvid, aid=aid, cid=int(pages[0]["cid"]), cookie=cookie)
        for idx, vp in enumerate(info.get("view_points") or [], start=1):
            if not isinstance(vp, Mapping):
                continue
            start = max(0.0, float(vp.get("from", 0.0)))
            end_val = vp.get("to")
            chapters.append(
                ChapterSegment(
                    id=f"vp-{idx}",
                    start_sec=start,
                    end_sec=max(start, float(end_val)) if end_val is not None else None,
                    label=str(vp.get("content") or f"章节 {idx}"),
                    kind="content",
                )
            )

    if plugin.ctx is not None and plugin.ctx.capabilities.has("player.timeline.v1"):
        cid = int(pages[0]["cid"]) if pages and pages[0].get("cid") else target.get("cid")
        tl_res = await plugin.ctx.capabilities.call(
            "player.timeline.v1",
            {"action": "get_segments", "provider": "bilibili", "video_id": bvid or f"av{aid}", "bvid": bvid, "cid": cid},
        )
        if tl_res.ok:
            for idx, seg in enumerate(tl_res.data.get("segments") or tl_res.data.get("chapters") or [], start=1):
                if not isinstance(seg, Mapping):
                    continue
                kind_raw = str(seg.get("kind") or "sponsor").lower()
                kind = kind_raw if kind_raw in {"content", "ad", "sponsor"} else "sponsor"
                start = max(0.0, float(seg.get("start_sec", seg.get("start", 0.0))))
                end_raw = seg.get("end_sec", seg.get("end"))
                chapters.append(
                    ChapterSegment(
                        id=str(seg.get("id") or f"sb-{idx}"),
                        start_sec=start,
                        end_sec=max(start, float(end_raw)) if end_raw is not None else None,
                        label=str(seg.get("label") or seg.get("category") or "SponsorBlock"),
                        kind=kind,  # type: ignore[arg-type]
                    )
                )

    chapters.sort(key=lambda ch: ch.start_sec)
    return chapters
