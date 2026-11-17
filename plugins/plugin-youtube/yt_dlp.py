"""Safe yt-dlp subprocess runner, JSON parser, format selector, and failure mapper."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

from omnidesk_sdk import PluginContext, SubprocessResult

if __package__:
    from .yt_security import (
        canonical_watch_url,
        extract_video_id,
        hqdefault_cover_url,
        redact_sensitive,
        resolve_executable,
        sanitize_headers,
        validate_https_url,
    )
else:
    from yt_security import (
        canonical_watch_url,
        extract_video_id,
        hqdefault_cover_url,
        redact_sensitive,
        resolve_executable,
        sanitize_headers,
        validate_https_url,
    )

_QUALITY_HEIGHTS: dict[str, int | None] = {
    "best": None,
    "2160p": 2160,
    "4k": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "240p": 240,
    "144p": 144,
    "audio": 0,
}
_RESTRICTED_AVAILABILITY = frozenset({"subscriber_only", "premium_only", "needs_auth", "private"})


def parse_quality_limit(quality: str | int | None) -> tuple[bool, int | None]:
    """Return `(audio_only, max_height)` from a quality preference string."""
    if isinstance(quality, int) and not isinstance(quality, bool) and quality > 0:
        return False, quality
    raw = str(quality or "1080p").strip().lower()
    if raw == "audio":
        return True, 0
    if raw in _QUALITY_HEIGHTS:
        return False, _QUALITY_HEIGHTS[raw]
    digits = raw.removesuffix("p")
    return (False, int(digits)) if (digits.isdigit() and int(digits) > 0) else (False, 1080)


def _to_finite_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) and num >= 0.0 else default


def _to_nonneg_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def classify_ytdlp_failure(res: SubprocessResult, extra_secrets: Iterable[str] = ()) -> tuple[str, str, dict[str, Any]]:
    """Map non-zero/timed-out yt-dlp execution to canonical OmniDesk error codes."""
    if res.timed_out:
        return "timeout", "yt-dlp 执行超时", {"reason": "timeout"}
    if res.cancelled:
        return "cancelled", "yt-dlp 执行已取消", {"reason": "cancelled"}
    combined = redact_sensitive(f"{res.stderr}\n{res.stdout}".strip(), extra_secrets)
    low = combined.lower()
    if any(k in low for k in ("drm protected", "is drm", "widevine", "fairplay", "playready")):
        return "permission_denied", "该视频受 DRM 保护，不支持绕过", {"reason": "drm_protected"}
    if any(k in low for k in ("members-only", "members only", "join this channel", "subscribers only", "requires payment", "rental", "purchase")):
        return "permission_denied", "该视频仅限频道会员或付费观看，不支持绕过", {"reason": "members_only", "http_status": 403}
    if any(k in low for k in ("private video", "sign in to confirm", "cookies are required", "login required", "age-restricted", "confirm your age")):
        return "permission_denied", "该视频受登录、年龄或隐私限制，无法直接解析", {"reason": "auth_required", "http_status": 403}
    if any(k in low for k in ("http error 403", "403 forbidden", "status 403", "error 403")):
        return "permission_denied", "上游返回 HTTP 403 Forbidden", {"reason": "forbidden", "http_status": 403}
    if any(k in low for k in ("http error 401", "401 unauthorized", "status 401", "error 401")):
        return "permission_denied", "上游返回 HTTP 401 Unauthorized", {"reason": "unauthorized", "http_status": 401}
    if any(k in low for k in ("http error 429", "too many requests", "rate limit")):
        return "rate_limited", "上游请求过于频繁 (HTTP 429)", {"reason": "rate_limited", "http_status": 429}
    if any(k in low for k in ("video unavailable", "does not exist", "not available", "http error 404")):
        return "not_found", "视频不存在或当前不可用", {"reason": "not_found", "http_status": 404}
    excerpt = combined.splitlines()[-1][:240] if combined else f"returncode={res.returncode}"
    return "service_offline", f"yt-dlp 执行失败: {excerpt}", {"reason": "ytdlp_error", "returncode": res.returncode}


def parse_ytdlp_json_output(stdout: str) -> Any:
    """Parse single-JSON or NDJSON output from yt-dlp."""
    cleaned = stdout.strip()
    if not cleaned:
        raise ValueError("empty yt-dlp output")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        entries = [dict(json.loads(line)) for raw in cleaned.splitlines() if (line := raw.strip())]
        if entries:
            return {"entries": entries}
        raise


async def run_ytdlp_json(
    ctx: PluginContext, ytdlp_bin: str, args: list[str], *, timeout_sec: float = 15.0, extra_secrets: Iterable[str] = ()
) -> tuple[Any | None, dict[str, Any] | None]:
    """Run yt-dlp via PluginContext.exec_subprocess (shell=False) and decode JSON."""
    resolved, err_code = resolve_executable(ytdlp_bin)
    if err_code == "path_safety":
        return None, {"error_code": "path_safety", "error": "ytdlp_bin 包含非法路径字符"}
    if resolved is None:
        return None, {"error_code": "not_configured", "error": "未找到可用的 yt-dlp 可执行文件"}
    try:
        res = await ctx.exec_subprocess([resolved, *args], timeout_sec=timeout_sec)
    except FileNotFoundError:
        return None, {"error_code": "not_configured", "error": "yt-dlp 可执行文件不存在"}
    except (PermissionError, OSError) as exc:
        return None, {"error_code": "service_offline", "error": f"启动 yt-dlp 失败: {redact_sensitive(str(exc) or type(exc).__name__, extra_secrets)}"}
    if not res.ok:
        code, msg, extra = classify_ytdlp_failure(res, extra_secrets)
        return None, {"error_code": code, "error": msg, **extra}
    try:
        return parse_ytdlp_json_output(res.stdout), None
    except (json.JSONDecodeError, ValueError):
        return None, {"error_code": "service_offline", "error": "yt-dlp 返回的 JSON 数据无法解析"}


async def fetch_video_info(
    ctx: PluginContext,
    ytdlp_bin: str,
    video_or_url: str,
    *,
    timeout_sec: float = 15.0,
    extra_secrets: Iterable[str] = (),
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch full yt-dlp -J metadata for a single validated YouTube video."""
    try:
        vid = extract_video_id(
            video_or_url, allow_loopback_http=allow_loopback_http, allowed_loopback_origins=allowed_loopback_origins
        )
    except ValueError as exc:
        return None, {"error_code": "invalid_argument", "error": redact_sensitive(str(exc), extra_secrets)}
    payload, err = await run_ytdlp_json(
        ctx, ytdlp_bin, ["-J", "--no-playlist", "--no-warnings", canonical_watch_url(vid)], timeout_sec=timeout_sec, extra_secrets=extra_secrets
    )
    if err is not None:
        return None, err
    if not isinstance(payload, Mapping):
        return None, {"error_code": "service_offline", "error": "yt-dlp 未返回有效视频元数据"}
    data = dict(payload)
    data.setdefault("id", vid)
    return data, None


