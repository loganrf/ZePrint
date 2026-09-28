"""
1-bit raster encoding for ``^GFA`` graphic fields.

Replaces the ``zebra_image.py`` helper the original scripts imported. Images are
thresholded to black/white, packed 8 dots per byte (MSB first, 1 = black), and
written in Zebra's ASCII compression scheme, one row per line:

    G..Y      repeat the next hex digit 1..19 times
    g..z      repeat the next hex digit 20..400 times (steps of 20); combinable
    ,         fill the rest of the row with 0 (white)
    !         fill the rest of the row with F (black)
    :         repeat the previous row
"""

from __future__ import annotations

import numpy as np

_HI = "GHIJKLMNOPQRSTUVWXY"      # 1..19
_LO = "ghijklmnopqrstuvwxyz"     # 20, 40, .. 400


def encode_image(img, threshold: int = 128) -> tuple[bytes, int, int]:
    """
    Threshold a PIL image and pack it for ``^GFA``.

    The width is padded with white up to a whole number of bytes, so nothing is
    resampled and the rightmost columns aren't lost - text and shipping labels
    are often drawn right up to the image's edge. Returns
    ``(packed, row_bytes, rows)``.
    """
    gray = np.asarray(img.convert("L"))
    if gray.shape[1] == 0 or gray.shape[0] == 0:
        raise ValueError("image is too small to print")
    black = gray < threshold
    packed = np.packbits(black, axis=1)          # pads the last byte with 0 (white)
    return packed.tobytes(), packed.shape[1], packed.shape[0]


def pack_bits(bits, width: int, height: int) -> tuple[bytes, int]:
    """Compatibility shim for the original helper: 0/1 per dot -> (packed, row_bytes)."""
    arr = np.frombuffer(bytes(bits), dtype=np.uint8).reshape(height, width).astype(bool)
    packed = np.packbits(arr, axis=1)
    return packed.tobytes(), packed.shape[1]


def _repeat(n: int) -> str:
    """Repeat-count prefix for a run of n identical hex digits (n >= 1)."""
    out = []
    while n >= 400:
        out.append("z")
        n -= 400
    if n >= 20:
        out.append(_LO[n // 20 - 1])
        n %= 20
    if n >= 1 and (n > 1 or out):
        out.append(_HI[n - 1])
    return "".join(out)


def _encode_row(row: str) -> str:
    body, tail = row, ""
    stripped = row.rstrip("0")
    if len(stripped) < len(row):
        body, tail = stripped, ","
    else:
        stripped = row.rstrip("F")
        if len(stripped) < len(row):
            body, tail = stripped, "!"
    out = []
    i = 0
    while i < len(body):
        j = i
        while j < len(body) and body[j] == body[i]:
            j += 1
        out.append(_repeat(j - i) + body[i])
        i = j
    return "".join(out) + tail


def compress_hex(data: bytes, row_bytes: int) -> str:
    """Zebra ASCII-compressed hex for packed raster ``data`` (newline per row)."""
    lines = []
    prev = None
    for i in range(0, len(data), row_bytes):
        row = data[i:i + row_bytes].hex().upper()
        if row == prev:
            lines.append(":")
            continue
        prev = row
        lines.append(_encode_row(row))
    return "\n".join(lines)


def decompress_hex(data: str, row_bytes: int, total: int) -> bytes:
    """
    Inverse of :func:`compress_hex`; also accepts plain or unwrapped hex.
    Used by the preview renderer. Output is bounded by ``total`` bytes no matter
    what repeat counts the input claims.
    """
    if row_bytes <= 0 or total <= 0:
        return b""
    nib = row_bytes * 2
    max_rows = -(-total // row_bytes)
    rows: list[str] = []
    prev = "0" * nib
    cur = ""
    n = 0
    for c in data:
        if len(rows) >= max_rows:
            break
        if c.isspace():
            continue
        if c in _HI:
            n += _HI.index(c) + 1
            continue
        if c in _LO:
            n += (_LO.index(c) + 1) * 20
            continue
        if c == ":":
            if not cur:
                rows.append(prev)
            n = 0
            continue
        if c == ",":
            cur += "0" * (nib - len(cur))
        elif c == "!":
            cur += "F" * (nib - len(cur))
        else:
            budget = (max_rows - len(rows)) * nib - len(cur)
            cur += c * min(n or 1, budget)
        n = 0
        while len(cur) >= nib:
            prev = cur[:nib]
            rows.append(prev)
            cur = cur[nib:]
    if cur and len(rows) < max_rows:
        rows.append(cur.ljust(nib, "0"))
    raw = bytes.fromhex("".join(rows[:max_rows]))
    return raw[:total].ljust(total, b"\x00")
