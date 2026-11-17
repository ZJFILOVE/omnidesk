"""Skill validation, identity canonicalization, provenance & authenticity helpers for `ai.curation.v1`."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import CapabilityResult, PluginContext

try:
    from .magpie_common import PROTECTED_DAILY_PLAYLIST_ID
except ImportError:
    from magpie_common import PROTECTED_DAILY_PLAYLIST_ID  # type: ignore[no-redef]

_AM_SONG_URL_RE = re.compile(r"music\.apple\.com/[a-z]{2}/song/(?:[^/]+/)?(\d+)")
_YT_VIDEO_RE = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|shorts/))([A-Za-z0-9_-]{11})")
_REQUIRED_SKILL_FILES = (
    "SKILL.md",
    "references/archive-rehydration.md",
    "references/daily-immutable-rules.md",
    "references/interactive-repeatable-rules.md",
    "references/writeback-rpc-and-apple-music.md",
)

ChatCallable = Callable[[dict[str, Any]], Coroutine[Any, Any, CapabilityResult]]


def resolve_skill_dir(ctx: PluginContext | None) -> Path:
    """Locate `omnidesk-curation-writeback` skill directory."""
    configured = "" if ctx is None else str(ctx.get_config("skill_dir", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if ctx is not None:
        candidate = ctx.plugin_dir.resolve().parents[1] / ".agents" / "skills" / "omnidesk-curation-writeback"
        if candidate.is_dir():
            return candidate
    return Path.cwd() / ".agents" / "skills" / "omnidesk-curation-writeback"


def check_curation_skill(ctx: PluginContext | None) -> tuple[Path | None, list[str]]:
    """Verify `SKILL.md` and all required reference files exist and are non-empty."""
    skill_dir = resolve_skill_dir(ctx)
    missing = [rel for rel in _REQUIRED_SKILL_FILES if not (skill_dir / rel).is_file() or (skill_dir / rel).stat().st_size == 0]
    return (skill_dir, []) if not missing else (None, missing)


def is_protected_daily_playlist(playlist_id: str, playlist_name: str) -> bool:
    """Return True if target refers to `p.YJXV7dEIeb3GzZX` or `ChatGPT Daily 5*`."""
    if playlist_id.strip() == PROTECTED_DAILY_PLAYLIST_ID:
        return True
    folded = re.sub(r"[^a-z0-9]", "", playlist_name.lower())
    return "chatgptdaily5" in folded or playlist_name.strip().lower().startswith("chatgpt daily 5")


def extract_song_id(item: Mapping[str, Any]) -> str:
    """Extract numeric Apple Music song ID from item fields or Apple Music URL."""
    extra = item.get("extra") if isinstance(item.get("extra"), Mapping) else {}
    for src in (item, extra):
        for key in ("apple_music_song_id", "song_id", "track_id"):
            val = str(src.get(key) or "").strip()
            if val.isdigit():
                return val
    raw_id = str(item.get("id") or "").strip()
    if raw_id.isdigit():
        return raw_id
    for prefix in ("am-cn-", "ix-am-cn-", "ix-am-"):
        if raw_id.startswith(prefix) and raw_id[len(prefix):].isdigit():
            return raw_id[len(prefix):]
    for url_key in ("play_url", "url"):
        m = _AM_SONG_URL_RE.search(str(item.get(url_key) or ""))
        if m:
            return m.group(1)
    return ""


def _norm_doi(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if s.startswith(pfx):
            return s[len(pfx):]
    return s


def _norm_url(raw: Any) -> str:
    return str(raw or "").strip().split("#", 1)[0].rstrip("/")


def item_identity(item: Mapping[str, Any], domain_hint: str = "") -> str:
    """Compute canonical identity key (`song:` / `video:` / `doi:` / `post:` / `url:` / `title:`)."""
    extra = item.get("extra") if isinstance(item.get("extra"), Mapping) else {}
    if song_id := extract_song_id(item):
        return f"song:{song_id}"
    for src in (item, extra):
        vid = str(src.get("video_id") or src.get("youtube_id") or "").strip()
        if vid:
            return f"video:{vid}"
    for url_key in ("play_url", "url"):
        if m := _YT_VIDEO_RE.search(str(item.get(url_key) or "")):
            return f"video:{m.group(1)}"
    if doi := (_norm_doi(item.get("doi")) or _norm_doi(extra.get("doi"))):
        return f"doi:{doi}"
    for src in (item, extra):
        pid = str(src.get("post_id") or "").strip()
        if pid:
            return f"post:{pid}"
    for url_key in ("url", "play_url"):
        if u := _norm_url(item.get(url_key)):
            return f"url:{u}"
    if domain_hint == "book" and str(item.get("id") or "").strip():
        return f"book:{str(item['id']).strip()}"
    title = re.sub(r"\s+", " ", str(item.get("title") or "").strip().lower())
    creator = re.sub(r"\s+", " ", str(item.get("creator") or item.get("author") or "").strip().lower())
    if title and creator:
        return f"work:{creator}::{title}"
    return f"title:{title}" if title else f"id:{str(item.get('id') or '').strip()}"


def validate_publication_evidence(
    req: Mapping[str, Any],
    operation_id: str,
    project: str,
    business_date: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Verify upstream `PUBLISHED` transaction and non-empty receipt; `qa_verified` alone never suffices."""
    if req.get("qa_verified") is not True:
        return None, "缺少 qa_verified=true 或上游 PUBLISHED 事务证据"
    raw = req.get("publication_evidence")
    if not isinstance(raw, Mapping):
        return None, "缺少 publication_evidence（qa_verified 不能单独替代上游 PUBLISHED 事务与收据）"
    if str(raw.get("status") or "") != "PUBLISHED" or raw.get("publication_verified") is not True:
        return None, "publication_evidence 必须包含 status='PUBLISHED' 与 publication_verified=true"
    ev_op = str(raw.get("operation_id") or "").strip()
    if ev_op != operation_id:
        return None, f"publication_evidence.operation_id ({ev_op!r}) 与 {operation_id!r} 不匹配"
    ev_proj = str(raw.get("project") or "").strip()
    if ev_proj not in {project, "daily"}:
        return None, f"publication_evidence.project ({ev_proj!r}) 与 {project!r} 不匹配"
    ev_date = str(raw.get("business_date") or "").strip()
    if ev_date != business_date:
        return None, f"publication_evidence.business_date ({ev_date!r}) 与 {business_date!r} 不匹配"
    receipt = raw.get("receipt")
    receipt_ok = isinstance(receipt, str) and bool(receipt.strip())
    if not receipt_ok and isinstance(receipt, Mapping):
        receipt_ok = any(
            isinstance(receipt.get(k), str) and str(receipt[k]).strip()
            for k in ("id", "receipt_id", "path", "bundle_sha256", "manifest_blob")
        )
    if not receipt_ok:
        return None, "publication_evidence.receipt 不能为空"
    return dict(raw), None


