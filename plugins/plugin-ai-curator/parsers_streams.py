"""Fail-closed parsers for Music, YouTube, News, Reddit, Journal, and JSONL ledgers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from omnidesk_sdk import MediaItem, TextArticle, TextFeedItem, TextParagraph

if __package__:
    from .models import (
        HISTORY_START_DATE,
        LedgerReadError,
        extract_apple_music_song_id,
        extract_apple_music_storefront,
        extract_doi,
        extract_reddit_post_id,
        extract_youtube_video_id,
        is_valid_iso_date,
        normalize_doi,
        normalize_for_dedup,
        read_json_strict,
        read_text_strict,
        truncate_text,
    )
    from .parsers_common import (
        extract_markdown_link_url,
        extract_urls,
        extract_user_notes_after_auto_end,
        match_bold_field,
        parse_frontmatter,
        parse_journal_heading,
        parse_numbered_heading,
        strip_auto_comments,
        strip_markdown_links,
    )
else:
    from models import (  # type: ignore[no-redef]
        HISTORY_START_DATE,
        LedgerReadError,
        extract_apple_music_song_id,
        extract_apple_music_storefront,
        extract_doi,
        extract_reddit_post_id,
        extract_youtube_video_id,
        is_valid_iso_date,
        normalize_doi,
        normalize_for_dedup,
        read_json_strict,
        read_text_strict,
        truncate_text,
    )
    from parsers_common import (  # type: ignore[no-redef]
        extract_markdown_link_url,
        extract_urls,
        extract_user_notes_after_auto_end,
        match_bold_field,
        parse_frontmatter,
        parse_journal_heading,
        parse_numbered_heading,
        strip_auto_comments,
        strip_markdown_links,
    )

_SOURCE_ID = "plugin-ai-curator"


def extract_date_from_stem(path: Path) -> str:
    """Return YYYY-MM-DD if path.stem is a valid date >= 2026-09-04, else empty."""
    stem = path.stem
    if is_valid_iso_date(stem) and stem >= HISTORY_START_DATE:
        return stem
    return ""


def parse_music_manifest_file(
    json_path: Path,
    repo_root: Path,
    user_notes: str = "",
) -> dict[str, Any]:
    """Parse a daily music manifest JSON and detect incomplete-track anomalies."""
    data = read_json_strict(json_path, repo_root=repo_root)
    if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
        raise LedgerReadError(f"Invalid music manifest structure (expected object with tracks[]): {json_path}")

    doc_date = str(data.get("date") or json_path.stem)
    if not is_valid_iso_date(doc_date):
        raise LedgerReadError(f"Invalid date in music manifest {json_path}: {doc_date!r}")

    items: list[MediaItem] = []
    positions: list[int] = []
    for idx, tr in enumerate(data["tracks"]):
        if not isinstance(tr, dict):
            raise LedgerReadError(f"Invalid track entry at index {idx} in {json_path}")
        pos_raw = tr.get("position", idx + 1)
        pos = int(pos_raw) if isinstance(pos_raw, int) and not isinstance(pos_raw, bool) else idx + 1
        positions.append(pos)

        title = str(tr.get("title") or "").strip() or f"Track {pos}"
        artist = str(tr.get("artist") or "").strip()
        album = str(tr.get("album") or "").strip()
        canonical_id = str(tr.get("canonical_id") or "").strip() or (f"{artist}::{title}" if artist else "")

        am = tr.get("apple_music") if isinstance(tr.get("apple_music"), dict) else {}
        raw_sid = am.get("song_id")
        song_id = str(raw_sid).strip() if raw_sid is not None else ""
        play_url = str(am.get("url") or "").strip()
        storefront = str(am.get("storefront") or "").strip().lower() or extract_apple_music_storefront(play_url) or "cn"
        if not song_id and play_url:
            song_id = extract_apple_music_song_id(play_url)

        analysis = str(tr.get("analysis_summary") or "").strip()
        cue = str(tr.get("listening_cue") or "").strip()
        item_id = f"am-cn-{song_id}" if song_id else f"am-cn-{doc_date}-{pos}"
        creator_line = f"{artist} · {album}" if (artist and album) else (artist or album)

        items.append(
            MediaItem(
                id=item_id,
                source_id=_SOURCE_ID,
                title=title,
                subtitle=creator_line,
                creator=artist or creator_line,
                kind="music",
                aspect="1:1",
                source_badge=f"每日 5 首 · {doc_date}",
                play_url=play_url,
                quality_label="Apple Music CN · ALAC",
                has_bilingual_sub=True,
                summary=analysis,
                listening_cue=cue,
                curation_mode="daily_immutable",
                business_date=doc_date,
                extra={
                    "curation_mode": "daily_immutable",
                    "domain": "music",
                    "business_date": doc_date,
                    "priority_rank": pos,
                    "track_index": idx,
                    "apple_music_song_id": song_id,
                    "storefront": storefront,
                    "canonical_id": canonical_id,
                    "artist": artist,
                    "album": album,
                    "why_recommended": analysis,
                    "listening_or_reading_cue": cue,
                    "user_notes": user_notes,
                    "verification_method": "apple_music_cn_catalog",
                },
            )
        )

    anomaly: dict[str, Any] | None = None
    if len(items) != 5:
        expected_positions = [1, 2, 3, 4, 5]
        missing = [p for p in expected_positions if p not in positions]
        rel = str(json_path.relative_to(repo_root)) if json_path.is_relative_to(repo_root) else str(json_path)
        anomaly = {
            "date": doc_date,
            "domain": "music",
            "kind": "incomplete_daily_tracks",
            "expected_count": 5,
            "actual_count": len(items),
            "positions": positions,
            "missing_positions": missing,
            "known_historical_anomaly": doc_date == "2026-11-06",
            "source": rel,
            "note": (
                "2026-11-06 已知历史异常：原第 3 首撤回后保留 4 首，严格不补造第 5 首"
                if doc_date == "2026-11-06"
                else f"{doc_date} 音乐清单仅包含 {len(items)} 首（预期 5 首），已如实保留"
            ),
        }

    return {"date": doc_date, "items": items, "anomaly": anomaly}


def parse_music_state_file(tracks_path: Path, repo_root: Path) -> dict[str, Any]:
    """Parse `state/tracks.json` strictly and extract canonical/song provenance."""
    data = read_json_strict(tracks_path, repo_root=repo_root)
    if not isinstance(data, dict):
        raise LedgerReadError(f"Invalid state/tracks.json object: {tracks_path}")
    recommended = data.get("recommended")
    if recommended is not None and not isinstance(recommended, dict):
        raise LedgerReadError(f"Invalid 'recommended' map in {tracks_path}")
    canonical_dates: dict[str, str] = {}
    canonical_keys: set[str] = set()
    for cid, meta in (recommended or {}).items():
        if not isinstance(cid, str) or not cid.strip():
            continue
        canonical_keys.add(cid)
        if isinstance(meta, dict):
            d = str(meta.get("date") or meta.get("first_date") or "").strip()
            if is_valid_iso_date(d):
                canonical_dates[cid] = d
    return {"canonical_keys": sorted(canonical_keys), "canonical_dates": canonical_dates}


def parse_music_note_file(md_path: Path, repo_root: Path) -> str:
    """Read a Music/Daily/YYYY-MM-DD.md note and extract trailing ## Notes."""
    raw = read_text_strict(md_path, repo_root=repo_root)
    return extract_user_notes_after_auto_end(raw)


