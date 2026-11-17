"""Pure-Python ISO/IEC 18004 QR Code encoder (Byte mode, ECC-L) generating SVG and PNG data URIs."""

from __future__ import annotations

import base64
import struct
import zlib
from typing import Any

# (version, total_cw, ec_per_block, g1_blocks, g1_data, g2_blocks, g2_data, align_centers)
_VERSION_SPECS: tuple[tuple[int, int, int, int, int, int, int, tuple[int, ...]], ...] = (
    (1, 26, 7, 1, 19, 0, 0, ()),
    (2, 44, 10, 1, 34, 0, 0, (6, 18)),
    (3, 70, 15, 1, 55, 0, 0, (6, 22)),
    (4, 100, 20, 1, 80, 0, 0, (6, 26)),
    (5, 134, 26, 1, 108, 0, 0, (6, 30)),
    (6, 172, 18, 2, 68, 0, 0, (6, 34)),
    (7, 196, 20, 2, 78, 0, 0, (6, 22, 38)),
    (8, 242, 24, 2, 97, 0, 0, (6, 24, 42)),
    (9, 292, 30, 2, 116, 0, 0, (6, 26, 46)),
    (10, 346, 18, 2, 68, 2, 69, (6, 28, 50)),
    (11, 404, 20, 4, 81, 0, 0, (6, 30, 54)),
    (12, 466, 24, 2, 92, 2, 93, (6, 32, 58)),
    (13, 532, 26, 4, 107, 0, 0, (6, 34, 62)),
    (14, 581, 30, 3, 115, 1, 116, (6, 26, 46, 66)),
    (15, 655, 22, 5, 87, 1, 88, (6, 26, 48, 70)),
)


def _init_gf() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    val = 1
    for i in range(255):
        exp[i] = val
        log[val] = i
        val <<= 1
        if val & 0x100:
            val ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_remainder(data: list[int], ec_len: int) -> list[int]:
    gen = [1]
    for i in range(ec_len):
        nxt = [0] * (len(gen) + 1)
        factor = _GF_EXP[i]
        for j, coef in enumerate(gen):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, factor)
        gen = nxt
    rem = [0] * ec_len
    for byte in data:
        lead = byte ^ rem.pop(0)
        rem.append(0)
        if lead:
            for idx in range(ec_len):
                rem[idx] ^= _gf_mul(gen[idx + 1], lead)
    return rem


def _format_bits(mask: int) -> int:
    data = (0b01 << 3) | mask  # ECC L = 01
    rem = data << 10
    for shift in range(14, 9, -1):
        if rem & (1 << shift):
            rem ^= 0x537 << (shift - 10)
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(version: int) -> int:
    rem = version << 12
    for shift in range(17, 11, -1):
        if rem & (1 << shift):
            rem ^= 0x1F25 << (shift - 12)
    return (version << 12) | rem


def _select_spec(byte_len: int) -> tuple[int, int, int, int, int, int, int, tuple[int, ...]]:
    for spec in _VERSION_SPECS:
        ver, _, _, g1_b, g1_d, g2_b, g2_d, _ = spec
        data_cap = g1_b * g1_d + g2_b * g2_d
        header_bits = 4 + (8 if ver < 10 else 16)
        if header_bits + byte_len * 8 <= data_cap * 8:
            return spec
    raise ValueError(f"QR payload too long ({byte_len} bytes; max 520)")