def collect_archive_buckets(archive_data: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Unpack `search_archive` response into `(media_items, text_items, book_items)`."""
    media = [dict(x) for x in (archive_data.get("media_items") or []) if isinstance(x, Mapping)]
    texts = [dict(x) for x in (archive_data.get("text_items") or []) if isinstance(x, Mapping)]
    books = [dict(x) for x in (archive_data.get("book_items") or []) if isinstance(x, Mapping)]
    seen_ids = {str(x.get("id") or "") for x in (*media, *texts, *books) if x.get("id")}
    for entry in archive_data.get("items") or []:
        if not isinstance(entry, Mapping):
            continue
        inner = dict(entry.get("item")) if isinstance(entry.get("item"), Mapping) else dict(entry)
        eid = str(inner.get("id") or entry.get("id") or "")
        if eid and eid in seen_ids:
            continue
        if eid:
            inner.setdefault("id", eid)
            seen_ids.add(eid)
        dom = str(entry.get("domain") or inner.get("kind") or inner.get("category") or "").lower()
        if dom in {"music", "video", "nas", "media"} or extract_song_id(inner):
            media.append(inner)
        elif dom in {"book", "english-book"} or inner.get("format"):
            books.append(inner)
        else:
            texts.append(inner)
    return media, texts, books


def build_auth_index(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index authentic candidate records by ID and canonical identity."""
    idx: dict[str, dict[str, Any]] = {}
    for item in items:
        for key in (f"id:{str(item.get('id') or '').strip()}", item_identity(item)):
            if key and not key.endswith(":") and key not in idx:
                idx[key] = dict(item)
    return idx


def match_authentic(cand: Mapping[str, Any], auth_idx: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Match an LLM candidate strictly against authentic items without mutating authoritative fields."""
    cid = str(cand.get("id") or "").strip()
    matched = (auth_idx.get(f"id:{cid}") if cid else None) or auth_idx.get(item_identity(cand))
    if matched is None:
        return None
    out = dict(matched)
    extra = dict(out.get("extra")) if isinstance(out.get("extra"), Mapping) else {}
    cand_extra = cand.get("extra") if isinstance(cand.get("extra"), Mapping) else {}
    for k in ("why_recommended", "listening_or_reading_cue"):
        val = str(cand_extra.get(k) or cand.get(k) or "").strip()
        if val:
            extra[k] = val
    extra["curation_mode"] = "interactive_ephemeral"
    out["extra"] = extra
    return out


async def emit_progress(ctx: PluginContext, job_id: str, mode: str, stage: str, message: str) -> None:
    try:
        await ctx.emit("curation.job.progress", {"job_id": job_id, "mode": mode, "stage": stage, "message": message})
    except Exception:
        pass


async def llm_select_interactive(
    skill_dir: Path,
    prompt: str,
    repeat_policy: str,
    pool_media: list[dict[str, Any]],
    pool_texts: list[dict[str, Any]],
    pool_books: list[dict[str, Any]],
    active_entity: Any,
    call_chat: ChatCallable,
) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str]:
    """Use `llm.chat.v1` with Skill rules to rank candidates and compose Chinese guidance."""
    skill_rules = (skill_dir / "references" / "interactive-repeatable-rules.md").read_text(encoding="utf-8")[:2400]
    snapshot = {
        "media_items": pool_media[:15],
        "text_items": pool_texts[:15],
        "book_items": pool_books[:10],
        "active_entity": active_entity,
    }
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "media_items": {"type": "array", "items": {"type": "object"}},
            "text_items": {"type": "array", "items": {"type": "object"}},
            "book_items": {"type": "array", "items": {"type": "object"}},
        },
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是 OmniDesk 策展编排助手，严格遵守以下 Skill 规则且严禁编造任何 ID、URL、DOI 或曲目：\n"
                + skill_rules
                + "\n只能从 candidates 中挑选条目，或给出需经 Apple Music preflight 核验的真实 song_id；"
                "必须为每条附上具体中文 why_recommended 与 listening_or_reading_cue。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"prompt": prompt, "repeat_policy": repeat_policy, "candidates": snapshot}, ensure_ascii=False),
        },
    ]
    res = await call_chat({"messages": messages, "json_schema": schema, "temperature": 0.2})
    if not res.ok:
        return False, [], [], [], ""
    parsed = res.data.get("structured")
    if not isinstance(parsed, Mapping):
        return False, [], [], [], ""
    return (
        True,
        [dict(x) for x in (parsed.get("media_items") or []) if isinstance(x, Mapping)],
        [dict(x) for x in (parsed.get("text_items") or []) if isinstance(x, Mapping)],
        [dict(x) for x in (parsed.get("book_items") or []) if isinstance(x, Mapping)],
        str(parsed.get("title") or "").strip(),
    )