def parse_youtube_daily_file(md_path: Path, repo_root: Path) -> dict[str, Any]:
    """Parse `Resources/YouTube/Daily/YYYY-MM-DD.md` into MediaItem records."""
    date = md_path.stem
    raw = read_text_strict(md_path, repo_root=repo_root)
    _, body = parse_frontmatter(raw)
    user_notes = extract_user_notes_after_auto_end(raw)
    lines = strip_auto_comments(body).splitlines()

    daily_priority: list[str] = []
    banner: list[str] = []
    in_priority = False
    videos: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    sub = ""

    def flush() -> None:
        nonlocal cur, sub
        if cur is None:
            return
        cur["why_watch"] = "\n".join(cur["why_lines"]).strip()
        cur["verification_note"] = "\n".join(cur["verify_lines"]).strip()
        if not cur["video_id"] and cur["url"]:
            cur["video_id"] = extract_youtube_video_id(cur["url"])
        if not cur["verification_method"] and "creator_official_page" in cur["verification_note"]:
            cur["verification_method"] = "creator_official_page"
        videos.append(cur)
        cur = None
        sub = ""

    for raw_line in lines:
        t = raw_line.strip()
        numbered = parse_numbered_heading(t)
        if numbered is not None:
            in_priority = False
            flush()
            rank, heading_title = numbered
            cur = {
                "rank": rank,
                "title": heading_title or f"YouTube Video {rank}",
                "channel": "",
                "topic": "",
                "url": "",
                "video_id": "",
                "why_lines": [],
                "verify_lines": [],
                "verification_method": "",
            }
            continue
        if t.startswith("## "):
            flush()
            in_priority = t == "## 今日优先级"
            continue
        if in_priority:
            if t.startswith(">"):
                q = t[1:].strip()
                if q:
                    banner.append(q)
            elif t:
                daily_priority.append(t)
            continue
        if cur is None:
            if t.startswith(">") and not banner:
                banner.append(t[1:].strip())
            continue
        if t.startswith("### "):
            sh = t[4:].strip().lower()
            if "why" in sh:
                sub = "why"
            elif "核验" in sh or "verify" in sh:
                sub = "verify"
            else:
                sub = ""
            continue
        if (val := match_bold_field(t, ("Channel", "频道"))) is not None:
            cur["channel"] = val
        elif (val := match_bold_field(t, ("Topic", "主题"))) is not None:
            cur["topic"] = val
        elif (val := match_bold_field(t, ("URL", "链接"))) is not None:
            urls = extract_urls(val)
            cur["url"] = urls[0] if urls else val
            cur["video_id"] = extract_youtube_video_id(cur["url"])
        elif (val := match_bold_field(t, ("Why watch",))) is not None:
            if val:
                cur["why_lines"].append(val)
            sub = "why"
        elif sub == "why" and t:
            cur["why_lines"].append(t)
        elif sub == "verify" and t:
            cur["verify_lines"].append(t)
            if "verification_method:" in t:
                method = t.split("verification_method:", 1)[1].strip().rstrip(".,`")
                cur["verification_method"] = method

    flush()
    priority_text = "\n".join(daily_priority).strip()
    banner_text = " ".join(banner).strip()
    items: list[MediaItem] = []
    for v in videos:
        vid = v["video_id"]
        is_wildcard = "wildcard" in v["topic"].lower()
        items.append(
            MediaItem(
                id=f"yt-{date}-{vid or v['rank']}",
                source_id=_SOURCE_ID,
                title=v["title"],
                subtitle=v["topic"],
                creator=v["channel"],
                kind="video",
                aspect="16:9",
                source_badge=f"YouTube 精选 · {date}",
                cover_url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if len(vid) == 11 else "",
                play_url=v["url"],
                quality_label="YouTube · Wildcard" if is_wildcard else "YouTube",
                has_bilingual_sub=False,
                summary=v["why_watch"],
                listening_cue=priority_text,
                curation_mode="daily_immutable",
                business_date=date,
                extra={
                    "curation_mode": "daily_immutable",
                    "domain": "youtube",
                    "business_date": date,
                    "priority_rank": v["rank"],
                    "video_id": vid,
                    "topic": v["topic"],
                    "is_wildcard": is_wildcard,
                    "why_recommended": v["why_watch"],
                    "listening_or_reading_cue": priority_text,
                    "verification_note": v["verification_note"] or banner_text,
                    "verification_method": v["verification_method"],
                    "user_notes": user_notes,
                },
            )
        )
    return {"date": date, "items": items}


