"""yt-dlp based Bilibili stream resolution."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .client import BilibiliApiError
from .security import (
    SecurityValidationError,
    redact_bilibili_secrets,
    to_netscape_cookie_txt,
    validate_url,
)

if TYPE_CHECKING:
    from plugin import BilibiliPlugin


def _format_matches_quality(fmt: Mapping[str, Any], label: str, qn: int | None) -> bool:
    if qn is None:
        return True
    fid = str(fmt.get("format_id") or "")
    if fid.isdigit() and int(fid) == qn:
        return True
    height = int(fmt.get("height") or 0)
    fps = float(fmt.get("fps") or 0.0)
    note = str(fmt.get("format_note") or "").lower()
    if label == "4k":
        return height >= 2160 or "4k" in note
    if label == "8k":
        return height >= 4320 or "8k" in note
    if label == "1080p60":
        return height >= 1080 and fps >= 50
    if label == "1080p+":
        return height >= 1080 and (
            "1080p+" in note or "高码" in note or float(fmt.get("vbr") or 0) >= 3000
        )
    return height >= {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}.get(label, 0)


def _extract_ytdlp_streams(
    info: Mapping[str, Any], *, req_label: str, req_qn: int | None, allowed_origins: frozenset[str]
) -> dict[str, Any]:
    if info.get("has_drm") or info.get("drm"):
        raise BilibiliApiError("目标视频受 DRM 保护，拒绝绕过", error_code="drm_protected")
    formats = [f for f in (info.get("formats") or []) if isinstance(f, Mapping)]
    if any(f.get("has_drm") for f in formats):
        raise BilibiliApiError("流清单包含 DRM 受保护轨道，拒绝绕过", error_code="drm_protected")

    video_streams, audio_streams, progressive_streams = [], [], []
    for fmt in formats:
        raw_url = str(fmt.get("url") or "").strip()
        if not raw_url:
            continue
        safe_url = validate_url(raw_url, allow_cdn=True, allowed_loopback_origins=allowed_origins)
        vcodec, acodec = str(fmt.get("vcodec") or "none"), str(fmt.get("acodec") or "none")
        entry = {
            "url": safe_url,
            "format_id": str(fmt.get("format_id") or ""),
            "format_note": str(fmt.get("format_note") or ""),
            "codecs": vcodec if vcodec != "none" else acodec,
            "width": int(fmt.get("width") or 0),
            "height": int(fmt.get("height") or 0),
            "fps": float(fmt.get("fps") or 0.0),
            "bandwidth": int(fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr") or 0),
        }
        if vcodec != "none" and acodec != "none":
            progressive_streams.append(entry)
        elif vcodec != "none":
            video_streams.append(entry)
        elif acodec != "none":
            audio_streams.append(entry)

    top_url = str(info.get("url") or "").strip()
    if top_url and not (video_streams or progressive_streams):
        safe_top = validate_url(top_url, allow_cdn=True, allowed_loopback_origins=allowed_origins)
        progressive_streams.append(
            {"url": safe_top, "format_id": "", "height": int(info.get("height") or 0)}
        )

    candidates = video_streams or progressive_streams
    if not candidates:
        raise BilibiliApiError("yt-dlp 未返回可用视频流", error_code="quality_unavailable")

    matching = [s for s in candidates if _format_matches_quality(s, req_label, req_qn)]
    if req_qn is not None and not matching:
        raise BilibiliApiError(
            f"请求的清晰度 {req_label} 不可用（可能需要登录或大会员权限）",
            error_code="quality_unavailable",
        )
    chosen_v = (matching or candidates)[-1] if req_label == "auto" else matching[0]
    chosen_a = audio_streams[-1] if audio_streams else {}
    actual_label = req_label
    if req_label == "auto":
        actual_label = str(chosen_v.get("format_note") or f"{chosen_v.get('height', '')}p")
    return {
        "title": str(info.get("title") or ""),
        "duration_sec": float(info.get("duration") or 0.0),
        "quality": actual_label,
        "qn": req_qn,
        "video_url": chosen_v["url"],
        "audio_url": str(chosen_a.get("url") or ""),
        "play_url": chosen_v["url"],
        "streams": {
            "video": video_streams,
            "audio": audio_streams,
            "progressive": progressive_streams,
        },
        "source": "yt-dlp",
    }


async def resolve_via_ytdlp(
    plugin: BilibiliPlugin,
    ytdlp_bin: str,
    target_url: str,
    *,
    cookie: str,
    proxy: str,
    timeout_sec: float,
    req_label: str,
    req_qn: int | None,
    allowed_origins: frozenset[str],
) -> dict[str, Any]:
    """Run yt-dlp via `ctx.exec_subprocess` (shell=False) with a 0600 cookie file."""
    assert plugin.ctx is not None
    cookie_path = ""
    secrets = tuple(s for s in (cookie, proxy) if s)
    try:
        if cookie:
            fd, cookie_path = tempfile.mkstemp(prefix="bili-ck-", suffix=".txt")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(to_netscape_cookie_txt(cookie))
            except Exception:
                os.close(fd)
                raise
        args = [ytdlp_bin, "--dump-single-json", "--no-playlist", "--no-warnings"]
        if proxy:
            args.extend(["--proxy", proxy])
        if cookie_path:
            args.extend(["--cookies", cookie_path])
        args.append(target_url)

        res = await plugin.ctx.exec_subprocess(args, timeout_sec=timeout_sec)
        if res.timed_out:
            raise BilibiliApiError("yt-dlp 流解析超时", error_code="timeout", extra_secrets=secrets)
        if res.cancelled:
            raise BilibiliApiError("yt-dlp 流解析被取消", error_code="cancelled", extra_secrets=secrets)
        if not res.ok:
            err = redact_bilibili_secrets(res.stderr or res.stdout, secrets)
            lowered = err.lower()
            code = (
                "rate_limited"
                if "412" in lowered
                else (
                    "unauthorized"
                    if any(k in lowered for k in ("401", "login", "cookie"))
                    else ("drm_protected" if "drm" in lowered else "service_offline")
                )
            )
            raise BilibiliApiError(
                f"yt-dlp 执行失败: {err or f'rc={res.returncode}'}",
                error_code=code,
                extra_secrets=secrets,
            )

        info = json.loads(res.stdout)
        if not isinstance(info, Mapping):
            raise BilibiliApiError("yt-dlp 输出格式无效", error_code="invalid_response")
        return _extract_ytdlp_streams(
            info, req_label=req_label, req_qn=req_qn, allowed_origins=allowed_origins
        )
    except SecurityValidationError as exc:
        raise BilibiliApiError(str(exc), error_code=exc.error_code, extra_secrets=secrets) from None
    except FileNotFoundError:
        raise BilibiliApiError(
            f"未找到 yt-dlp 可执行文件: {ytdlp_bin}", error_code="service_offline"
        ) from None
    except ValueError as exc:
        raise BilibiliApiError(
            f"yt-dlp JSON 解析失败: {exc}", error_code="invalid_response"
        ) from None
    finally:
        if cookie_path:
            Path(cookie_path).unlink(missing_ok=True)
