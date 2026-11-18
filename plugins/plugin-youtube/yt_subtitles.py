"""VTT/SRT subtitle parsing, track discovery, priority selection, and download."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping
from typing import Any

from omnidesk_sdk import LyricLine

if __package__:
    from .yt_http import http_get_bytes
    from .yt_security import redact_sensitive, validate_https_url, validate_lang
else:
    from yt_http import http_get_bytes
    from yt_security import redact_sensitive, validate_https_url, validate_lang

_TS_RE = re.compile(
    r"^\s*(?:(?P<h>\d{1,3}):)?(?P<m>\d{1,2}):(?P<s>\d{2})[.,](?P<ms>\d{3})\s*-->\s*"
    r"(?:(?P<eh>\d{1,3}):)?(?P<em>\d{1,2}):(?P<es>\d{2})[.,](?P<ems>\d{3})(?:\s+.*)?$"
)
_VTT_INLINE_TS_RE = re.compile(r"<\d{1,3}:\d{2}(?::\d{2})?[.,]\d{3}>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ASS_TAG_RE = re.compile(r"\{\\[^}]*\}")
_EXT_PRIORITY = {"vtt": 0, "srt": 1}


def _parse_ts_ms(hours: str | None, mins: str, secs: str, ms: str) -> int:
    h, m, s, millis = int(hours) if hours is not None else 0, int(mins), int(secs), int(ms)
    if m >= 60 or s >= 60:
        raise ValueError(f"Invalid subtitle timestamp: {m}:{s}.{millis}")
    return ((h * 3600) + (m * 60) + s) * 1000 + millis


def _clean_cue_text(lines: list[str]) -> str:
    joined = "\n".join(lines)
    cleaned = _ASS_TAG_RE.sub("", _HTML_TAG_RE.sub("", _VTT_INLINE_TS_RE.sub("", joined)))
    cleaned = html.unescape(cleaned)
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip()).strip()


def _make_cue(index: int, start_ms: int, end_ms: int, body: str) -> dict[str, Any]:
    return {
        "index": index,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "start_sec": start_ms / 1000.0,
        "end_sec": end_ms / 1000.0,
        "original": body,
        "translation": "",
        "text": body,
    }


def _dedupe_rolling_cues(raw_cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_cues.sort(key=lambda c: (c["start_ms"], c["end_ms"]))
    out: list[dict[str, Any]] = []
    for cue in raw_cues:
        if out and out[-1]["original"] == cue["original"] and cue["start_ms"] <= out[-1]["end_ms"] + 250:
            if cue["end_ms"] > out[-1]["end_ms"]:
                out[-1]["end_ms"] = cue["end_ms"]
                out[-1]["end_sec"] = cue["end_ms"] / 1000.0
            continue
        out.append(cue)
    for idx, cue in enumerate(out, 1):
        cue["index"] = idx
    return out


def parse_srt(text: str) -> list[dict[str, Any]]:
    """Parse SubRip (.srt) text into normalized cue dicts."""
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("Empty SRT subtitle payload")
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        m = _TS_RE.match(lines[0])
        if not m:
            raise ValueError(f"Invalid SRT cue header: {lines[0]!r}")
        start_ms = _parse_ts_ms(m.group("h"), m.group("m"), m.group("s"), m.group("ms"))
        end_ms = _parse_ts_ms(m.group("eh"), m.group("em"), m.group("es"), m.group("ems"))
        body = _clean_cue_text(lines[1:])
        if end_ms > start_ms and body:
            cues.append(_make_cue(len(cues) + 1, start_ms, end_ms, body))
    if not cues:
        raise ValueError("No valid cues found in SRT subtitle")
    return _dedupe_rolling_cues(cues)


def parse_vtt(text: str) -> list[dict[str, Any]]:
    """Parse WebVTT (.vtt) text into normalized cue dicts."""
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("Empty WebVTT subtitle payload")
    cues: list[dict[str, Any]] = []
    for idx, block in enumerate(re.split(r"\n\s*\n", normalized)):
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if idx == 0 and lines[0].strip().startswith("WEBVTT"):
            ts_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
            if ts_idx is None:
                continue
            lines = lines[ts_idx:]
        if lines[0].strip().startswith(("NOTE", "STYLE", "REGION")):
            continue
        ts_idx = 0 if "-->" in lines[0] else (1 if len(lines) > 1 and "-->" in lines[1] else -1)
        if ts_idx < 0:
            continue
        m = _TS_RE.match(lines[ts_idx])
        if not m:
            continue
        start_ms = _parse_ts_ms(m.group("h"), m.group("m"), m.group("s"), m.group("ms"))
        end_ms = _parse_ts_ms(m.group("eh"), m.group("em"), m.group("es"), m.group("ems"))
        body = _clean_cue_text(lines[ts_idx + 1 :])
        if end_ms > start_ms and body:
            cues.append(_make_cue(len(cues) + 1, start_ms, end_ms, body))
    if not cues:
        raise ValueError("No valid cues found in WebVTT subtitle")
    return _dedupe_rolling_cues(cues)


def parse_subtitles(text: str, fmt: str = "auto") -> tuple[str, list[dict[str, Any]]]:
    """Parse subtitle text as VTT or SRT and return `(detected_format, cues)`."""
    cleaned = text.lstrip("\ufeff").strip()
    fmt_low = (fmt or "auto").strip().lower()
    if fmt_low == "vtt" or (fmt_low == "auto" and cleaned.startswith("WEBVTT")):
        return "vtt", parse_vtt(text)
    if fmt_low == "srt" or fmt_low == "auto":
        return "srt", parse_srt(text)
    raise ValueError(f"Unsupported subtitle format: {fmt!r}")


def extract_subtitle_tracks(
    info: Mapping[str, Any],
    *,
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Extract and prioritize official subtitles over automatic captions with HTTPS validation."""
    tracks: list[dict[str, Any]] = []
    for bucket_name, is_auto in (("subtitles", False), ("automatic_captions", True)):
        raw_map = info.get(bucket_name)
        if not isinstance(raw_map, Mapping):
            continue
        source_type = "automatic" if is_auto else "official"
        for raw_lang, variants in raw_map.items():
            if not isinstance(raw_lang, str) or raw_lang.strip().lower() == "live_chat":
                continue
            try:
                lang = validate_lang(raw_lang)
            except ValueError:
                continue
            if not isinstance(variants, list):
                continue
            for item in variants:
                if not isinstance(item, Mapping):
                    continue
                ext = str(item.get("ext") or "").strip().lower()
                raw_url = str(item.get("url") or "").strip()
                if ext not in {"vtt", "srt"} or not raw_url:
                    continue
                try:
                    safe_url = validate_https_url(
                        raw_url, allow_loopback_http=allow_loopback_http, allowed_loopback_origins=allowed_loopback_origins
                    )
                except ValueError:
                    continue
                tracks.append(
                    {"lang": lang, "source_type": source_type, "is_auto": is_auto, "ext": ext, "name": str(item.get("name") or lang), "url": safe_url}
                )
    tracks.sort(key=lambda t: (1 if t["is_auto"] else 0, _EXT_PRIORITY.get(t["ext"], 9), t["lang"]))
    return tracks


