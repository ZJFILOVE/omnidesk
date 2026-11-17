"""Target, quality, duration, danmaku XML, and subtitle JSON parsers."""

from __future__ import annotations

import gzip
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from omnidesk_sdk import LyricLine

from .security import SecurityValidationError, validate_no_traversal, validate_url

_BVID_RE = re.compile(r"\b(BV[0-9A-Za-z]{10})\b")
_AVID_RE = re.compile(r"(?:^|/|video/)(?:av|AV)(\d+)\b")
_EP_RE = re.compile(r"(?:^|/|play/)(?:ep)(\d+)\b")
_SS_RE = re.compile(r"(?:^|/|play/)(?:ss)(\d+)\b")
_LIVE_RE = re.compile(r"(?:live\.bilibili\.com/|live:)(\d+)\b")
_TAG_RE = re.compile(r"<[^>]+>")

QUALITY_TO_QN: dict[str, int] = {
    "360p": 16,
    "480p": 32,
    "720p": 64,
    "1080p": 80,
    "1080p+": 112,
    "1080p60": 116,
    "4k": 120,
    "hdr": 125,
    "dolby": 126,
    "8k": 127,
}
QN_TO_QUALITY: dict[int, str] = {v: k for k, v in QUALITY_TO_QN.items()}

_MODE_LABELS: dict[int, str] = {
    1: "scroll",
    2: "scroll",
    3: "scroll",
    4: "bottom",
    5: "top",
    6: "reverse",
    7: "special",
    8: "code",
}
_MAX_DECOMPRESSED_BYTES = 16 * 1024 * 1024


def normalize_quality(quality: str | int | None) -> tuple[str, int | None]:
    """Normalize quality label or numeric qn into `(label, qn_or_None)`."""
    if quality is None:
        return "1080p", 80
    if isinstance(quality, int) and not isinstance(quality, bool):
        return QN_TO_QUALITY.get(quality, f"qn_{quality}"), quality
    cleaned = str(quality).strip().lower()
    if not cleaned or cleaned == "auto":
        return "auto", None
    if cleaned.isdigit():
        qn = int(cleaned)
        return QN_TO_QUALITY.get(qn, f"qn_{qn}"), qn
    aliases = {"4k60": "4k", "2160p": "4k", "1080": "1080p", "720": "720p", "480": "480p", "360": "360p"}
    canonical = aliases.get(cleaned, cleaned)
    if canonical not in QUALITY_TO_QN:
        raise SecurityValidationError(f"Unsupported quality value: {quality!r}", error_code="invalid_argument")
    return canonical, QUALITY_TO_QN[canonical]


def _parse_optional_pos_int(raw: Any, name: str) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        cleaned = validate_no_traversal(raw, field_name=name)
        if cleaned.lower().startswith("av") and name == "aid":
            cleaned = cleaned[2:]
        if not cleaned.isdigit():
            raise SecurityValidationError(f"{name} must be a positive integer", error_code="invalid_argument")
        val = int(cleaned)
    elif isinstance(raw, int) and not isinstance(raw, bool):
        val = raw
    else:
        raise SecurityValidationError(f"{name} must be a positive integer", error_code="invalid_argument")
    if val <= 0:
        raise SecurityValidationError(f"{name} must be > 0", error_code="invalid_argument")
    return val


