"""Subtitle parsing (SRT/VTT/ASS/LRC) and English clip-jump search for plugin-fnos-nas."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_sdk import LyricLine

if __package__:
    from .safety import MAX_SUBTITLE_BYTES, SUBTITLE_EXTENSIONS, MountValidationError, is_safe_scan_entry, read_text_bounded
else:
    from safety import MAX_SUBTITLE_BYTES, SUBTITLE_EXTENSIONS, MountValidationError, is_safe_scan_entry, read_text_bounded

_TAG_RE = re.compile(r"<[^>]*>|\{\\[^}]*\}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")
_EPISODE_RE = re.compile(r"(?:^|[^a-z0-9])S(\d{1,2})[ ._-]*E(\d{1,3})(?:$|[^a-z0-9])", re.IGNORECASE)
_EPISODE_ALT_RE = re.compile(r"(?:^|[^a-z0-9])(\d{1,2})x(\d{1,3})(?:$|[^a-z0-9])", re.IGNORECASE)
_LANG_SUFFIX_RE = re.compile(
    r"[._-](?:zh[-_]?en|en[-_]?zh|zh[-_]?cn|zh[-_]?tw|zh[-_]?hk|chs&eng|eng&chs|chs|cht|zh|eng|en|bilingual|dual|forced|sdh|cc)$",
    re.IGNORECASE,
)
_TS_RE = re.compile(r"^(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})$")
_LRC_LINE_RE = re.compile(r"^\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\](.*)$")

_IRREGULAR_FORMS: dict[str, tuple[str, ...]] = {
    "be": ("am", "is", "are", "was", "were", "been", "being"), "begin": ("began", "begun", "beginning"),
    "break": ("broke", "broken", "breaking", "breaks"), "bring": ("brought", "bringing"),
    "build": ("built", "building"), "catch": ("caught", "catching"), "choose": ("chose", "chosen", "choosing"),
    "come": ("came", "coming"), "do": ("did", "done", "doing", "does"), "draw": ("drew", "drawn", "drawing"),
    "drive": ("drove", "driven", "driving"), "fall": ("fell", "fallen", "falling"),
    "find": ("found", "finding"), "give": ("gave", "given", "giving"), "go": ("went", "gone", "going", "goes"),
    "grow": ("grew", "grown", "growing"), "have": ("had", "has", "having"), "hold": ("held", "holding"),
    "keep": ("kept", "keeping"), "know": ("knew", "known", "knowing"), "lead": ("led", "leading"),
    "leave": ("left", "leaving"), "lose": ("lost", "losing"), "make": ("made", "making"),
    "mean": ("meant", "meaning"), "meet": ("met", "meeting"), "run": ("ran", "running"),
    "see": ("saw", "seen", "seeing"), "send": ("sent", "sending"), "show": ("showed", "shown", "showing"),
    "speak": ("spoke", "spoken", "speaking"), "take": ("took", "taken", "taking"), "teach": ("taught", "teaching"),
    "think": ("thought", "thinking"), "write": ("wrote", "written", "writing"),
    "child": ("children",), "person": ("people",),
}


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    """Single parsed subtitle cue with optional bilingual line split."""

    start_sec: float
    end_sec: float
    text: str
    en_text: str
    zh_text: str


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _parse_ts(raw: str) -> float | None:
    m = _TS_RE.match(raw.strip())
    if not m:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2))
    seconds = int(m.group(3))
    frac = m.group(4)
    if minutes > 59 or seconds > 59:
        return None
    ms = int(frac.ljust(3, "0")[:3])
    return round(hours * 3600 + minutes * 60 + seconds + ms / 1000.0, 3)


def _clean_text(raw: str) -> str:
    text = raw.replace("\\N", "\n").replace("\\n", "\n")
    text = _TAG_RE.sub("", text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _split_bilingual(text: str) -> tuple[str, str]:
    en_lines: list[str] = []
    zh_lines: list[str] = []
    for ln in text.splitlines():
        if _CJK_RE.search(ln):
            zh_lines.append(ln)
            latin = re.sub(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+", " ", ln).strip()
            if re.search(r"[A-Za-z]{2,}", latin):
                en_lines.append(re.sub(r"\s+", " ", latin))
        else:
            en_lines.append(ln)
    return " ".join(en_lines).strip(), " ".join(zh_lines).strip()


def parse_srt_or_vtt(source: str) -> list[SubtitleCue]:
    """Parse SRT or WebVTT text into sorted SubtitleCue entries."""
    normalized = source.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", normalized)
    cues: list[SubtitleCue] = []
    for block in blocks:
        stripped = block.strip()
        if not stripped or re.match(r"^(?:WEBVTT|NOTE|STYLE|REGION)(?:\s|$)", stripped):
            continue
        lines = stripped.splitlines()
        timing_idx = next((idx for idx, ln in enumerate(lines) if "-->" in ln), -1)
        if timing_idx < 0:
            continue
        parts = lines[timing_idx].split("-->", 1)
        start = _parse_ts(parts[0].strip().split()[0]) if parts[0].strip() else None
        end_token = parts[1].strip().split()[0] if len(parts) > 1 and parts[1].strip() else ""
        end = _parse_ts(end_token)
        body = _clean_text("\n".join(lines[timing_idx + 1 :]))
        if start is None or end is None or end <= start or not body:
            continue
        en_text, zh_text = _split_bilingual(body)
        cues.append(SubtitleCue(start_sec=start, end_sec=end, text=body, en_text=en_text, zh_text=zh_text))
    cues.sort(key=lambda c: (c.start_sec, c.end_sec))
    return cues


def parse_ass(source: str) -> list[SubtitleCue]:
    """Parse ASS/SSA Dialogue events into SubtitleCue entries."""
    normalized = source.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[SubtitleCue] = []
    for line in normalized.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line[len("Dialogue:") :].split(",", 9)
        if len(fields) < 10:
            continue
        start = _parse_ts(fields[1].strip())
        end = _parse_ts(fields[2].strip())
        body = _clean_text(fields[9])
        if start is None or end is None or end <= start or not body:
            continue
        en_text, zh_text = _split_bilingual(body)
        cues.append(SubtitleCue(start_sec=start, end_sec=end, text=body, en_text=en_text, zh_text=zh_text))
    cues.sort(key=lambda c: (c.start_sec, c.end_sec))
    return cues


def parse_subtitle_text(source: str, suffix: str) -> list[SubtitleCue]:
    """Dispatch subtitle parsing by file extension."""
    ext = suffix.lower()
    if ext in {".ass", ".ssa"}:
        return parse_ass(source)
    return parse_srt_or_vtt(source)


def parse_lrc(source: str) -> list[LyricLine]:
    """Parse LRC lyrics (merging identical timestamps as original/translation) into LyricLine list."""
    grouped: dict[float, list[str]] = {}
    for raw_line in source.lstrip("\ufeff").splitlines():
        m = _LRC_LINE_RE.match(raw_line.strip())
        if not m:
            continue
        minutes, seconds = int(m.group(1)), int(m.group(2))
        frac = m.group(3) or "0"
        ms = int(frac.ljust(3, "0")[:3]) if len(frac) <= 3 else int(frac[:3])
        time_sec = round(minutes * 60 + seconds + ms / 1000.0, 3)
        text = m.group(4).strip()
        if text:
            grouped.setdefault(time_sec, []).append(text)
    out: list[LyricLine] = []
    for ts in sorted(grouped):
        lines = grouped[ts]
        if len(lines) >= 2:
            out.append(LyricLine(time_sec=ts, original=lines[0], translation=lines[1]))
        else:
            en, zh = _split_bilingual(lines[0])
            if en and zh:
                out.append(LyricLine(time_sec=ts, original=en, translation=zh))
            else:
                out.append(LyricLine(time_sec=ts, original=lines[0]))
    return out


def strip_subtitle_lang_suffix(stem: str) -> str:
    """Strip common language/bilingual tags from a subtitle stem to match its video stem."""
    cur = stem
    for _ in range(3):
        stripped = _LANG_SUFFIX_RE.sub("", cur)
        if stripped == cur:
            break
        cur = stripped
    return cur


def extract_episode_info(name: str) -> tuple[int | None, int | None, str]:
    """Extract (season, episode, 'SxxEyy') from a filename or path string."""
    m = _EPISODE_RE.search(name) or _EPISODE_ALT_RE.search(name)
    if not m:
        return None, None, ""
    season, episode = int(m.group(1)), int(m.group(2))
    return season, episode, f"S{season:02d}E{episode:02d}"


def _tokenize_words(text: str) -> list[tuple[str, int, int]]:
    normalized = text.lower().replace("’", "'").replace("‘", "'")
    return [(m.group(0), m.start(), m.end()) for m in _LATIN_WORD_RE.finditer(normalized)]


def _query_variants(word: str) -> list[str]:
    variants = [word]
    if len(word) < 3:
        return variants
    forms: list[str] = list(_IRREGULAR_FORMS.get(word, ()))
    forms.extend([f"{word}s", f"{word}es", f"{word}'s"])
    if word.endswith("y") and len(word) > 3 and word[-2] not in "aeiou":
        forms.extend([f"{word[:-1]}ies", f"{word[:-1]}ied"])
    if word.endswith("e"):
        forms.extend([f"{word}d", f"{word[:-1]}ing"])
    else:
        forms.extend([f"{word}ed", f"{word}ing"])
    for form in forms:
        if form not in variants:
            variants.append(form)
    return variants


def match_cue_query(cue: SubtitleCue, query: str) -> tuple[bool, bool]:
    """Return (matched, is_variant) for an English word/phrase query on a cue."""
    q_tokens = [tok for tok, _, _ in _tokenize_words(query)]
    if not q_tokens:
        clean_q = query.strip().lower()
        return (bool(clean_q and clean_q in cue.text.lower()), False)

    haystack_text = cue.en_text or cue.text
    hay_tokens = _tokenize_words(haystack_text)
    if not hay_tokens:
        return False, False

    words = [tok for tok, _, _ in hay_tokens]
    n = len(q_tokens)
    for idx in range(len(words) - n + 1):
        if words[idx : idx + n] == q_tokens:
            if n > 1:
                span = haystack_text[hay_tokens[idx][1] : hay_tokens[idx + n - 1][2]]
                if re.search(r"[.!?;。！？]", span):
                    continue
            return True, False

    if n == 1:
        allowed = set(_query_variants(q_tokens[0]))
        if any(w in allowed for w in words):
            return True, True
    return False, False


def _find_companion_video(
    sub_path: Path,
    video_by_dir_stem: dict[tuple[Path, str], dict[str, Any]],
) -> dict[str, Any] | None:
    base_stem = strip_subtitle_lang_suffix(sub_path.stem).lower()
    for candidate_dir in (sub_path.parent, sub_path.parent.parent):
        hit = video_by_dir_stem.get((candidate_dir, base_stem))
        if hit is not None:
            return hit
        hit_exact = video_by_dir_stem.get((candidate_dir, sub_path.stem.lower()))
        if hit_exact is not None:
            return hit_exact
    return None


def search_local_subtitles(
    mount_root: Path,
    query: str,
    *,
    video_items: Sequence[dict[str, Any]],
    series_filter: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search local SRT/VTT/ASS files under mount_root for matching dialogue clips."""
    clean_query = query.strip()
    if not clean_query:
        return []
    resolved_root = mount_root.resolve()
    series_q = series_filter.strip().lower()
    max_hits = max(1, min(int(limit), 100))

    video_by_dir_stem: dict[tuple[Path, str], dict[str, Any]] = {}
    for entry in video_items:
        vpath = Path(entry["path"])
        video_by_dir_stem[(vpath.parent, vpath.stem.lower())] = entry

    results: list[dict[str, Any]] = []
    sub_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(mount_root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        cur_dir = Path(dirpath)
        if not is_safe_scan_entry(resolved_root, cur_dir):
            dirnames.clear()
            continue
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            cand = cur_dir / fname
            if cand.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            if cand.is_symlink() or not is_safe_scan_entry(resolved_root, cand):
                continue
            sub_files.append(cand)
            if len(sub_files) >= 5000:
                dirnames.clear()
                break

    for sub_path in sub_files:
        rel = sub_path.relative_to(mount_root).as_posix()
        video_info = _find_companion_video(sub_path, video_by_dir_stem)
        title = video_info["title"] if video_info else strip_subtitle_lang_suffix(sub_path.stem).replace(".", " ").strip()
        if series_q and series_q not in title.lower() and series_q not in rel.lower():
            continue
        try:
            text = read_text_bounded(sub_path, MAX_SUBTITLE_BYTES, root_guard=mount_root, allow_symlinks=False)
        except MountValidationError:
            continue
        cues = parse_subtitle_text(text, sub_path.suffix)
        if not cues:
            continue

        season, episode, ep_code = extract_episode_info(sub_path.name)
        if not ep_code and video_info:
            season = video_info.get("season")
            episode = video_info.get("episode")
            ep_code = video_info.get("episode_code", "")

        sub_id = f"fnos-sub:{hashlib.sha1(rel.encode('utf-8')).hexdigest()[:16]}"
        item_id = video_info["id"] if video_info else sub_id
        play_url = video_info["play_url"] if video_info else ""
        video_path = video_info["path"] if video_info else ""

        for cue in cues:
            matched, is_variant = match_cue_query(cue, clean_query)
            if not matched:
                continue
            open_payload = {
                "item_id": item_id,
                "source_id": "plugin-fnos-nas",
                "title": title,
                "kind": "nas",
                "play_url": play_url,
                "start_sec": cue.start_sec,
                "end_sec": cue.end_sec,
                "subtitle_path": str(sub_path),
            }
            results.append(
                {
                    "item_id": item_id,
                    "source_id": "plugin-fnos-nas",
                    "title": title,
                    "episode_code": ep_code,
                    "season": season,
                    "episode": episode,
                    "kind": "nas",
                    "dialogue": cue.text,
                    "text": cue.text,
                    "en_text": cue.en_text,
                    "zh_text": cue.zh_text,
                    "start_sec": cue.start_sec,
                    "end_sec": cue.end_sec,
                    "timestamp_label": format_timestamp(cue.start_sec),
                    "subtitle_path": str(sub_path),
                    "subtitle_relative_path": rel,
                    "video_path": video_path,
                    "play_url": play_url,
                    "has_video": bool(play_url),
                    "variant_match": is_variant,
                    "match_source": "local_subtitle",
                    "open_payload": open_payload,
                }
            )

    results.sort(key=lambda r: (bool(r["variant_match"]), not bool(r["has_video"]), r["title"].lower(), r["start_sec"]))
    return results[:max_hits]


def is_bilingual_subtitle_file(path: Path, mount_root: Path) -> bool:
    """Safely check if a subtitle file has both Latin and CJK text (or bilingual filename marker)."""
    lowered = path.stem.lower()
    if any(tag in lowered for tag in ("zh-en", "en-zh", "bilingual", "dual", "chs&eng", "eng&chs")):
        return True
    try:
        raw = read_text_bounded(path, MAX_SUBTITLE_BYTES, root_guard=mount_root)
    except MountValidationError:
        return False
    return bool(_CJK_RE.search(raw) and re.search(r"[A-Za-z]{3,}", raw))
