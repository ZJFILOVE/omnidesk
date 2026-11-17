"""Stream resolution via yt-dlp (`ctx.exec_subprocess`, shell=False) or Bilibili playurl API."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .android_player import fetch_play_view_unite
from .client import BilibiliApiError, BilibiliClient
from .parsers import QN_TO_QUALITY
from .security import (
    DEFAULT_ANDROID_USER_AGENT,
    DEFAULT_USER_AGENT,
    validate_url,
)
from .ytdlp_streams import resolve_via_ytdlp

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
    client: BilibiliClient,
    target: dict[str, Any],
    *,
    cookie: str,
    req_label: str,
    req_qn: int | None,
    android_token: str = "",
    unlock_hd: bool = False,
    preferred_codec: int = 0,
    proxy: str = "",
    grpc_endpoint: str = "",
    curl_bin: str = "curl",
    plugin: BilibiliPlugin | None = None,
) -> dict[str, Any]:
    """Resolve video/audio streams via Bilibili `/x/player/wbi/playurl` or live playInfo API."""
    if target.get("room_id") and not target.get("bvid") and not target.get("aid"):
        return await _resolve_live_playurl(client, int(target["room_id"]), cookie=cookie, req_label=req_label, req_qn=req_qn)
    cid, title = await resolve_target_cid(client, target, cookie)
    try:
        data = await client.fetch_playurl(bvid=target.get("bvid", ""), aid=target.get("aid"), cid=cid, qn=req_qn or 127, cookie=cookie)
    except BilibiliApiError as exc:
        if exc.error_code in {"drm_protected", "not_found", "cancelled"}:
            raise
        android = await _try_android_stream(
            plugin, target=target, aid=target.get("aid"), cid=cid,
            req_qn=req_qn, current_best=0, android_token=android_token,
            unlock_hd=unlock_hd, preferred_codec=preferred_codec, proxy=proxy,
            grpc_endpoint=grpc_endpoint, curl_bin=curl_bin,
        )
        if android is not None:
            return {**android, "cid": cid, "title": title}
        raise
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
            "codecid": int(v.get("codecid") or 0),
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
    current_best = max(video_qns) if video_qns else actual_qn
    if req_qn is not None:
        has_exact = (req_qn in video_qns) or (bool(progressive) and actual_qn == req_qn)
        accepted = (not accept_qns) or (req_qn in accept_qns)
        if not has_exact or not accepted:
            android = await _try_android_stream(
                plugin,
                target=target,
                aid=target.get("aid"),
                cid=cid,
                req_qn=req_qn,
                current_best=current_best,
                android_token=android_token,
                unlock_hd=unlock_hd,
                preferred_codec=preferred_codec,
                proxy=proxy,
                grpc_endpoint=grpc_endpoint,
                curl_bin=curl_bin,
            )
            if android is not None:
                return {**android, "cid": cid, "title": title}
            avail = [QN_TO_QUALITY.get(q, str(q)) for q in sorted(accept_qns or video_qns, reverse=True)]
            hint = "" if android_token else "；4K/HDR 可能需要在插件页签授权 Android/TV 会话"
            raise BilibiliApiError(
                f"请求的清晰度 {req_label} (qn={req_qn}) 不可用，可用清晰度: {', '.join(avail) or 'none'}{hint}",
                error_code="quality_unavailable",
            )
    elif unlock_hd and android_token and cid:
        android = await _try_android_stream(
            plugin,
            target=target,
            aid=target.get("aid"),
            cid=cid,
            req_qn=None,
            current_best=current_best,
            android_token=android_token,
            unlock_hd=unlock_hd,
            preferred_codec=preferred_codec,
            proxy=proxy,
            grpc_endpoint=grpc_endpoint,
            curl_bin=curl_bin,
        )
        if android is not None:
            return {**android, "cid": cid, "title": title}

    candidates = [v for v in videos if req_qn is None or v["qn"] == req_qn]
    chosen_v = max(candidates, key=lambda v: (v["qn"], v["codecid"] == preferred_codec, v["bandwidth"])) if candidates else None
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


async def _try_android_stream(
    plugin: BilibiliPlugin | None,
    *,
    target: dict[str, Any],
    aid: Any,
    cid: int,
    req_qn: int | None,
    current_best: int,
    android_token: str,
    unlock_hd: bool,
    preferred_codec: int,
    proxy: str,
    grpc_endpoint: str,
    curl_bin: str,
) -> dict[str, Any] | None:
    if plugin is None or not unlock_hd or not android_token or cid <= 0:
        return None
    try:
        if not aid and target.get("bvid"):
            cookie, _ = await plugin.resolve_cookie()
            view = await plugin._build_client().fetch_view(bvid=target["bvid"], cookie=cookie)
            aid = view.get("aid")
            if aid:
                target["aid"] = int(aid)
        if not aid:
            return None
        candidate = await fetch_play_view_unite(
            plugin,
            aid=int(aid),
            cid=cid,
            qn=req_qn if req_qn is not None else 127,
            token=android_token,
            preferred_codec=preferred_codec,
            proxy=proxy,
            endpoint=grpc_endpoint,
            curl_bin=curl_bin,
            timeout_sec=12.0,
            allowed_loopback_origins=plugin._build_client().allowed_loopback_origins,
        )
    except (BilibiliApiError, ValueError, TypeError):
        return None
    if req_qn is None and int(candidate.get("qn") or 0) <= current_best:
        return None
    if req_qn is not None and int(candidate.get("qn") or 0) < req_qn:
        return None
    return candidate


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


def default_stream_headers(
    bvid: str = "",
    aid: int | None = None,
    *,
    android_media: bool = False,
) -> dict[str, str]:
    """Return safe playback headers (strictly excluding Cookie/Authorization)."""
    if android_media:
        return {"User-Agent": DEFAULT_ANDROID_USER_AGENT}
    referer = f"https://www.bilibili.com/video/{bvid}" if bvid else (f"https://www.bilibili.com/video/av{aid}" if aid else "https://www.bilibili.com/")
    return {"Referer": referer, "User-Agent": DEFAULT_USER_AGENT}