def parse_bilibili_target(
    raw: str | Mapping[str, Any],
    *,
    allowed_loopback_origins: Iterable[str] = (),
) -> dict[str, Any]:
    """Parse `bvid`/`aid`/`cid`/`ep_id`/`season_id`/`room_id`/`url` safely without traversal."""
    if isinstance(raw, Mapping):
        bvid = str(raw.get("bvid") or "").strip()
        aid_raw = raw.get("aid") or raw.get("avid")
        cid_raw = raw.get("cid")
        ep_raw = raw.get("ep_id")
        ss_raw = raw.get("season_id")
        room_raw = raw.get("room_id")
        page_raw = raw.get("page") or raw.get("p") or 1
        url = str(raw.get("url") or raw.get("play_url") or "").strip()
        item_id = str(raw.get("item_id") or raw.get("id") or "").strip()
        if item_id and not (bvid or aid_raw or url or ep_raw or ss_raw or room_raw):
            return parse_bilibili_target(item_id, allowed_loopback_origins=allowed_loopback_origins)
    else:
        bvid, aid_raw, cid_raw, ep_raw, ss_raw, room_raw, page_raw, url = "", None, None, None, None, None, 1, ""
        text = validate_no_traversal(str(raw), field_name="target")
        if text.startswith(("http://", "https://", "//")):
            url = text
        elif text.startswith("live:"):
            room_raw = text.split(":", 1)[1]
        elif (m_bv := _BVID_RE.search(text)):
            bvid = m_bv.group(1)
        elif text.lower().startswith("av") and text[2:].isdigit():
            aid_raw = text[2:]
        elif text.isdigit():
            aid_raw = text
        elif text.lower().startswith("ep") and text[2:].isdigit():
            ep_raw = text[2:]
        elif text.lower().startswith("ss") and text[2:].isdigit():
            ss_raw = text[2:]
        else:
            raise SecurityValidationError(f"Invalid Bilibili media identifier: {text!r}", error_code="invalid_argument")

    validated_url = ""
    if url:
        validated_url = validate_url(url, allow_cdn=False, allowed_loopback_origins=allowed_loopback_origins)
        if not bvid and (m_bv := _BVID_RE.search(validated_url)):
            bvid = m_bv.group(1)
        if not aid_raw and (m_av := _AVID_RE.search(validated_url)):
            aid_raw = m_av.group(1)
        if not ep_raw and (m_ep := _EP_RE.search(validated_url)):
            ep_raw = m_ep.group(1)
        if not ss_raw and (m_ss := _SS_RE.search(validated_url)):
            ss_raw = m_ss.group(1)
        if not room_raw and (m_lv := _LIVE_RE.search(validated_url)):
            room_raw = m_lv.group(1)
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(validated_url).query)
        if "p" in qs and qs["p"] and qs["p"][0].isdigit():
            page_raw = int(qs["p"][0])
        if "bvid" in qs and qs["bvid"] and not bvid:
            bvid = qs["bvid"][0].strip()

    if bvid:
        bvid = validate_no_traversal(bvid, field_name="bvid")
        if not _BVID_RE.fullmatch(bvid):
            raise SecurityValidationError(f"Invalid bvid format: {bvid!r}", error_code="invalid_argument")

    aid = _parse_optional_pos_int(aid_raw, "aid")
    cid = _parse_optional_pos_int(cid_raw, "cid")
    ep_id = _parse_optional_pos_int(ep_raw, "ep_id")
    season_id = _parse_optional_pos_int(ss_raw, "season_id")
    room_id = _parse_optional_pos_int(room_raw, "room_id")
    page_num = _parse_optional_pos_int(page_raw, "page") or 1

    canonical_url = validated_url
    if not canonical_url:
        if bvid:
            canonical_url = f"https://www.bilibili.com/video/{bvid}" + (f"?p={page_num}" if page_num > 1 else "")
        elif aid:
            canonical_url = f"https://www.bilibili.com/video/av{aid}" + (f"?p={page_num}" if page_num > 1 else "")
        elif ep_id:
            canonical_url = f"https://www.bilibili.com/bangumi/play/ep{ep_id}"
        elif season_id:
            canonical_url = f"https://www.bilibili.com/bangumi/play/ss{season_id}"
        elif room_id:
            canonical_url = f"https://live.bilibili.com/{room_id}"

    if not any((bvid, aid, cid, ep_id, season_id, room_id, canonical_url)):
        raise SecurityValidationError("Missing Bilibili target (bvid, aid, cid, or url)", error_code="invalid_argument")

    return {
        "bvid": bvid,
        "aid": aid,
        "cid": cid,
        "ep_id": ep_id,
        "season_id": season_id,
        "room_id": room_id,
        "page": page_num,
        "url": canonical_url,
    }


def parse_duration_sec(value: Any) -> float:
    """Convert numeric seconds or `MM:SS` / `HH:MM:SS` string to float seconds >= 0."""
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip()
    if not text:
        return 0.0
    if text.isdigit():
        return float(int(text))
    parts = text.split(":")
    if all(p.isdigit() for p in parts) and 2 <= len(parts) <= 3:
        total = 0
        for part in parts:
            total = total * 60 + int(part)
        return float(total)
    return 0.0


