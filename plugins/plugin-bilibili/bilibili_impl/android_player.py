"""Android/TV PlayViewUnite transport and DASH candidate normalization."""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .client import BilibiliApiError
from .parsers import QN_TO_QUALITY
from .protobuf_grpc import (
    ProtoError,
    build_play_view_unite_request,
    field_bytes,
    field_varint,
    parse_play_view_unite_reply,
    wrap_grpc_frame,
)
from .security import DEFAULT_ANDROID_USER_AGENT, SecurityValidationError, redact_bilibili_secrets, validate_url

if TYPE_CHECKING:
    from plugin import BilibiliPlugin

DEFAULT_GRPC_ENDPOINT = (
    "https://grpc.biliapi.net/bilibili.app.playerunite.v1.Player/PlayViewUnite"
)
_CODEC_NAMES = {7: "avc1", 12: "hev1", 13: "av01"}


def _b64(message: bytes) -> str:
    return base64.b64encode(message).decode("ascii")


def _android_metadata(token: str) -> tuple[str, str, str, str]:
    metadata = field_bytes(1, token) if token else b""
    metadata += field_bytes(2, "android") + field_varint(4, 9130500)
    metadata += field_bytes(5, "master") + field_bytes(7, "android")
    device = (
        field_varint(1, 1)
        + field_varint(2, 9130500)
        + field_bytes(4, "android")
        + field_bytes(5, "android")
        + field_bytes(7, "master")
        + field_bytes(8, "redroid")
        + field_bytes(9, "redroid")
        + field_bytes(10, "12")
    )
    network = field_varint(1, 1) + field_bytes(2, "46007")
    fawkes = field_bytes(1, "android64") + field_bytes(2, "prod") + field_bytes(3, "omnidesk-bili")
    return _b64(metadata), _b64(device), _b64(network), _b64(fawkes)


def _header_file(token: str) -> str:
    metadata, device, network, fawkes = _android_metadata(token)
    lines = [
        "content-type: application/grpc",
        "te: trailers",
        f"user-agent: {DEFAULT_ANDROID_USER_AGENT}",
        "grpc-accept-encoding: identity,gzip",
        f"x-bili-metadata-bin: {metadata}",
        f"x-bili-device-bin: {device}",
        f"x-bili-network-bin: {network}",
        f"x-bili-fawkes-req-bin: {fawkes}",
        f"authorization: identify_v1 {token}",
    ]
    fd, path = tempfile.mkstemp(prefix="bili-grpc-", suffix=".headers")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except Exception:
        os.close(fd)
        Path(path).unlink(missing_ok=True)
        raise
    return path


async def fetch_play_view_unite(
    plugin: BilibiliPlugin,
    *,
    aid: int,
    cid: int,
    qn: int,
    token: str,
    preferred_codec: int,
    proxy: str,
    endpoint: str,
    curl_bin: str,
    timeout_sec: float,
    allowed_loopback_origins: frozenset[str],
) -> dict[str, Any]:
    """Call PlayViewUnite over HTTP/2 using curl with a 0600 ephemeral header file."""
    if plugin.ctx is None:
        raise BilibiliApiError("插件尚未加载", error_code="not_configured")
    safe_endpoint = validate_url(
        endpoint,
        allow_cdn=False,
        allowed_loopback_origins=allowed_loopback_origins,
    )
    framed = wrap_grpc_frame(
        build_play_view_unite_request(aid, cid, qn, preferred_codec=preferred_codec, trial=True)
    )
    header_path = _header_file(token)
    secrets = tuple(secret for secret in (token, proxy) if secret)
    try:
        args = [
            curl_bin,
            "--http2",
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--no-progress-meter",
            "--max-time",
            f"{max(0.5, timeout_sec):.2f}",
            "--request",
            "POST",
            "--url",
            safe_endpoint,
            "--header",
            f"@{header_path}",
            "--data-binary",
            "@-",
        ]
        if proxy:
            args.extend(["--proxy", proxy])
        result = await plugin.ctx.exec_subprocess(
            args,
            stdin_data=framed,
            timeout_sec=timeout_sec + 1.0,
        )
    finally:
        Path(header_path).unlink(missing_ok=True)

    if result.timed_out:
        raise BilibiliApiError("Android/TV 取流请求超时", error_code="timeout", extra_secrets=secrets)
    if result.cancelled:
        raise BilibiliApiError("Android/TV 取流请求已取消", error_code="cancelled", extra_secrets=secrets)
    if not result.ok:
        detail = redact_bilibili_secrets((result.stderr or result.stdout).strip(), secrets)
        raise BilibiliApiError(
            f"Android/TV PlayViewUnite 请求失败: {detail or f'rc={result.returncode}'}",
            error_code="service_offline" if result.returncode != 22 else "quality_unavailable",
            extra_secrets=secrets,
        )
    try:
        parsed = parse_play_view_unite_reply(result.stdout_bytes)
    except ProtoError as exc:
        raise BilibiliApiError(
            f"PlayViewUnite 响应解析失败: {exc}",
            error_code="invalid_response",
            extra_secrets=secrets,
        ) from None
    return normalize_android_reply(parsed, allowed_loopback_origins=allowed_loopback_origins)


def normalize_android_reply(
    parsed: dict[str, Any],
    *,
    allowed_loopback_origins: frozenset[str],
) -> dict[str, Any]:
    """Validate and normalize PlayViewUnite video/audio URLs."""
    videos: list[dict[str, Any]] = []
    audios: list[dict[str, Any]] = []
    try:
        for video in parsed.get("videos", []):
            if not isinstance(video, dict) or not video.get("url"):
                continue
            videos.append(
                {
                    **video,
                    "url": validate_url(
                        str(video["url"]),
                        allow_cdn=True,
                        allowed_loopback_origins=allowed_loopback_origins,
                    ),
                    "quality": QN_TO_QUALITY.get(int(video.get("qn") or 0), str(video.get("qn") or "")),
                    "codecs": _CODEC_NAMES.get(int(video.get("codecid") or 0), ""),
                }
            )
        for audio in parsed.get("audios", []):
            if not isinstance(audio, dict) or not audio.get("url"):
                continue
            audios.append(
                {
                    **audio,
                    "url": validate_url(
                        str(audio["url"]),
                        allow_cdn=True,
                        allowed_loopback_origins=allowed_loopback_origins,
                    ),
                }
            )
    except SecurityValidationError as exc:
        raise BilibiliApiError(str(exc), error_code=exc.error_code) from None
    if not videos:
        raise BilibiliApiError("PlayViewUnite 未返回可用视频流", error_code="quality_unavailable")
    best = max(videos, key=lambda item: int(item.get("qn") or 0))
    audio = max(audios, key=lambda item: int(item.get("bandwidth") or 0)) if audios else {}
    return {
        "quality": QN_TO_QUALITY.get(int(parsed.get("quality") or 0), ""),
        "qn": int(best.get("qn") or parsed.get("quality") or 0),
        "available_qualities": [QN_TO_QUALITY.get(int(item.get("qn") or 0), str(item.get("qn") or "")) for item in videos],
        "video_url": best["url"],
        "audio_url": str(audio.get("url") or ""),
        "play_url": best["url"],
        "streams": {"video": videos, "audio": audios, "progressive": []},
        "duration_sec": float(parsed.get("duration_sec") or 0.0),
        "android_media": True,
        "source": "android_playview",
    }