def _format_summary(fmt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "format_id": str(fmt.get("format_id") or ""),
        "ext": str(fmt.get("ext") or ""),
        "height": _to_nonneg_int(fmt.get("height")),
        "fps": _to_finite_float(fmt.get("fps")),
        "vcodec": str(fmt.get("vcodec") or "none"),
        "acodec": str(fmt.get("acodec") or "none"),
        "tbr": _to_finite_float(fmt.get("tbr")),
        "abr": _to_finite_float(fmt.get("abr")),
        "format_note": str(fmt.get("format_note") or ""),
    }


def _pick_video_format(candidates: list[dict[str, Any]], max_height: int | None) -> dict[str, Any] | None:
    if not candidates:
        return None
    rank = lambda f: (_to_nonneg_int(f.get("height")), _to_finite_float(f.get("tbr")) or _to_finite_float(f.get("vbr")), _to_finite_float(f.get("fps")), 1 if str(f.get("acodec") or "none").lower() != "none" else 0)
    if max_height is None:
        return max(candidates, key=rank)
    within = [f for f in candidates if 0 < _to_nonneg_int(f.get("height")) <= max_height]
    if within:
        return max(within, key=rank)
    with_height = [f for f in candidates if _to_nonneg_int(f.get("height")) > 0]
    if with_height:
        return min(with_height, key=lambda f: (_to_nonneg_int(f.get("height")), -_to_finite_float(f.get("tbr"))))
    return max(candidates, key=rank)


