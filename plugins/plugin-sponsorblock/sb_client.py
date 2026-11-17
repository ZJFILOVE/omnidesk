"""K-anonymity SponsorBlock API client, ChapterSegment mapper, merger, and F9 next-segment finder."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from omnidesk_sdk import ChapterSegment

_PLATFORMS = frozenset({"bilibili", "youtube"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SKIPPABLE_KINDS = frozenset({"sponsor", "ad"})
_MAX_REDIRECTS = 5
_MERGE_EPSILON_SEC = 0.5
_AUTH_HEADERS = ("authorization", "cookie", "proxy-authorization")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CATEGORY_LABELS: dict[str, str] = {
    "sponsor": "赞助商片段",
    "selfpromo": "自我推广",
    "interaction": "一键三连/互动提醒",
    "intro": "片头/过场",
    "outro": "片尾/致谢",
    "preview": "预告/回顾",
    "music_offtopic": "非音乐片段",
    "filler": "闲聊/注水片段",
    "chapter": "章节",
    "poi_highlight": "高光时刻",
}


class SponsorBlockError(RuntimeError):
    """Classified SponsorBlock client or validation error."""

    def __init__(self, error_code: str, message: str, *, http_status: int | None = None) -> None:
        self.error_code = error_code
        self.http_status = http_status
        super().__init__(message)


def is_loopback_host(host: str) -> bool:
    """Return True if host is loopback."""
    low = host.strip().lower().strip("[]")
    if low in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(low).is_loopback
    except ValueError:
        return False


def validate_platform_and_id(platform: Any, video_id: Any) -> tuple[str, str]:
    """Validate platform in {'bilibili', 'youtube'} and safe non-empty video_id."""
    plat = str(platform or "").strip().lower()
    if plat not in _PLATFORMS:
        raise SponsorBlockError("invalid_argument", f"未知视频平台: {platform!r}（仅支持 bilibili / youtube）")
    vid = str(video_id or "").strip()
    if not vid or not _VIDEO_ID_RE.match(vid) or ".." in vid:
        raise SponsorBlockError("invalid_argument", f"video_id 无效: {video_id!r}")
    return plat, vid


def k_anon_prefix(video_id: str) -> str:
    """Compute 4-hex-char SHA-256 K-anonymity prefix for `video_id`."""
    return hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:4]


def normalize_categories(raw: Any, fallback: str = "sponsor,selfpromo,interaction,intro,outro") -> list[str]:
    """Parse list or comma-separated categories into a deduplicated list."""
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        items = [str(part).strip() for part in raw if str(part).strip()]
    else:
        items = [part.strip() for part in fallback.split(",") if part.strip()]
    deduped = list(dict.fromkeys(items))
    return deduped or ["sponsor"]


def validate_api_base(url: str, *, allow_loopback_http: bool = True) -> urllib.parse.SplitResult:
    """Enforce https:// for public endpoints while allowing loopback http:// for local tests."""
    if not isinstance(url, str) or not url.strip():
        raise SponsorBlockError("invalid_argument", "API Base URL 不能为空")
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError as exc:
        raise SponsorBlockError("invalid_argument", f"API Base URL 格式无效: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise SponsorBlockError("invalid_argument", f"API Base URL 非法: {url!r}")
    host = parts.hostname.strip().lower()
    loopback = is_loopback_host(host)
    if scheme == "http" and not (allow_loopback_http and loopback):
        raise SponsorBlockError("invalid_argument", "公网 SponsorBlock API 仅允许使用 https://")
    if not loopback:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise SponsorBlockError("invalid_argument", f"API 主机名无效: {host}")
        else:
            if not addr.is_global:
                raise SponsorBlockError("path_safety", f"禁止访问私网或保留 IP: {host}")
    return parts


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, initial_loopback: bool) -> None:
        super().__init__()
        self._initial_loopback = initial_loopback
        self._hops = 0

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        self._hops += 1
        if self._hops > _MAX_REDIRECTS:
            raise SponsorBlockError("service_offline", "SponsorBlock 重定向次数过多")
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        parts = validate_api_base(resolved, allow_loopback_http=self._initial_loopback)
        if not self._initial_loopback and is_loopback_host(parts.hostname or ""):
            raise SponsorBlockError("path_safety", "禁止重定向至本地回环地址")
        nxt = super().redirect_request(req, fp, code, msg, headers, resolved)
        if nxt is not None:
            for hdr in list(nxt.headers):
                if hdr.lower() in _AUTH_HEADERS:
                    nxt.remove_header(hdr)
        return nxt


