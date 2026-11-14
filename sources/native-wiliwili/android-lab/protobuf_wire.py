"""Small, bounded protobuf wire reader for independently checking play replies."""


def varint(value):
    if value < 0 or value >= 1 << 64:
        raise ValueError("unsigned varint out of range")
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def message(fields):
    result = bytearray()
    for number, value in fields:
        if isinstance(value, int):
            result += varint(number << 3) + varint(value)
        else:
            if isinstance(value, str):
                value = value.encode()
            result += varint(number << 3 | 2) + varint(len(value)) + value
    return bytes(result)


def decode(data):
    pos = 0

    def read_varint():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError("truncated varint")
            byte = data[pos]
            pos += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                if value >= 1 << 64:
                    raise ValueError("oversize varint")
                return value
        raise ValueError("unterminated varint")

    fields = {}
    while pos < len(data):
        tag = read_varint()
        number, wire = tag >> 3, tag & 7
        if number < 1:
            raise ValueError("invalid field number")
        if wire == 0:
            value = read_varint()
        else:
            size = read_varint() if wire == 2 else {1: 8, 5: 4}.get(wire)
            if size is None or pos + size > len(data):
                raise ValueError("invalid or truncated field")
            value = data[pos:pos + size]
            pos += size
        fields.setdefault(number, []).append(value)
    return fields


def first(fields, number, default=0):
    return (fields.get(number) or [default])[0]


def play_reply(data):
    reply = decode(data)
    vod = decode(first(reply, 1, b""))
    videos = []
    for value in vod.get(5, []):
        stream = decode(value)
        info = decode(first(stream, 1, b""))
        dash = decode(first(stream, 2, b""))
        videos.append({"quality": first(info, 1), "need_vip": bool(first(info, 6)),
                       "need_login": bool(first(info, 7)), "intact": bool(first(info, 8)),
                       "has_url": bool(first(dash, 1, b"")),
                       "width": first(dash, 10), "height": first(dash, 11),
                       "bandwidth": first(dash, 3), "codec_id": first(dash, 4),
                       "size": first(dash, 6),
                       # URLs are only for private runtime use, remove before reporting.
                       "url": first(dash, 1, b"").decode(),
                       "backup_urls": [url.decode() for url in dash.get(2, [])]})
    return {"quality": first(vod, 1), "timelength": first(vod, 3),
            "trial_info_present": 7 in reply, "videos": videos,
            "audio_messages": vod.get(6, [])}


def public_reply(parsed):
    return {key: ([{k: v for k, v in stream.items() if k not in ("url", "backup_urls")}
                   for stream in value] if key == "videos" else value)
            for key, value in parsed.items() if key != "audio_messages"}