def parse_news_daily_file(md_path: Path, repo_root: Path) -> dict[str, Any]:
    """Parse `News/Daily/YYYY-MM-DD.md` into issue and per-story TextFeedItem/TextArticle."""
    date = md_path.stem
    raw = read_text_strict(md_path, repo_root=repo_root)
    _, body = parse_frontmatter(raw)
    user_notes = extract_user_notes_after_auto_end(raw)
    lines = strip_auto_comments(body).splitlines()

    exec_lines: list[str] = []
    in_exec = False
    stories: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    sub = ""

    def flush() -> None:
        nonlocal cur, sub
        if cur is None:
            return
        for k in ("facts", "why", "impact_text", "watch", "sources"):
            cur[k] = "\n".join(cur[f"{k}_lines"]).strip()
        cur["source_urls"] = extract_urls(cur["sources"])
        stories.append(cur)
        cur = None
        sub = ""

    for raw_line in lines:
        t = raw_line.strip()
        numbered = parse_numbered_heading(t)
        if numbered is not None:
            in_exec = False
            flush()
            rank, st_title = numbered
            cur = {
                "rank": rank,
                "title": st_title or f"News Story {rank}",
                "event_date": "",
                "impact": "",
                "facts_lines": [],
                "why_lines": [],
                "impact_text_lines": [],
                "watch_lines": [],
                "sources_lines": [],
            }
            continue
        if t.startswith("## "):
            flush()
            in_exec = t == "## Executive View"
            continue
        if in_exec:
            if t and t != "---":
                exec_lines.append(t)
            continue
        if cur is None or t == "---":
            continue
        if t.startswith("### "):
            sh = t[4:].strip().lower()
            if "事实" in sh or "发生" in sh:
                sub = "facts"
            elif "why it matters" in sh:
                sub = "why"
            elif "long-term" in sh:
                sub = "impact_text"
            elif "watch" in sh:
                sub = "watch"
            elif "source" in sh:
                sub = "sources"
            else:
                sub = ""
            continue
        if not sub and (val := match_bold_field(t, ("事件日期", "Event date"))) is not None:
            cur["event_date"] = val
        elif not sub and (val := match_bold_field(t, ("影响等级", "Impact"))) is not None:
            cur["impact"] = val
        elif sub and t:
            cur[f"{sub}_lines"].append(t)

    flush()
    exec_view = "\n".join(exec_lines).strip()
    feed_items: list[TextFeedItem] = []
    articles: dict[str, TextArticle] = {}
    if not stories and not exec_view:
        return {"date": date, "feed_items": feed_items, "articles": articles, "story_count": 0}

    issue_id = f"news-{date}"
    issue_title = f"Daily News · {date}（{len(stories)} 条深度早报）"
    issue_excerpt = truncate_text(exec_view or (stories[0]["facts"] if stories else ""), 220)
    issue_paras: list[TextParagraph] = []
    if exec_view:
        issue_paras.append(TextParagraph(id="p-1", kind="heading", original="Executive View"))
        issue_paras.append(TextParagraph(id="p-2", kind="paragraph", original=exec_view))
    pidx = len(issue_paras) + 1
    for s in stories:
        issue_paras.append(TextParagraph(id=f"p-{pidx}", kind="heading", original=f"{s['rank']}. {s['title']}"))
        pidx += 1
        if s["event_date"] or s["impact"]:
            issue_paras.append(
                TextParagraph(id=f"p-{pidx}", kind="quote", original=f"事件日期: {s['event_date']} · 影响等级: {s['impact']}")
            )
            pidx += 1
        for label, key, pkind in (
            ("【事实摘要】\n", "facts", "paragraph"),
            ("【Why it matters】\n", "why", "paragraph"),
            ("【Long-term impact】\n", "impact_text", "paragraph"),
            ("【What to watch next】\n", "watch", "paragraph"),
            ("Sources:\n", "sources", "quote"),
        ):
            if s[key]:
                issue_paras.append(TextParagraph(id=f"p-{pidx}", kind=pkind, original=label + s[key]))
                pidx += 1
    if user_notes:
        issue_paras.append(TextParagraph(id=f"p-{pidx}", kind="heading", original="My Notes"))
        issue_paras.append(TextParagraph(id=f"p-{pidx + 1}", kind="quote", original=user_notes))

    feed_items.append(
        TextFeedItem(
            id=issue_id,
            source_id=_SOURCE_ID,
            source_name="每日早报 (News)",
            category="news",
            channel_id="news",
            title=issue_title,
            excerpt=issue_excerpt,
            author="AI Curation",
            published_at=date,
            tags=["news", date],
            curation_mode="daily_immutable",
            business_date=date,
            extra={"curation_mode": "daily_immutable", "business_date": date, "executive_view": exec_view},
        )
    )
    articles[issue_id] = TextArticle(
        id=issue_id,
        source_id=_SOURCE_ID,
        source_name="每日早报 (News)",
        category="news",
        channel_id="news",
        title=issue_title,
        subtitle=f"Long-Horizon Daily News · {date}",
        authors="AI Curation",
        published_at=date,
        summary_zh=exec_view,
        tags=["news", date],
        paragraphs=issue_paras,
        curation_mode="daily_immutable",
        business_date=date,
        extra={"executive_view": exec_view, "user_notes": user_notes},
    )

    for s in stories:
        sid = f"news-{date}-{s['rank']}"
        tags = ["news", date] + ([f"Impact {s['impact']}"] if s["impact"] else [])
        primary_url = s["source_urls"][0] if s["source_urls"] else ""
        paras: list[TextParagraph] = []
        idx = 1
        for heading, key, pkind in (
            ("事实摘要", "facts", "paragraph"),
            ("Why it matters", "why", "paragraph"),
            ("Long-term impact", "impact_text", "paragraph"),
            ("What to watch next", "watch", "paragraph"),
            ("Sources", "sources", "quote"),
        ):
            if s[key]:
                paras.append(TextParagraph(id=f"p-{idx}", kind="heading", original=heading))
                paras.append(TextParagraph(id=f"p-{idx + 1}", kind=pkind, original=s[key]))
                idx += 2
        feed_items.append(
            TextFeedItem(
                id=sid,
                source_id=_SOURCE_ID,
                source_name="每日早报 (News)",
                category="news",
                channel_id="news",
                title=s["title"],
                excerpt=truncate_text(s["facts"], 220),
                author=f"影响等级 {s['impact']}" if s["impact"] else "News Daily",
                published_at=date,
                url=primary_url,
                tags=tags,
                curation_mode="daily_immutable",
                business_date=date,
                extra={
                    "curation_mode": "daily_immutable",
                    "business_date": date,
                    "event_date": s["event_date"],
                    "impact": s["impact"],
                    "source_urls": s["source_urls"],
                },
            )
        )
        articles[sid] = TextArticle(
            id=sid,
            source_id=_SOURCE_ID,
            source_name="每日早报 (News)",
            category="news",
            channel_id="news",
            title=s["title"],
            subtitle=date
            + (f" · 事件日期 {s['event_date']}" if s["event_date"] else "")
            + (f" · 影响等级 {s['impact']}" if s["impact"] else ""),
            authors="AI Curation",
            published_at=date,
            url=primary_url,
            impact_score=s["impact"],
            summary_zh=s["facts"],
            tags=tags,
            paragraphs=paras,
            curation_mode="daily_immutable",
            business_date=date,
            extra={"executive_view": exec_view, "source_urls": s["source_urls"], "user_notes": user_notes},
        )
    return {"date": date, "feed_items": feed_items, "articles": articles, "story_count": len(stories)}