def _lang_match_rank(track_lang: str, requested: str, preferred_langs: tuple[str, ...]) -> tuple[int, int]:
    t_low = track_lang.lower()
    if requested:
        r_low = requested.lower()
        if t_low == r_low:
            return (0, 0)
        if t_low.split("-", 1)[0] == r_low.split("-", 1)[0]:
            return (0, 1)
        return (9, 9)
    for idx, pref in enumerate(preferred_langs):
        p_low = pref.lower()
        if t_low == p_low:
            return (0, idx)
        if t_low.split("-", 1)[0] == p_low.split("-", 1)[0]:
            return (1, idx)
    return (2, 0)


def select_best_track(
    tracks: list[dict[str, Any]],
    *,
    lang: str = "",
    preferred_langs: tuple[str, ...] = ("en", "en-US", "zh-Hans", "zh-CN", "zh"),
) -> dict[str, Any] | None:
    """Select highest-priority subtitle track (official > automatic, VTT > SRT)."""
    if not tracks:
        return None
    req_clean = lang.strip()
    pool = [t for t in tracks if _lang_match_rank(t["lang"], req_clean, preferred_langs)[0] < 9] if req_clean else tracks
    if not pool:
        return None
    return min(
        pool,
        key=lambda t: (1 if t["is_auto"] else 0, _lang_match_rank(t["lang"], req_clean, preferred_langs), _EXT_PRIORITY.get(t["ext"], 9)),
    )


