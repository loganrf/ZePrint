"""
Render ZPL to a PNG locally - no printer, no network.

Implements the subset ZePrint's labels emit (^FO, ^A0, ^FB, ^FH/^FD, ^GB, ^GFA,
^BQ, ^BC/^BY, ^PW/^LL, ^LH). It is a faithful-enough proof of the layout, not a
pixel-exact printer emulator: font 0 is approximated with DejaVu Sans and Code
128 barcodes are drawn as placeholders of the right footprint.
"""

from __future__ import annotations

import io
import os
import re
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .raster import decompress_hex

_CMD = re.compile(r"[\^~]([A-Za-z@][A-Za-z0-9@]?)")

# Bounds for untrusted ZPL (the preview endpoint renders whatever it is sent):
MAX_SIDE = 8000                # dots per side (26.7 in at 300 dpi)
MAX_FONT = 2000
MAX_TEXT = 2000                # characters drawn per field


@lru_cache(maxsize=1)
def _font_file() -> str | None:
    try:
        import matplotlib
        path = os.path.join(os.path.dirname(matplotlib.__file__),
                            "mpl-data", "fonts", "ttf", "DejaVuSans.ttf")
        return path if os.path.exists(path) else None
    except Exception:
        return None


@lru_cache(maxsize=64)
def _font(h: int):
    path = _font_file()
    if path:
        return ImageFont.truetype(path, max(6, int(h * 0.82)))
    return ImageFont.load_default()


def _tokens(zpl: str):
    """Yield (command, argument) pairs; ^FD/^GF data runs to the next ^FS."""
    i = 0
    n = len(zpl)
    while i < n:
        m = _CMD.search(zpl, i)
        if not m:
            return
        code = m.group(1).upper()
        start = m.end()
        if code in ("FD", "FV") or code == "GF":
            end = zpl.find("^FS", start)
            end = n if end < 0 else end
        else:
            nxt = _CMD.search(zpl, start)
            end = nxt.start() if nxt else n
        yield code, zpl[start:end]
        i = end


def _unescape(text: str, indicator: str) -> str:
    def sub(m):
        try:
            return bytes.fromhex(m.group(1)).decode("utf-8", "replace")
        except ValueError:
            return m.group(0)
    return re.sub(re.escape(indicator) + r"([0-9A-Fa-f]{2})", sub, text)


def _ints(arg: str, count: int, defaults: list[int]) -> list[int]:
    out = []
    parts = arg.split(",")
    for i in range(count):
        try:
            out.append(int(float(parts[i])))
        except (IndexError, ValueError):
            out.append(defaults[i])
    return out