def _fetch_prefix_sync(
    api_base: str,
    prefix: str,
    categories: Sequence[str],
    *,
    timeout_sec: float,
    ssl_context: ssl.SSLContext | None,
) -> list[dict[str, Any]]:
    parts = validate_api_base(api_base, allow_loopback_http=True)
    loopback = is_loopback_host(parts.hostname or "")
    query = urllib.parse.urlencode({"categories": json.dumps(list(categories), separators=(",", ":"))})
    target = f"{api_base.rstrip('/')}/api/skipSegments/{prefix}?{query}"

    handlers: list[Any] = [_SafeRedirectHandler(loopback)]
    if loopback:
        handlers.append(urllib.request.ProxyHandler({}))
    if parts.scheme.lower() == "https":
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context or ssl.create_default_context()))
    opener = urllib.request.build_opener(*handlers)

    req = urllib.request.Request(
        target,
        headers={"User-Agent": "OmniDesk-SponsorBlock/0.2", "Accept": "application/json"},
        method="GET",
    )
    try:
        with opener.open(req, timeout=timeout_sec) as resp:
            raw = resp.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise SponsorBlockError("service_offline", "SponsorBlock 响应体积异常过大")
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, list):
                raise SponsorBlockError("service_offline", "SponsorBlock 返回格式无效")
            return [dict(item) for item in parsed if isinstance(item, Mapping)]
    except SponsorBlockError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        if exc.code == 429:
            raise SponsorBlockError("rate_limited", "SponsorBlock 触发限流 (HTTP 429)", http_status=429) from None
        raise SponsorBlockError("service_offline", f"SponsorBlock 上游返回 HTTP {exc.code}", http_status=int(exc.code)) from None
    except (TimeoutError, socket.timeout):
        raise SponsorBlockError("timeout", "SponsorBlock 查询超时") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise SponsorBlockError("timeout", "SponsorBlock 查询超时") from None
        raise SponsorBlockError("service_offline", f"SponsorBlock 服务不可达: {exc.reason}") from None
    except (OSError, ssl.SSLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SponsorBlockError("service_offline", f"SponsorBlock 查询失败: {exc}") from None


def _map_segment_kind(category: str, action_type: str) -> str:
    cat = category.strip().lower()
    act = action_type.strip().lower()
    if act == "chapter" or cat == "chapter":
        return "content"
    if cat == "sponsor":
        return "sponsor"
    return "ad"


def parse_bucket_segments(
    buckets: Sequence[Mapping[str, Any]],
    *,
    platform: str,
    video_id: str,
) -> list[ChapterSegment]:
    """Filter K-anonymity hash bucket strictly by `video_id` and map to ChapterSegment."""
    segments: list[ChapterSegment] = []
    for entry in buckets:
        entry_vid = entry.get("videoID") if "videoID" in entry else entry.get("videoId")
        if str(entry_vid or "") != video_id:
            continue
        raw_segs = entry.get("segments")
        if not isinstance(raw_segs, list):
            continue
        for raw in raw_segs:
            if not isinstance(raw, Mapping):
                continue
            pair = raw.get("segment")
            if not isinstance(pair, Sequence) or len(pair) < 2:
                continue
            try:
                start_sec = float(pair[0])
                end_sec = float(pair[1])
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(start_sec) and math.isfinite(end_sec)) or start_sec < 0 or end_sec < start_sec:
                continue
            category = str(raw.get("category") or "sponsor").strip().lower()
            action_type = str(raw.get("actionType") or "skip").strip().lower()
            kind = _map_segment_kind(category, action_type)
            uuid = str(raw.get("UUID") or raw.get("uuid") or f"sb-{platform}-{start_sec:.2f}-{end_sec:.2f}")
            desc = str(raw.get("description") or "").strip()
            label = desc or f"{_CATEGORY_LABELS.get(category, category)} (SponsorBlock)"
            segments.append(
                ChapterSegment(
                    id=uuid,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    label=label,
                    digest=f"{platform}:{category}:{action_type}",
                    kind=kind,  # type: ignore[arg-type]
                )
            )
    return merge_and_sort_segments(segments)