async def download_and_parse_track(
    track: Mapping[str, Any],
    *,
    timeout_sec: float = 15.0,
    extra_secrets: Iterable[str] = (),
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
    proxy: str = "",
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Download a subtitle track URL via HTTPS and parse it as VTT or SRT."""
    raw_bytes, err = await http_get_bytes(
        str(track["url"]),
        timeout_sec=timeout_sec,
        extra_secrets=extra_secrets,
        allow_loopback_http=allow_loopback_http,
        allowed_loopback_origins=allowed_loopback_origins,
        proxy=proxy,
    )
    if err is not None or raw_bytes is None:
        return None, err
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    try:
        _, cues = parse_subtitles(text, fmt=str(track.get("ext") or "auto"))
    except ValueError as exc:
        return None, {"error_code": "service_offline", "error": redact_sensitive(str(exc), extra_secrets)}
    return cues, None


def cues_to_lyric_lines(cues: Iterable[Mapping[str, Any]]) -> list[LyricLine]:
    """Convert subtitle cues to SDK LyricLine models."""
    lines: list[LyricLine] = []
    for cue in cues:
        orig = str(cue.get("original") or cue.get("text") or "").strip()
        if orig:
            lines.append(
                LyricLine(
                    time_sec=max(0.0, float(cue.get("start_sec") or 0.0)),
                    original=orig,
                    translation=str(cue.get("translation") or "").strip(),
                )
            )
    return lines


def format_srt_timestamp(ms: int) -> str:
    """Format milliseconds as SRT timestamp ``HH:MM:SS,mmm``."""
    safe = max(0, int(ms))
    h, rem = divmod(safe, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, millis = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{millis:03d}"


def cues_to_srt(cues: Iterable[Mapping[str, Any]], *, mode: str = "bilingual") -> str:
    """Serialize normalized subtitle cues or lyric lines to UTF-8 SRT text."""
    cue_list = list(cues)
    blocks: list[str] = []
    idx = 0
    for i, cue in enumerate(cue_list):
        start_ms = cue.get("start_ms")
        if start_ms is None:
            start_ms = int(round(float(cue.get("start_sec") if cue.get("start_sec") is not None else cue.get("time_sec", 0.0)) * 1000))
        end_ms = cue.get("end_ms")
        if end_ms is None:
            raw_end_sec = cue.get("end_sec")
            if raw_end_sec is not None:
                end_ms = int(round(float(raw_end_sec) * 1000))
            elif i + 1 < len(cue_list):
                nxt = cue_list[i + 1]
                nxt_ms = nxt.get("start_ms")
                if nxt_ms is None:
                    nxt_ms = int(round(float(nxt.get("start_sec") if nxt.get("start_sec") is not None else nxt.get("time_sec", 0.0)) * 1000))
                end_ms = max(int(start_ms) + 500, min(int(start_ms) + 6000, int(nxt_ms) - 50))
            else:
                end_ms = int(start_ms) + 3000
        if int(end_ms) <= int(start_ms):
            end_ms = int(start_ms) + 2000

        orig = str(cue.get("original") or cue.get("text") or "").strip()
        trans = str(cue.get("translation") or "").strip()
        if mode == "translated":
            body = trans or orig
        elif mode == "original":
            body = orig or trans
        else:
            body = f"{orig}\n{trans}" if (orig and trans and trans != orig) else (orig or trans)
        if not body:
            continue
        idx += 1
        blocks.append(f"{idx}\n{format_srt_timestamp(int(start_ms))} --> {format_srt_timestamp(int(end_ms))}\n{body}")
    return ("\n\n".join(blocks) + "\n") if blocks else ""


_CJK_LINE_RE = re.compile(r"[\u4e00-\u9fff]")


def parse_bilingual_srt(text: str) -> list[dict[str, Any]]:
    """Parse an SRT file into cues, splitting bilingual lines into original + translation."""
    cues = parse_srt(text)
    for cue in cues:
        lines = [ln.strip() for ln in str(cue.get("original") or "").splitlines() if ln.strip()]
        if len(lines) >= 2:
            cjk = [ln for ln in lines if _CJK_LINE_RE.search(ln)]
            non_cjk = [ln for ln in lines if not _CJK_LINE_RE.search(ln)]
            if cjk and non_cjk:
                orig = " ".join(non_cjk)
                trans = " ".join(cjk)
                cue["original"] = orig
                cue["text"] = orig
                cue["translation"] = trans
    return cues
