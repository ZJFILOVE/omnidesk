"""Minimal protobuf wire codec and gRPC framing for PlayViewUnite."""

from __future__ import annotations

import gzip
import io
import struct
from collections.abc import Iterable
from typing import Any

_MAX_UNCOMPRESSED = 16 * 1024 * 1024


class ProtoError(ValueError):
    """Raised when a protobuf or gRPC payload is malformed."""


def write_varint(value: int) -> bytes:
    """Encode an unsigned protobuf varint."""
    if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("varint value out of uint64 range")
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def field_varint(field: int, value: int) -> bytes:
    """Encode a varint field."""
    return write_varint((field << 3) | 0) + write_varint(value)


def field_bytes(field: int, value: bytes | str) -> bytes:
    """Encode a length-delimited field."""
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return write_varint((field << 3) | 2) + write_varint(len(raw)) + raw


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode one unsigned varint, returning `(value, new_pos)`."""
    value = 0
    for shift in range(0, 64, 7):
        if pos >= len(data):
            raise ProtoError("truncated varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if shift == 63 and byte > 1:
                raise ProtoError("varint overflow")
            return value, pos
    raise ProtoError("varint too long")


def decode_fields(data: bytes, *, depth: int = 0) -> list[tuple[int, int, Any]]:
    """Decode protobuf fields into `(number, wire_type, value)` tuples."""
    if depth > 12:
        raise ProtoError("protobuf nesting too deep")
    fields: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if number <= 0:
            raise ProtoError("invalid protobuf field number")
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire == 1:
            if pos + 8 > len(data):
                raise ProtoError("truncated fixed64")
            value, pos = data[pos : pos + 8], pos + 8
        elif wire == 2:
            length, pos = read_varint(data, pos)
            if length > len(data) - pos:
                raise ProtoError("truncated length-delimited field")
            value, pos = data[pos : pos + length], pos + length
        elif wire == 5:
            if pos + 4 > len(data):
                raise ProtoError("truncated fixed32")
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ProtoError(f"unsupported protobuf wire type {wire}")
        fields.append((number, wire, value))
    return fields


def wrap_grpc_frame(message: bytes) -> bytes:
    """Wrap a protobuf message in an uncompressed gRPC data frame."""
    return b"\x00" + struct.pack(">I", len(message)) + message


def unwrap_grpc_frame(payload: bytes) -> bytes:
    """Return the first gRPC message, accepting raw protobuf or gzip-compressed frames."""
    if len(payload) < 5 or payload[0] not in (0, 1):
        return payload
    length = struct.unpack(">I", payload[1:5])[0]
    if length <= 0 or length > len(payload) - 5:
        raise ProtoError("invalid gRPC frame length")
    message = payload[5 : 5 + length]
    if payload[0] == 0:
        return message
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(message)) as handle:
            result = handle.read(_MAX_UNCOMPRESSED + 1)
    except OSError as exc:
        raise ProtoError(f"invalid gzip gRPC frame: {exc}") from None
    if len(result) > _MAX_UNCOMPRESSED:
        raise ProtoError("gRPC frame exceeds decompression limit")
    return result


def build_play_view_unite_request(
    aid: int,
    cid: int,
    qn: int,
    *,
    preferred_codec: int = 0,
    trial: bool = True,
) -> bytes:
    """Build the PlayViewUnite protobuf request used by the Android/TV client."""
    vod = b"".join(
        (
            field_varint(1, aid),
            field_varint(2, cid),
            field_varint(3, qn if qn > 0 else 127),
            field_varint(4, preferred_codec) if preferred_codec > 0 else b"",
            field_varint(5, 4048),
            field_varint(7, 2),
            field_varint(8, 1),
            field_varint(9, 2),
            field_varint(11, 1 if trial else 0),
        )
    )
    return b"".join(
        (
            field_bytes(1, vod),
            field_bytes(2, "united.player-video-detail.0.0"),
            field_bytes(3, "main.my-history.0.0"),
        )
    )


def _first(fields: Iterable[tuple[int, int, Any]], number: int, wire: int, default: Any = None) -> Any:
    for field_no, field_wire, value in fields:
        if field_no == number and field_wire == wire:
            return value
    return default


def _all(fields: Iterable[tuple[int, int, Any]], number: int, wire: int) -> list[Any]:
    return [value for field_no, field_wire, value in fields if field_no == number and field_wire == wire]


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value or "")


def parse_play_view_unite_reply(payload: bytes) -> dict[str, Any]:
    """Parse PlayViewUnite DASH video/audio streams and playback metadata."""
    top = decode_fields(unwrap_grpc_frame(payload))
    vod_raw = _first(top, 1, 2)
    if not isinstance(vod_raw, (bytes, bytearray)):
        raise ProtoError("PlayViewUnite reply has no Vod message")
    vod = decode_fields(bytes(vod_raw))
    quality = int(_first(vod, 1, 0, 0) or 0)
    duration_ms = int(_first(vod, 3, 0, 0) or 0)

    videos: list[dict[str, Any]] = []
    for stream_raw in _all(vod, 5, 2):
        stream = decode_fields(bytes(stream_raw))
        info_raw = _first(stream, 1, 2)
        stream_qn = 0
        if isinstance(info_raw, (bytes, bytearray)):
            info = decode_fields(bytes(info_raw))
            stream_qn = int(_first(info, 1, 0, 0) or 0)
        dash_raw = _first(stream, 2, 2)
        if not isinstance(dash_raw, (bytes, bytearray)):
            continue
        dash = decode_fields(bytes(dash_raw))
        base_url = _first(dash, 1, 2, b"")
        if not base_url:
            continue
        videos.append(
            {
                "url": _text(base_url),
                "backup_urls": [_text(url) for url in _all(dash, 2, 2)],
                "qn": stream_qn,
                "codecs": "",
                "codecid": int(_first(dash, 4, 0, 0) or 0),
                "bandwidth": int(_first(dash, 3, 0, 0) or 0),
                "width": int(_first(dash, 10, 0, 0) or 0),
                "height": int(_first(dash, 11, 0, 0) or 0),
                "fps": str(_first(dash, 12, 0, "") or ""),
            }
        )

    audios: list[dict[str, Any]] = []
    for audio_raw in _all(vod, 6, 2):
        audio = decode_fields(bytes(audio_raw))
        base_url = _first(audio, 2, 2, b"")
        if not base_url:
            continue
        audios.append(
            {
                "url": _text(base_url),
                "backup_urls": [_text(url) for url in _all(audio, 3, 2)],
                "id": int(_first(audio, 1, 0, 0) or 0),
                "bandwidth": int(_first(audio, 4, 0, 0) or 0),
                "codecid": int(_first(audio, 5, 0, 0) or 0),
            }
        )

    return {
        "quality": quality,
        "duration_sec": duration_ms / 1000.0,
        "videos": videos,
        "audios": audios,
        "android_media": True,
    }
