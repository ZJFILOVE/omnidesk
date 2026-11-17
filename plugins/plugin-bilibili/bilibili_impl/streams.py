"""Stream resolution via yt-dlp (`ctx.exec_subprocess`, shell=False) or Bilibili playurl API."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .client import BilibiliApiError, BilibiliClient
from .parsers import QN_TO_QUALITY
from .security import DEFAULT_USER_AGENT, SecurityValidationError, redact_bilibili_secrets, to_netscape_cookie_txt, validate_url

if TYPE_CHECKING:
    from plugin import BilibiliPlugin


async def resolve_target_cid(client: BilibiliClient, target: dict[str, Any], cookie: str) -> tuple[int, str]:
    """Resolve missing `cid` and `title` via `/x/player/pagelist` or `/x/web-interface/view`."""
    if target.get("cid"):
        return int(target["cid"]), ""
    bvid, aid = target.get("bvid", ""), target.get("aid")
    if not bvid and not aid:
        raise BilibiliApiError("无法解析视频 CID：缺少 bvid 或 aid", error_code="invalid_argument")
    pages = await client.fetch_pagelist(bvid=bvid, aid=aid, cookie=cookie)
    title = ""
    if not pages:
        view = await client.fetch_view(bvid=bvid, aid=aid, cookie=cookie)
        pages = view.get("pages") if isinstance(view.get("pages"), list) else []
        title = str(view.get("title") or "")
        if view.get("cid"):
            return int(view["cid"]), title
    if not pages:
        raise BilibiliApiError("目标视频未返回任何分P/CID", error_code="not_found")
    idx = min(max(0, int(target.get("page", 1)) - 1), len(pages) - 1)
    cid = pages[idx].get("cid")
    if not cid:
        raise BilibiliApiError("分P信息缺少有效 CID", error_code="invalid_response")
    return int(cid), title or str(pages[idx].get("part") or "")


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
        return height >= 1080 and ("1080p+" in note or "高码" in note or float(fmt.get("vbr") or 0) >= 3000)
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
        progressive_streams.append({"url": safe_top, "format_id": "", "height": int(info.get("height") or 0)})

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
    return {
        "title": str(info.get("title") or ""),
        "duration_sec": float(info.get("duration") or 0.0),
        "quality": req_label if req_label != "auto" else (chosen_v.get("format_note") or f"{chosen_v.get('height', '')}p"),
        "qn": req_qn,
        "video_url": chosen_v["url"],
        "audio_url": str(chosen_a.get("url") or ""),
        "play_url": chosen_v["url"],
        "streams": {"video": video_streams, "audio": audio_streams, "progressive": progressive_streams},
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
    """Run yt-dlp via `ctx.exec_subprocess` (shell=False) with 0600 ephemeral cookie file."""
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
                "rate_limited" if "412" in lowered
                else ("unauthorized" if any(k in lowered for k in ("401", "login", "cookie"))
                      else ("drm_protected" if "drm" in lowered else "service_offline"))
            )
            raise BilibiliApiError(f"yt-dlp 执行失败: {err or f'rc={res.returncode}'}", error_code=code, extra_secrets=secrets)

        info = json.loads(res.stdout)
        if not isinstance(info, Mapping):
            raise BilibiliApiError("yt-dlp 输出格式无效", error_code="invalid_response")
        return _extract_ytdlp_streams(info, req_label=req_label, req_qn=req_qn, allowed_origins=allowed_origins)
    except SecurityValidationError as exc:
        raise BilibiliApiError(str(exc), error_code=exc.error_code, extra_secrets=secrets) from None
    except FileNotFoundError:
        raise BilibiliApiError(f"未找到 yt-dlp 可执行文件: {ytdlp_bin}", error_code="service_offline") from None
    except ValueError as exc:
        raise BilibiliApiError(f"yt-dlp JSON 解析失败: {exc}", error_code="invalid_response") from None
    finally:
        if cookie_path:
            Path(cookie_path).unlink(missing_ok=True)


async def _resolve_live_playurl(
    client: BilibiliClient, room_id: int, *, cookie: str, req_label: str, req_qn: int | None
) -> dict[str, Any]:
    """Resolve live room stream URLs via `/xlive/web-room/v2/index/getRoomPlayInfo`."""
    payload = await client.get_json(
        "/xlive/web-room/v2/index/getRoomPlayInfo",
        params={"room_id": room_id, "protocol": "0,1", "format": "0,1,2", "codec": "0,1", "qn": req_qn or 10000},
        cookie=cookie,
        base_override=client.live_base,
    )
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    if data.get("live_status") == 0:
        raise BilibiliApiError("直播间当前未开播", error_code="not_found")
    playurl_info = data.get("playurl_info") if isinstance(data.get("playurl_info"), Mapping) else {}
    playurl = playurl_info.get("playurl") if isinstance(playurl_info.get("playurl"), Mapping) else {}
    streams = playurl.get("stream") if isinstance(playurl.get("stream"), list) else []
    allowed = client.allowed_loopback_origins
    urls: list[dict[str, Any]] = []
    for st in streams:
        for fmt in (st.get("format") or []) if isinstance(st, Mapping) else ():
            for codec in (fmt.get("codec") or []) if isinstance(fmt, Mapping) else ():
                base_url = str(codec.get("base_url") or "")
                for info in (codec.get("url_info") or []) if isinstance(codec, Mapping) else ():
                    host = str(info.get("host") or "")
                    extra = str(info.get("extra") or "")
                    if host and base_url:
                        safe = validate_url(f"{host}{base_url}{extra}", allow_cdn=True, allowed_loopback_origins=allowed)
                        urls.append({"url": safe, "qn": int(codec.get("current_qn") or 0), "codecs": str(codec.get("codec_name") or "")})
    if not urls:
        raise BilibiliApiError("直播间未返回可用流地址", error_code="quality_unavailable")
    return {
        "room_id": room_id,
        "title": f"直播间 {room_id}",
        "quality": req_label if req_label != "auto" else "live",
        "qn": urls[0]["qn"],
        "available_qualities": ["live"],
        "video_url": urls[0]["url"],
        "audio_url": "",
        "play_url": urls[0]["url"],
        "streams": {"video": urls, "audio": [], "progressive": urls},
        "duration_sec": 0.0,
        "source": "live_playurl",
    }


async def resolve_via_playurl(
    client: BilibiliClient, target: dict[str, Any], *, cookie: str, req_label: str, req_qn: int | None
) -> dict[str, Any]:
    """Resolve video/audio streams via Bilibili `/x/player/wbi/playurl` or live playInfo API."""
    if target.get("room_id") and not target.get("bvid") and not target.get("aid"):
        return await _resolve_live_playurl(client, int(target["room_id"]), cookie=cookie, req_label=req_label, req_qn=req_qn)
    cid, title = await resolve_target_cid(client, target, cookie)
    data = await client.fetch_playurl(bvid=target.get("bvid", ""), aid=target.get("aid"), cid=cid, qn=req_qn or 80, cookie=cookie)
    if data.get("is_preview") == 1 and not data.get("dash") and not data.get("durl"):
        raise BilibiliApiError("当前视频为试看或受限片段，无完整可用流", error_code="quality_unavailable")
    if data.get("drm_tech_type"):
        raise BilibiliApiError("目标流启用了 DRM 加密，拒绝绕过", error_code="drm_protected")

    accept_qns = [int(q) for q in (data.get("accept_quality") or []) if isinstance(q, int)]
    actual_qn = int(data.get("quality") or 0)
    dash = data.get("dash") if isinstance(data.get("dash"), Mapping) else {}
    allowed = client.allowed_loopback_origins

    videos = [
        {
            "url": validate_url(str(v.get("baseUrl") or v.get("base_url") or "").strip(), allow_cdn=True, allowed_loopback_origins=allowed),
            "qn": int(v.get("id") or 0),
            "quality": QN_TO_QUALITY.get(int(v.get("id") or 0), str(v.get("id") or "")),
            "codecs": str(v.get("codecs") or ""),
            "width": int(v.get("width") or 0),
            "height": int(v.get("height") or 0),
            "fps": str(v.get("frameRate") or v.get("frame_rate") or ""),
            "bandwidth": int(v.get("bandwidth") or 0),
        }
        for v in (dash.get("video") or [])
        if isinstance(v, Mapping) and str(v.get("baseUrl") or v.get("base_url") or "").strip()
    ]
    audios = [
        {
            "url": validate_url(str(a.get("baseUrl") or a.get("base_url") or "").strip(), allow_cdn=True, allowed_loopback_origins=allowed),
            "id": int(a.get("id") or 0),
            "codecs": str(a.get("codecs") or ""),
            "bandwidth": int(a.get("bandwidth") or 0),
        }
        for a in (dash.get("audio") or [])
        if isinstance(a, Mapping) and str(a.get("baseUrl") or a.get("base_url") or "").strip()
    ]
    progressive = [
        {
            "url": validate_url(str(d.get("url") or "").strip(), allow_cdn=True, allowed_loopback_origins=allowed),
            "size": int(d.get("size") or 0),
            "length": int(d.get("length") or 0),
        }
        for d in (data.get("durl") or [])
        if isinstance(d, Mapping) and str(d.get("url") or "").strip()
    ]

    video_qns = {v["qn"] for v in videos if v["qn"] > 0}
    if req_qn is not None:
        has_exact = (req_qn in video_qns) or (bool(progressive) and actual_qn == req_qn)
        accepted = (not accept_qns) or (req_qn in accept_qns)
        if not has_exact or not accepted:
            avail = [QN_TO_QUALITY.get(q, str(q)) for q in sorted(accept_qns or video_qns, reverse=True)]
            raise BilibiliApiError(
                f"请求的清晰度 {req_label} (qn={req_qn}) 不可用，可用清晰度: {', '.join(avail) or 'none'}",
                error_code="quality_unavailable",
            )

    chosen_v = next((v for v in videos if v["qn"] == req_qn), None) if req_qn is not None else (max(videos, key=lambda v: v["qn"]) if videos else None)
    if chosen_v is None and progressive:
        chosen_v = {"url": progressive[0]["url"], "qn": actual_qn}
    if chosen_v is None:
        raise BilibiliApiError("playurl 未返回可用的视频流地址", error_code="quality_unavailable")
    chosen_a = max(audios, key=lambda a: a["bandwidth"]) if audios else {}
    resolved_qn = int(chosen_v.get("qn") or actual_qn)
    return {
        "cid": cid,
        "title": title,
        "quality": QN_TO_QUALITY.get(resolved_qn, req_label),
        "qn": resolved_qn,
        "available_qualities": [QN_TO_QUALITY.get(q, str(q)) for q in (accept_qns or sorted(video_qns, reverse=True))],
        "video_url": chosen_v["url"],
        "audio_url": str(chosen_a.get("url") or ""),
        "play_url": chosen_v["url"],
        "streams": {"video": videos, "audio": audios, "progressive": progressive},
        "duration_sec": float(data.get("timelength") or 0) / 1000.0,
        "source": "playurl",
    }


def resolve_ytdlp_mode(client: BilibiliClient, *, method: str, ytdlp_bin: str) -> tuple[str, str]:
    """Select `ytdlp` vs `playurl` without accidentally calling host yt-dlp against local mock servers."""
    if method == "playurl":
        return "playurl", ""
    if method == "ytdlp":
        if not ytdlp_bin:
            raise BilibiliApiError("未配置 ytdlp_bin", error_code="not_configured")
        return "ytdlp", ytdlp_bin
    if client.is_custom_base and ytdlp_bin in {"", "yt-dlp"}:
        return "playurl", ""
    if ytdlp_bin:
        has_sep = "/" in ytdlp_bin or "\\" in ytdlp_bin
        exists = Path(ytdlp_bin).expanduser().is_file() if has_sep else bool(shutil.which(ytdlp_bin))
        if exists:
            return "ytdlp", ytdlp_bin
        if ytdlp_bin != "yt-dlp":
            raise BilibiliApiError(f"未找到指定的 ytdlp_bin: {ytdlp_bin}", error_code="service_offline")
    return "playurl", ""


def default_stream_headers(bvid: str = "", aid: int | None = None) -> dict[str, str]:
    """Return safe playback headers (strictly excluding Cookie/Authorization)."""
    referer = f"https://www.bilibili.com/video/{bvid}" if bvid else (f"https://www.bilibili.com/video/av{aid}" if aid else "https://www.bilibili.com/")
    return {"Referer": referer, "User-Agent": DEFAULT_USER_AGENT}