async def fetch_k_anon_segments(
    *,
    platform: str,
    video_id: str,
    api_base: str,
    categories: Sequence[str],
    timeout_sec: float,
    ssl_context: ssl.SSLContext | None = None,
) -> tuple[str, list[ChapterSegment]]:
    """Query SponsorBlock via 4-char SHA-256 prefix and return `(prefix, merged_segments)`."""
    prefix = k_anon_prefix(video_id)
    buckets = await asyncio.to_thread(
        _fetch_prefix_sync,
        api_base,
        prefix,
        categories,
        timeout_sec=timeout_sec,
        ssl_context=ssl_context,
    )
    return prefix, parse_bucket_segments(buckets, platform=platform, video_id=video_id)


def coerce_segments(raw_items: Sequence[Any]) -> list[ChapterSegment]:
    """Validate arbitrary chapter/segment dicts via `ChapterSegment`."""
    parsed: list[ChapterSegment] = []
    for item in raw_items:
        if isinstance(item, ChapterSegment):
            parsed.append(item)
        elif isinstance(item, Mapping):
            parsed.append(ChapterSegment.model_validate(item))
        else:
            raise SponsorBlockError("invalid_argument", "章节片段元素必须为 JSON 对象")
    return parsed


def merge_and_sort_segments(*groups: Sequence[ChapterSegment]) -> list[ChapterSegment]:
    """Merge, deduplicate, and deterministically sort ChapterSegment lists."""
    all_segs = [seg for group in groups for seg in group]
    if not all_segs:
        return []

    seen_ids: set[str] = set()
    unique_by_id: list[ChapterSegment] = []
    for seg in sorted(
        all_segs,
        key=lambda s: (s.start_sec, 0 if s.kind in _SKIPPABLE_KINDS else 1, s.end_sec if s.end_sec is not None else float("inf"), s.id),
    ):
        if seg.id and seg.id in seen_ids:
            continue
        if seg.id:
            seen_ids.add(seg.id)
        unique_by_id.append(seg)

    merged: list[ChapterSegment] = []
    for seg in unique_by_id:
        replaced = False
        for idx in range(len(merged) - 1, -1, -1):
            prev = merged[idx]
            if prev.kind != seg.kind:
                continue
            prev_end = prev.end_sec if prev.end_sec is not None else prev.start_sec
            seg_end = seg.end_sec if seg.end_sec is not None else seg.start_sec
            if seg.kind in _SKIPPABLE_KINDS and seg.start_sec <= prev_end + _MERGE_EPSILON_SEC:
                merged[idx] = prev.model_copy(
                    update={
                        "start_sec": min(prev.start_sec, seg.start_sec),
                        "end_sec": max(prev_end, seg_end),
                        "label": prev.label if len(prev.label) >= len(seg.label) else seg.label,
                        "digest": prev.digest or seg.digest,
                    }
                )
                replaced = True
                break
            if (
                seg.kind == "content"
                and abs(prev.start_sec - seg.start_sec) <= _MERGE_EPSILON_SEC
                and abs(prev_end - seg_end) <= _MERGE_EPSILON_SEC
                and prev.label.strip().lower() == seg.label.strip().lower()
            ):
                replaced = True
                break
        if not replaced:
            merged.append(seg)

    merged.sort(
        key=lambda s: (s.start_sec, 0 if s.kind in _SKIPPABLE_KINDS else 1, s.end_sec if s.end_sec is not None else float("inf"), s.id)
    )
    return merged


def find_next_skippable_segment(
    segments: Sequence[ChapterSegment],
    current_sec: Any = 0.0,
) -> tuple[ChapterSegment | None, float | None, bool]:
    """Return `(next_segment, skip_to_sec, active_now)` for F9 sponsor/ad skipping."""
    try:
        pos = float(current_sec)
    except (TypeError, ValueError) as exc:
        raise SponsorBlockError("invalid_argument", f"current_sec 参数无效: {exc}") from exc
    if not math.isfinite(pos) or pos < 0:
        raise SponsorBlockError("invalid_argument", "current_sec 必须为非负有限数字")

    ordered = sorted(
        (s for s in segments if s.kind in _SKIPPABLE_KINDS),
        key=lambda s: (s.start_sec, s.end_sec if s.end_sec is not None else float("inf")),
    )
    for seg in ordered:
        if seg.end_sec is not None:
            if seg.end_sec > pos:
                return seg, seg.end_sec, seg.start_sec <= pos < seg.end_sec
        elif seg.start_sec >= pos:
            return seg, None, False
    return None, None, False