def _pick_audio_format(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    pool = [f for f in candidates if str(f.get("vcodec") or "none").lower() == "none"] or candidates
    return max(pool, key=lambda f: (_to_finite_float(f.get("abr")), _to_finite_float(f.get("tbr"))))


def select_stream_formats(
    info: Mapping[str, Any],
    *,
    quality: str = "1080p",
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Filter DRM/non-HTTPS/storyboard formats and pick optimal video/audio streams."""
    if info.get("has_drm") is True:
        return None, {"error_code": "permission_denied", "reason": "drm_protected", "error": "该视频受 DRM 保护，不支持绕过"}
    availability = str(info.get("availability") or "").strip().lower()
    saw_drm, saw_insecure = False, False
    video_candidates, audio_candidates = [], []

    for entry in info.get("formats") or []:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("has_drm") is True:
            saw_drm = True
            continue
        ext, proto = str(entry.get("ext") or "").lower(), str(entry.get("protocol") or "").lower()
        vcodec, acodec = str(entry.get("vcodec") or "").lower(), str(entry.get("acodec") or "").lower()
        if ext == "mhtml" or proto == "mhtml" or (vcodec == "none" and acodec == "none"):
            continue
        raw_url = str(entry.get("url") or "").strip()
        if not raw_url:
            continue
        try:
            safe_url = validate_https_url(
                raw_url, allow_loopback_http=allow_loopback_http, allowed_loopback_origins=allowed_loopback_origins
            )
        except ValueError:
            saw_insecure = True
            continue
        fmt = {**dict(entry), "url": safe_url}
        has_v = (vcodec not in {"", "none"}) or (_to_nonneg_int(fmt.get("height")) > 0 and vcodec != "none")
        has_a = acodec not in {"", "none"}
        if not has_v and not has_a:
            has_v = True
        if has_v:
            video_candidates.append(fmt)
        if has_a:
            audio_candidates.append(fmt)

    audio_only, max_height = parse_quality_limit(quality)
    best_video = None if audio_only else _pick_video_format(video_candidates, max_height)
    best_audio = _pick_audio_format(audio_candidates)
    selected: dict[str, Any] | None = None
    audio_url, quality_label = "", "audio"

    if audio_only and best_audio is not None:
        selected, audio_url = best_audio, str(best_audio["url"])
    elif best_video is not None:
        selected = best_video
        if str(best_video.get("acodec") or "none").lower() in {"", "none"} and best_audio is not None:
            audio_url = str(best_audio["url"])
        h = _to_nonneg_int(best_video.get("height"))
        quality_label = f"{h}p" if h > 0 else str(best_video.get("format_note") or quality or "best")
    elif best_audio is not None:
        selected, audio_url = best_audio, str(best_audio["url"])
    elif isinstance(info.get("url"), str) and info["url"].strip():
        try:
            d_url = validate_https_url(info["url"].strip(), allow_loopback_http=allow_loopback_http, allowed_loopback_origins=allowed_loopback_origins)
            selected = {"url": d_url, "format_id": str(info.get("format_id") or "direct"), "ext": str(info.get("ext") or "mp4"), "http_headers": info.get("http_headers")}
            quality_label = str(quality or "best")
        except ValueError:
            saw_insecure = True

    if selected is None:
        if availability in _RESTRICTED_AVAILABILITY:
            return None, {"error_code": "permission_denied", "reason": availability, "http_status": 403, "error": f"视频受访问限制 ({availability})，不支持绕过"}
        if saw_drm:
            return None, {"error_code": "permission_denied", "reason": "drm_protected", "error": "仅检测到受 DRM 保护的流，不支持绕过"}
        if saw_insecure:
            return None, {"error_code": "invalid_argument", "reason": "insecure_url", "error": "仅允许 https:// 媒体流地址"}
        return None, {"error_code": "not_found", "reason": "no_formats", "error": "未找到可用的视频或音频流格式"}

    headers = sanitize_headers(info.get("http_headers"))
    headers.update(sanitize_headers(selected.get("http_headers")))
    raw_vid = str(info.get("id") or "")
    return {
        "video_id": raw_vid,
        "title": str(info.get("title") or ""),
        "channel": str(info.get("channel") or info.get("uploader") or ""),
        "duration_sec": _to_finite_float(info.get("duration")),
        "cover_url": hqdefault_cover_url(raw_vid) if len(raw_vid) == 11 else "",
        "play_url": str(selected["url"]),
        "audio_url": audio_url,
        "headers": headers,
        "quality": quality_label,
        "format_id": str(selected.get("format_id") or ""),
        "ext": str(selected.get("ext") or ""),
        "video_formats": [_format_summary(f) for f in video_candidates],
        "audio_formats": [_format_summary(f) for f in audio_candidates],
    }, None