def parse_reddit_daily_file(md_path: Path, repo_root: Path) -> dict[str, Any]:
    """Parse `Resources/Reddit/Daily/YYYY-MM-DD.md` preserving honest verification status."""
    date = md_path.stem
    raw = read_text_strict(md_path, repo_root=repo_root)
    _, body = parse_frontmatter(raw)
    user_notes = extract_user_notes_after_auto_end(raw)
    lines = strip_auto_comments(body).splitlines()

    banner_lines: list[str] = []
    posts: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    in_why = False

    def flush() -> None:
        nonlocal cur, in_why
        if cur is None:
            return
        cur["why_read"] = "\n".join(cur["why_lines"]).strip()
        if not cur["post_id"] and cur["url"]:
            cur["post_id"] = extract_reddit_post_id(cur["url"])
        posts.append(cur)
        cur = None
        in_why = False

    for raw_line in lines:
        t = raw_line.strip()
        numbered = parse_numbered_heading(t)
        if numbered is not None:
            flush()
            rank, ptitle = numbered
            cur = {
                "rank": rank,
                "title": ptitle or f"Reddit Post {rank}",
                "subreddit": "",
                "topic": "",
                "url": "",
                "post_id": "",
                "age": "",
                "verification": "",
                "why_lines": [],
                "discussion_note": "",
            }
            continue
        if t.startswith("## "):
            flush()
            continue
        if cur is None:
            if t.startswith(">"):
                banner_lines.append(t[1:].strip())
            continue
        if t.startswith("### "):
            in_why = "why" in t[4:].strip().lower()
            continue
        if (val := match_bold_field(t, ("Subreddit", "subreddit"))) is not None:
            cur["subreddit"] = val
        elif (val := match_bold_field(t, ("Topic", "主题"))) is not None:
            cur["topic"] = val
        elif (val := match_bold_field(t, ("URL", "链接"))) is not None:
            urls = extract_urls(val)
            cur["url"] = urls[0] if urls else val
            cur["post_id"] = extract_reddit_post_id(cur["url"])
        elif (val := match_bold_field(t, ("Age", "日期/约龄"))) is not None:
            cur["age"] = val
        elif (val := match_bold_field(t, ("original_post_verification",))) is not None:
            cur["verification"] = val
        elif (val := match_bold_field(t, ("Why read",))) is not None:
            if val:
                cur["why_lines"].append(val)
            in_why = True
        elif (val := match_bold_field(t, ("Discussion note",))) is not None:
            cur["discussion_note"] = val
            in_why = False
        elif in_why and t:
            cur["why_lines"].append(t)

    flush()
    banner = " ".join(b for b in banner_lines if b).strip()
    feed_items: list[TextFeedItem] = []
    articles: dict[str, TextArticle] = {}
    for p in posts:
        pid = f"reddit-{date}-{p['post_id'] or p['rank']}"
        meta_bits = [x for x in (p["topic"], p["age"]) if x]
        tags = ["reddit", date] + ([p["subreddit"]] if p["subreddit"] else [])
        if "wildcard" in p["topic"].lower():
            tags.append("wildcard")
        verification_status = p["verification"] or ("not_performed_user_waived" if "未打开原始帖子" in banner else "")
        extra = {
            "curation_mode": "daily_immutable",
            "business_date": date,
            "post_id": p["post_id"],
            "subreddit": p["subreddit"],
            "topic": p["topic"],
            "original_post_verification": verification_status,
            "verification_banner": banner,
            "user_notes": user_notes,
        }
        meta_lines = [f"Subreddit: {p['subreddit']}"] if p["subreddit"] else []
        if p["topic"]:
            meta_lines.append(f"Topic: {p['topic']}")
        if p["age"]:
            meta_lines.append(f"Age: {p['age']}")
        if verification_status:
            meta_lines.append(f"original_post_verification: {verification_status}")
        if p["url"]:
            meta_lines.append(f"URL: {p['url']}")

        paras = [TextParagraph(id="p-1", kind="quote", original="\n".join(meta_lines))]
        idx = 2
        if p["why_read"]:
            paras.append(TextParagraph(id=f"p-{idx}", kind="heading", original="Why read"))
            paras.append(TextParagraph(id=f"p-{idx + 1}", kind="paragraph", original=p["why_read"]))
            idx += 2
        if p["discussion_note"]:
            paras.append(TextParagraph(id=f"p-{idx}", kind="heading", original="Discussion note"))
            paras.append(TextParagraph(id=f"p-{idx + 1}", kind="paragraph", original=p["discussion_note"]))

        feed_items.append(
            TextFeedItem(
                id=pid,
                source_id=_SOURCE_ID,
                source_name=p["subreddit"] or "Reddit 每日精选",
                category="reddit",
                channel_id="reddit",
                title=p["title"],
                excerpt=truncate_text(" · ".join(meta_bits), 160),
                author=p["subreddit"],
                published_at=date,
                url=p["url"],
                tags=tags,
                curation_mode="daily_immutable",
                business_date=date,
                extra=extra,
            )
        )
        articles[pid] = TextArticle(
            id=pid,
            source_id=_SOURCE_ID,
            source_name=p["subreddit"] or "Reddit 每日精选",
            category="reddit",
            channel_id="reddit",
            title=p["title"],
            subtitle=" · ".join(x for x in (p["subreddit"], p["topic"], date) if x),
            authors=p["subreddit"],
            published_at=date,
            url=p["url"],
            summary_zh=p["why_read"],
            tags=tags,
            paragraphs=paras,
            curation_mode="daily_immutable",
            business_date=date,
            extra=extra,
        )
    return {"date": date, "feed_items": feed_items, "articles": articles}