def clean_bili_title(text: str) -> str:
    """Strip search-highlight HTML tags and unescape HTML entities."""
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def normalize_media_url(raw_url: str, *, api_base: str = "") -> str:
    """Normalize protocol-relative `//` or relative `/` URLs safely."""
    url = (raw_url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/") and api_base:
        return urllib.parse.urljoin(api_base.rstrip("/") + "/", url.lstrip("/"))
    return url


def decompress_danmaku_bytes(raw: bytes) -> str:
    """Safely decode plain UTF-8, gzip, zlib, or raw-deflate Bilibili danmaku bytes."""
    if not raw:
        return ""
    text_probe = raw.lstrip()[:20]
    if text_probe.startswith((b"<", b"\xef\xbb\xbf<")):
        if len(raw) > _MAX_DECOMPRESSED_BYTES:
            raise SecurityValidationError("Danmaku XML exceeds size limit", error_code="invalid_response")
        return raw.decode("utf-8", errors="replace")

    for wbits in (16 + zlib.MAX_WBITS, zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            out = zlib.decompressobj(wbits).decompress(raw, _MAX_DECOMPRESSED_BYTES + 1)
            if len(out) > _MAX_DECOMPRESSED_BYTES:
                raise SecurityValidationError("Decompressed danmaku exceeds size limit", error_code="invalid_response")
            return out.decode("utf-8", errors="replace")
        except zlib.error:
            continue
    try:
        out = gzip.decompress(raw)
        if len(out) > _MAX_DECOMPRESSED_BYTES:
            raise SecurityValidationError("Decompressed danmaku exceeds size limit", error_code="invalid_response")
        return out.decode("utf-8", errors="replace")
    except OSError:
        return raw.decode("utf-8", errors="replace")


def parse_danmaku_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse Bilibili XML danmaku into structured time-sorted entries (XXE-hardened)."""
    stripped = (xml_text or "").strip()
    if not stripped:
        return []
    upper = stripped[:1024].upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise SecurityValidationError("DTD/ENTITY declarations are forbidden in danmaku XML", error_code="path_safety")
    try:
        root = ET.fromstring(stripped)
    except ET.ParseError as exc:
        raise SecurityValidationError(f"Malformed danmaku XML: {exc}", error_code="invalid_response") from exc

    items: list[dict[str, Any]] = []
    for elem in root.findall("d"):
        parts = elem.attrib.get("p", "").split(",")
        if len(parts) < 5:
            continue
        try:
            time_sec = max(0.0, float(parts[0]))
            mode = int(parts[1])
            font_size = int(parts[2])
            color = int(parts[3]) & 0xFFFFFF
            timestamp = int(parts[4])
        except ValueError:
            continue
        pool = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        items.append(
            {
                "time_sec": round(time_sec, 3),
                "mode": mode,
                "mode_label": _MODE_LABELS.get(mode, "other"),
                "font_size": font_size,
                "color": color,
                "color_hex": f"#{color:06X}",
                "timestamp": timestamp,
                "pool": pool,
                "sender_hash": parts[6] if len(parts) > 6 else "",
                "dmid": parts[7] if len(parts) > 7 else "",
                "text": (elem.text or "").strip(),
            }
        )
    items.sort(key=lambda entry: entry["time_sec"])
    return items


def parse_subtitle_json(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Parse Bilibili JSON subtitle (`body: [{from, to, content}]`) into cues."""
    body = payload.get("body")
    if not isinstance(body, list):
        return []
    cues: list[dict[str, Any]] = []
    for idx, entry in enumerate(body, start=1):
        if not isinstance(entry, Mapping):
            continue
        try:
            start_sec = max(0.0, float(entry.get("from", 0.0)))
            end_sec = max(start_sec, float(entry.get("to", start_sec)))
        except (TypeError, ValueError):
            continue
        text = str(entry.get("content") or "").strip()
        if not text:
            continue
        cues.append(
            {
                "index": idx,
                "start_sec": round(start_sec, 3),
                "end_sec": round(end_sec, 3),
                "original": text,
                "translation": str(entry.get("translation") or "").strip(),
                "note": str(entry.get("note") or "").strip(),
            }
        )
    return cues


def cues_to_lyric_lines(cues: Sequence[Mapping[str, Any]]) -> list[LyricLine]:
    """Convert structured subtitle cues into SDK `LyricLine` models."""
    return [
        LyricLine(
            time_sec=max(0.0, float(cue.get("start_sec", 0.0))),
            original=str(cue.get("original") or cue.get("text") or ""),
            translation=str(cue.get("translation") or ""),
            note=str(cue.get("note") or ""),
        )
        for cue in cues
        if str(cue.get("original") or cue.get("text") or "").strip()
    ]