def _build_codewords(payload: bytes, spec: tuple[int, int, int, int, int, int, int, tuple[int, ...]]) -> list[int]:
    ver, _, ec_len, g1_b, g1_d, g2_b, g2_d, _ = spec
    data_cw = g1_b * g1_d + g2_b * g2_d
    bits: list[int] = []

    def push(val: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((val >> shift) & 1)

    push(0b0100, 4)  # Byte mode
    push(len(payload), 8 if ver < 10 else 16)
    for b in payload:
        push(b, 8)

    cap_bits = data_cw * 8
    term = min(4, cap_bits - len(bits))
    bits.extend([0] * term)
    while len(bits) % 8:
        bits.append(0)
    pad_bytes = (0xEC, 0x11)
    pad_idx = 0
    while len(bits) < cap_bits:
        push(pad_bytes[pad_idx & 1], 8)
        pad_idx += 1

    raw = [sum(bits[i + b] << (7 - b) for b in range(8)) for i in range(0, len(bits), 8)]
    blocks: list[list[int]] = []
    ecs: list[list[int]] = []
    pos = 0
    for count, size in ((g1_b, g1_d), (g2_b, g2_d)):
        for _ in range(count):
            blk = raw[pos : pos + size]
            pos += size
            blocks.append(blk)
            ecs.append(_rs_remainder(blk, ec_len))

    out: list[int] = []
    for col in range(max(g1_d, g2_d)):
        for blk in blocks:
            if col < len(blk):
                out.append(blk[col])
    for col in range(ec_len):
        for ec in ecs:
            out.append(ec[col])
    return out


def _place_function_patterns(
    ver: int, size: int, align_centers: tuple[int, ...]
) -> tuple[list[list[bool]], list[list[bool]]]:
    grid = [[False] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def set_mod(r: int, c: int, dark: bool) -> None:
        if 0 <= r < size and 0 <= c < size:
            grid[r][c] = dark
            func[r][c] = True

    def place_finder(top: int, left: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = top + dr, left + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                is_dark = (
                    0 <= dr <= 6
                    and 0 <= dc <= 6
                    and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
                )
                set_mod(r, c, is_dark)

    place_finder(0, 0)
    place_finder(0, size - 7)
    place_finder(size - 7, 0)

    for i in range(8, size - 8):
        set_mod(6, i, i % 2 == 0)
        set_mod(i, 6, i % 2 == 0)

    for cy in align_centers:
        for cx in align_centers:
            if (cy <= 8 and cx <= 8) or (cy <= 8 and cx >= size - 9) or (cy >= size - 9 and cx <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    set_mod(cy + dr, cx + dc, abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0))

    # Format reservation + dark module
    for i in range(9):
        if i != 6:
            func[8][i] = True
            func[i][8] = True
    for i in range(8):
        func[8][size - 1 - i] = True
        func[size - 1 - i][8] = True
    set_mod(size - 8, 8, True)

    if ver >= 7:
        vbits = _version_bits(ver)
        for i in range(18):
            bit = bool((vbits >> i) & 1)
            a, b = i // 3, size - 11 + (i % 3)
            set_mod(a, b, bit)
            set_mod(b, a, bit)
    return grid, func


def _apply_mask_and_format(
    base_grid: list[list[bool]], func: list[list[bool]], data_bits: list[int]
) -> list[list[bool]]:
    size = len(base_grid)
    placed = [row[:] for row in base_grid]
    bit_idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for dc in (0, -1):
                c = col + dc
                if not func[row][c]:
                    placed[row][c] = bool(data_bits[bit_idx]) if bit_idx < len(data_bits) else False
                    bit_idx += 1
        col -= 2
        upward = not upward

    best_grid: list[list[bool]] | None = None
    best_score = 10**9
    for mask_id in range(8):
        cand = [row[:] for row in placed]
        for r in range(size):
            for c in range(size):
                if not func[r][c] and _mask_hit(mask_id, r, c):
                    cand[r][c] = not cand[r][c]
        fmt = _format_bits(mask_id)
        coords_a = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8), (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
        coords_b = [(size - 1 - i, 8) for i in range(7)] + [(8, size - 8 + i) for i in range(8)]
        for idx in range(15):
            bit = bool((fmt >> (14 - idx)) & 1)
            ra, ca = coords_a[idx]
            rb, cb = coords_b[idx]
            cand[ra][ca] = bit
            cand[rb][cb] = bit
        score = _penalty_score(cand)
        if score < best_score:
            best_score = score
            best_grid = cand
    assert best_grid is not None
    return best_grid


def _mask_hit(mask_id: int, r: int, c: int) -> bool:
    if mask_id == 0:
        return (r + c) % 2 == 0
    if mask_id == 1:
        return r % 2 == 0
    if mask_id == 2:
        return c % 3 == 0
    if mask_id == 3:
        return (r + c) % 3 == 0
    if mask_id == 4:
        return ((r // 2) + (c // 3)) % 2 == 0
    if mask_id == 5:
        return ((r * c) % 2 + (r * c) % 3) == 0
    if mask_id == 6:
        return (((r * c) % 2 + (r * c) % 3) % 2) == 0
    return (((r + c) % 2 + (r * c) % 3) % 2) == 0


def _penalty_score(grid: list[list[bool]]) -> int:
    n = len(grid)
    score = 0
    for axis in (grid, [[grid[r][c] for r in range(n)] for c in range(n)]):
        for line in axis:
            run = 1
            for i in range(1, n):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run = 1
            if run >= 5:
                score += 3 + (run - 5)
    for r in range(n - 1):
        for c in range(n - 1):
            v = grid[r][c]
            if grid[r][c + 1] == v and grid[r + 1][c] == v and grid[r + 1][c + 1] == v:
                score += 3
    dark = sum(1 for row in grid for cell in row if cell)
    pct = abs((dark * 100) // (n * n) - 50) // 5
    return score + pct * 10


def build_qr_matrix(text: str) -> tuple[int, list[list[bool]]]:
    """Encode UTF-8 text into a boolean QR matrix (True = dark module)."""
    payload = text.encode("utf-8")
    spec = _select_spec(len(payload))
    ver = spec[0]
    size = 17 + 4 * ver
    codewords = _build_codewords(payload, spec)
    bits: list[int] = []
    for cw in codewords:
        for shift in range(7, -1, -1):
            bits.append((cw >> shift) & 1)
    base, func = _place_function_patterns(ver, size, spec[7])
    return ver, _apply_mask_and_format(base, func, bits)


def render_svg(matrix: list[list[bool]], *, scale: int = 6, border: int = 4) -> str:
    """Render a QR matrix as standalone SVG markup."""
    n = len(matrix)
    dim = (n + border * 2) * scale
    rects: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" width="{dim}" height="{dim}" shape-rendering="crispEdges">',
        f'<rect width="{dim}" height="{dim}" fill="#ffffff"/>',
    ]
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                x, y = (c + border) * scale, (r + border) * scale
                rects.append(f'<rect x="{x}" y="{y}" width="{scale}" height="{scale}" fill="#181718"/>')
    rects.append("</svg>")
    return "".join(rects)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def render_png_bytes(matrix: list[list[bool]], *, scale: int = 6, border: int = 4) -> bytes:
    """Render a QR matrix as a valid 8-bit grayscale PNG image."""
    n = len(matrix)
    dim = (n + border * 2) * scale
    white_row = bytes([255] * dim)
    raw_rows = bytearray()
    for _ in range(border * scale):
        raw_rows.append(0)
        raw_rows.extend(white_row)
    for row in matrix:
        px = bytearray([255] * (border * scale))
        for dark in row:
            px.extend(([0] if dark else [255]) * scale)
        px.extend([255] * (border * scale))
        for _ in range(scale):
            raw_rows.append(0)
            raw_rows.extend(px)
    for _ in range(border * scale):
        raw_rows.append(0)
        raw_rows.extend(white_row)

    ihdr = struct.pack(">IIBBBBB", dim, dim, 8, 0, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=6)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def render_ascii(matrix: list[list[bool]], *, border: int = 1) -> str:
    """Render a compact ASCII/UTF-8 QR preview."""
    n = len(matrix)
    pad = [False] * (n + border * 2)
    padded = [pad[:] for _ in range(border)]
    for row in matrix:
        padded.append([False] * border + row[:] + [False] * border)
    padded.extend([pad[:] for _ in range(border)])
    return "\n".join("".join("##" if cell else "  " for cell in row) for row in padded)


def encode_qr_payload(url: str) -> dict[str, Any]:
    """Generate QR matrix, SVG, PNG base64/data URI, and ASCII preview for `url`."""
    version, matrix = build_qr_matrix(url)
    svg = render_svg(matrix)
    png_bytes = render_png_bytes(matrix)
    svg_b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    png_b64 = base64.b64encode(png_bytes).decode("ascii")
    return {
        "qr_version": version,
        "qr_modules": len(matrix),
        "qr_svg": svg,
        "qr_svg_data_uri": f"data:image/svg+xml;base64,{svg_b64}",
        "qr_png_base64": png_b64,
        "qr_png_data_uri": f"data:image/png;base64,{png_b64}",
        "qr_data_uri": f"data:image/png;base64,{png_b64}",
        "qr_ascii": render_ascii(matrix),
    }