def render(zpl: str, default_size: tuple[int, int] = (1200, 1800)) -> Image.Image:
    """Render the first label in ``zpl`` to a grayscale PIL image."""
    mpw = re.search(r"\^PW(\d+)", zpl)
    mll = re.search(r"\^LL(\d+)", zpl)
    cw = int(mpw.group(1)) if mpw else default_size[0]
    ch = int(mll.group(1)) if mll else default_size[1]
    cw, ch = max(1, min(cw, MAX_SIDE)), max(1, min(ch, MAX_SIDE))
    canvas = Image.new("L", (cw, ch), 255)
    d = ImageDraw.Draw(canvas)

    lhx = lhy = 0
    x = y = 0
    font_h, font_w = 30, 30
    fb = None                  # (width, lines, spacing, align)
    fh = None                  # ^FH indicator for the current field
    pending = None             # ("qr", mag) | ("bc", height, interp)
    by_module, by_height = 2, 10

    def reset_field():
        nonlocal fb, fh, pending
        fb = fh = pending = None

    for code, arg in _tokens(zpl):
        if code == "XZ":
            break
        if code == "LH":
            lhx, lhy = _ints(arg, 2, [0, 0])
        elif code in ("FO", "FT"):
            fx, fy = _ints(arg, 2, [0, 0])
            x, y = lhx + fx, lhy + fy
        elif code.startswith("A") and code != "A@":
            # ^A0N,h,w  (font name is the char after ^A)
            parts = (code[1:] + arg).split(",")
            h = _ints(",".join(parts[1:]), 2, [font_h, 0])
            font_h = max(1, min(h[0], MAX_FONT, ch))
            font_w = h[1] or font_h
        elif code == "CF":
            # ^CFf,h,w - font name, then height/width
            h = _ints(",".join(arg.split(",")[1:]), 2, [font_h, 0])
            font_h = max(1, min(h[0], MAX_FONT, ch))
            font_w = h[1] or font_h
        elif code == "FB":
            p = arg.split(",")
            fb = (_ints(p[0], 1, [0])[0], _ints(p[1] if len(p) > 1 else "", 1, [1])[0],
                  0, (p[3].strip().upper() if len(p) > 3 and p[3].strip() else "L"))
        elif code == "FH":
            fh = arg.strip()[:1] or "_"
        elif code == "BY":
            by_module, _ratio, by_height = _ints(arg, 3, [by_module, 3, by_height])
            by_module = max(1, min(by_module, 10))
            by_height = max(1, min(by_height, ch))
        elif code == "BQ":
            p = arg.split(",")
            mag = _ints(p[2] if len(p) > 2 else "", 1, [2])[0]
            pending = ("qr", max(1, min(mag, 10)))
        elif code == "BC":
            p = arg.split(",")
            h = _ints(p[1] if len(p) > 1 else "", 1, [by_height])[0] or by_height
            h = max(1, min(h, ch))
            interp = (p[2].strip().upper() != "N") if len(p) > 2 and p[2].strip() else True
            pending = ("bc", h, interp)
        elif code == "GB":
            w, h, t = _ints(arg, 3, [1, 1, 1])
            t = max(1, t)
            w, h = max(w, t), max(h, t)
            if t * 2 >= min(w, h):
                d.rectangle([x, y, x + w - 1, y + h - 1], fill=0)
            else:
                d.rectangle([x, y, x + w - 1, y + h - 1], outline=0, width=t)
        elif code == "GF":
            parts = arg.split(",", 4)
            if len(parts) == 5 and parts[0].strip().upper() == "A":
                try:
                    total, rb = int(parts[1]), int(parts[3])
                    if not 0 < rb <= (MAX_SIDE + 7) // 8:
                        raise ValueError("row width out of range")
                    # rows past the label are invisible; never decode more than a label's worth
                    total = min(total, rb * (ch - y), rb * MAX_SIDE) if y < ch else 0
                    if total < rb:
                        raise ValueError("nothing visible")
                    total -= total % rb
                    raw = decompress_hex(parts[4], rb, total)
                    img = Image.frombytes("1", (rb * 8, total // rb),
                                          bytes(b ^ 0xFF for b in raw)).convert("L")
                    canvas.paste(img, (x, y))
                except (ValueError, ZeroDivisionError):
                    d.rectangle([x, y, x + 40, y + 40], outline=0)
        elif code == "FD" or code == "FV":
            text = (_unescape(arg, fh) if fh else arg)[:MAX_TEXT]
            if pending and pending[0] == "qr":
                _draw_qr(canvas, d, x, y, text, pending[1])
            elif pending and pending[0] == "bc":
                _draw_code128(d, x, y, text, by_module, pending[1], pending[2])
            else:
                _draw_text(d, x, y, text, font_h, fb)
        elif code == "FS":
            reset_field()
    return canvas


def _fit(text: str, avail: float, h: int) -> str:
    """Trim text that can't fit anyway (no glyph is narrower than ~0.15 h), so a
    hostile field can't make Pillow allocate a bitmap far wider than the label."""
    return text[:max(1, int(avail / (0.15 * h)) + 1)] if avail > 0 else ""


def _draw_text(d: ImageDraw.ImageDraw, x: int, y: int, text: str, h: int, fb) -> None:
    W, H = d.im.size
    if x >= W or y >= H:
        return
    f = _font(h)
    if not fb or fb[0] <= 0:
        d.text((x, y), _fit(text, W - x, h), fill=0, font=f)
        return
    width, max_lines, _, align = fb
    width = min(width, W)
    lines, line = [], ""
    for word in text.split(" "):
        trial = (line + " " + word).strip()
        if line and d.textlength(trial, font=f) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    if max_lines >= 1 and len(lines) > max_lines:      # ZPL overprints the last line
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    yy = y
    for ln in lines:
        if yy >= H:
            break
        ln = _fit(ln, width, h)
        tw = d.textlength(ln, font=f)
        xx = x + (width - tw if align == "R" else (width - tw) / 2 if align == "C" else 0)
        d.text((xx, yy), ln, fill=0, font=f)
        yy += int(h * 1.1)


def _draw_qr(canvas: Image.Image, d: ImageDraw.ImageDraw, x: int, y: int, payload: str, mag: int):
    ec = payload[:1].upper() if payload[:1].upper() in "LMQH" else "M"
    data = payload.split(",", 1)[1] if "," in payload[:3] else payload
    try:
        import qrcode
        level = {"L": qrcode.constants.ERROR_CORRECT_L, "M": qrcode.constants.ERROR_CORRECT_M,
                 "Q": qrcode.constants.ERROR_CORRECT_Q, "H": qrcode.constants.ERROR_CORRECT_H}[ec]
        q = qrcode.QRCode(border=0, error_correction=level, box_size=1)
        q.add_data(data)
        q.make(fit=True)
        img = q.make_image(fill_color="black", back_color="white").get_image().convert("L")
        side = q.modules_count * mag
        canvas.paste(img.resize((side, side), Image.NEAREST), (x, y))
    except Exception:
        d.rectangle([x, y, x + 25 * mag, y + 25 * mag], outline=0, width=3)
        d.text((x + 4, y + 4), "QR", fill=0, font=_font(max(12, 6 * mag)))


def _draw_code128(d: ImageDraw.ImageDraw, x: int, y: int, data: str, module: int, h: int,
                  interp: bool) -> None:
    """Placeholder bars with Code 128's footprint (11 modules/char + start/check/stop)."""
    import random
    modules = 11 * (len(data) + 3) + 2
    rnd = random.Random(data)
    xx = x
    right = d.im.size[0]
    for i in range(modules):
        if xx >= right:
            break
        if i < 2 or i >= modules - 2 or rnd.random() < 0.5:
            d.rectangle([xx, y, xx + module - 1, y + h - 1], fill=0)
        xx += module
    if interp:
        fh = max(12, int(h * 0.22))
        f = _font(fh)
        data = _fit(data, right - x, fh)
        tw = d.textlength(data, font=f)
        d.text((x + (modules * module - tw) / 2, y + h + 4), data, fill=0, font=f)


def render_png(zpl: str, default_size: tuple[int, int] = (1200, 1800)) -> bytes:
    buf = io.BytesIO()
    render(zpl, default_size).save(buf, format="PNG", optimize=True)
    return buf.getvalue()