def parse_journal_daily_file(md_path: Path, repo_root: Path) -> dict[str, Any]:
    """Parse `00-inbox/Journal/YYYY-MM-DD.md` into structured Journal TextFeedItem/TextArticle."""
    date = md_path.stem
    raw = read_text_strict(md_path, repo_root=repo_root)
    _, body = parse_frontmatter(raw)
    user_notes = extract_user_notes_after_auto_end(raw)
    lines = strip_auto_comments(body).splitlines()

    sci_lines: list[str] = []
    order_lines: list[str] = []
    in_sci = False
    in_order = False
    papers: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        if not cur["doi"]:
            cur["doi"] = extract_doi(cur["bib_line"] + " " + cur["chinese_sub"])
        if not cur["doi_url"] and cur["doi"]:
            cur["doi_url"] = f"https://doi.org/{cur['doi']}"
        papers.append(cur)
        cur = None

    for raw_line in lines:
        t = raw_line.strip()
        jhead = parse_journal_heading(t)
        if jhead is not None:
            in_sci = False
            in_order = False
            flush()
            rank, ptitle = jhead
            cur = {
                "rank": rank,
                "title": ptitle or f"Journal Paper {rank}",
                "chinese_sub": "",
                "bib_line": "",
                "bib_line_md": "",
                "doi": "",
                "doi_url": "",
                "pubmed_url": "",
                "pmc_url": "",
                "sections": [],
            }
            continue
        if t.startswith("## "):
            flush()
            in_sci = t == "## 今日科学问题"
            in_order = not in_sci and t not in {"## Notes", "## My Notes"}
            if in_order:
                order_lines.append(t[3:].strip())
            continue
        if in_sci:
            if t:
                sci_lines.append(raw_line.rstrip())
            continue
        if in_order:
            if t and not t.startswith("[[") and not t.startswith("[Journal 索引]"):
                order_lines.append(raw_line.rstrip())
            continue
        if cur is None or not t:
            continue
        if t.startswith("- **") and "**" in t[4:]:
            close_idx = t.find("**", 4)
            label = t[4:close_idx].strip().rstrip(":：")
            val = t[close_idx + 2 :].strip().lstrip(":：").strip()
            cur["sections"].append([label, val])
            continue
        if not cur["doi"]:
            cur["doi"] = extract_doi(t)
        if not cur["doi_url"]:
            cur["doi_url"] = extract_markdown_link_url(t, "doi")
        if not cur["pubmed_url"]:
            cur["pubmed_url"] = extract_markdown_link_url(t, "pubmed") or extract_markdown_link_url(t, "核验")
        if not cur["pmc_url"]:
            cur["pmc_url"] = extract_markdown_link_url(t, "pmc")
        for u in extract_urls(t):
            if not cur["doi_url"] and "doi.org/" in u:
                cur["doi_url"] = u
            elif not cur["pubmed_url"] and "pubmed.ncbi.nlm.nih.gov" in u:
                cur["pubmed_url"] = u
            elif not cur["pmc_url"] and "pmc.ncbi.nlm.nih.gov" in u:
                cur["pmc_url"] = u
        if t.startswith("**中文释义："):
            cur["chinese_sub"] = t.removeprefix("**中文释义：").removesuffix("**").strip()
        elif not cur["bib_line"]:
            cur["bib_line_md"] = t
            cur["bib_line"] = strip_markdown_links(t)
        elif cur["sections"]:
            cur["sections"][-1][1] += "\n" + raw_line.rstrip()

    flush()
    sci_question = "\n".join(sci_lines).strip()
    reading_order = "\n".join(order_lines).strip()
    feed_items: list[TextFeedItem] = []
    articles: dict[str, TextArticle] = {}

    for p in papers:
        jid = f"journal-{date}-{p['rank']}"
        primary_url = p["pmc_url"] or p["doi_url"] or p["pubmed_url"]
        bib = p["bib_line"]
        authors = ""
        journal_name = "科学期刊精读"
        if "et al." in bib:
            authors = bib[: bib.find("et al.") + 6].strip()
        m_j = re.search(r"\*([^*]+)\*", bib)
        if m_j:
            journal_name = m_j.group(1).strip()
        tags = ["journal", date] + ([f"DOI:{p['doi']}"] if p["doi"] else [])
        paras: list[TextParagraph] = []
        idx = 1
        if sci_question:
            paras.append(TextParagraph(id=f"p-{idx}", kind="heading", original="今日科学问题"))
            paras.append(TextParagraph(id=f"p-{idx + 1}", kind="quote", original=sci_question))
            idx += 2
        if bib:
            paras.append(TextParagraph(id=f"p-{idx}", kind="quote", original=p["bib_line_md"] or bib))
            idx += 1
        for label, content in p["sections"]:
            paras.append(TextParagraph(id=f"p-{idx}", kind="heading", original=label))
            paras.append(TextParagraph(id=f"p-{idx + 1}", kind="paragraph", original=content))
            idx += 2
        if reading_order:
            paras.append(TextParagraph(id=f"p-{idx}", kind="heading", original="建议阅读顺序与范围边界"))
            paras.append(TextParagraph(id=f"p-{idx + 1}", kind="quote", original=reading_order))
            idx += 2
        if user_notes:
            paras.append(TextParagraph(id=f"p-{idx}", kind="heading", original="My Notes"))
            paras.append(TextParagraph(id=f"p-{idx + 1}", kind="quote", original=user_notes))

        sections_dict = {label: content for label, content in p["sections"]}
        feed_items.append(
            TextFeedItem(
                id=jid,
                source_id=_SOURCE_ID,
                source_name=journal_name,
                category="journal",
                channel_id="journal",
                title=p["title"],
                excerpt=truncate_text(p["chinese_sub"] or bib, 240),
                author=authors or "Journal Daily",
                published_at=date,
                url=primary_url,
                doi=p["doi"],
                tags=tags,
                curation_mode="daily_immutable",
                business_date=date,
                extra={
                    "curation_mode": "daily_immutable",
                    "business_date": date,
                    "doi": p["doi"],
                    "pubmed_url": p["pubmed_url"],
                    "pmc_url": p["pmc_url"],
                    "sections": sections_dict,
                    "user_notes": user_notes,
                },
            )
        )
        articles[jid] = TextArticle(
            id=jid,
            source_id=_SOURCE_ID,
            source_name=journal_name,
            category="journal",
            channel_id="journal",
            title=p["title"],
            subtitle=p["chinese_sub"] or bib,
            authors=authors,
            published_at=date,
            url=primary_url,
            doi=p["doi"],
            journal_name=journal_name,
            summary_zh=p["chinese_sub"] or sci_question,
            tags=tags,
            paragraphs=paras,
            curation_mode="daily_immutable",
            business_date=date,
            extra={
                "scientific_question": sci_question,
                "reading_order": reading_order,
                "sections": sections_dict,
                "pubmed_url": p["pubmed_url"],
                "pmc_url": p["pmc_url"],
                "user_notes": user_notes,
            },
        )
    return {"date": date, "feed_items": feed_items, "articles": articles}


def parse_jsonl_history_file(jsonl_path: Path, repo_root: Path, stream_kind: str) -> list[dict[str, Any]]:
    """Parse `.recommendation/history/<kind>.jsonl` fail-closed on truncated or malformed lines."""
    import json

    raw = read_text_strict(jsonl_path, repo_root=repo_root)
    records: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(raw.splitlines(), start=1):
        t = raw_line.strip()
        if not t:
            continue
        try:
            obj = json.loads(t)
        except json.JSONDecodeError as exc:
            raise LedgerReadError(
                f"Corrupted or truncated JSONL at {jsonl_path}:{line_no}: {exc.msg}",
                error_code="unreadable_ledger",
            ) from exc
        if not isinstance(obj, dict):
            raise LedgerReadError(
                f"JSONL line must be object at {jsonl_path}:{line_no}",
                error_code="unreadable_ledger",
            )
        d = str(obj.get("date") or "").strip()
        rec: dict[str, Any] = {"date": d, "stream": stream_kind}
        if vid := str(obj.get("video_id") or "").strip():
            rec["video_id"] = vid
            rec["url"] = f"https://www.youtube.com/watch?v={vid}"
        if pid := str(obj.get("post_id") or "").strip():
            rec["post_id"] = pid
        if raw_doi := str(obj.get("normalized_doi") or obj.get("doi") or "").strip():
            rec["doi"] = normalize_doi(raw_doi)
        if src_url := str(obj.get("source_url") or obj.get("url") or "").strip():
            rec["url"] = src_url
        if title := str(obj.get("title") or "").strip():
            rec["norm_title"] = normalize_for_dedup(title)
        records.append(rec)
    return records
