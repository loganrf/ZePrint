"""
Auto-fitted text blocks rendered as rasters.

Native ZPL fonts can't be measured ahead of time, so text that must *fit* (an
address of unknown length) is drawn with a real TrueType face at the largest
size that fits its box, then placed with ``ZPL.image``. What you preview is
exactly what prints.
"""

from __future__ import annotations

import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont


@lru_cache(maxsize=2)
def _font_path(bold: bool) -> str | None:
    try:
        import matplotlib
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        path = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf", name)
        return path if os.path.exists(path) else None
    except Exception:
        return None


@lru_cache(maxsize=256)
def font(px: int, bold: bool = False):
    path = _font_path(bold)
    return ImageFont.truetype(path, max(4, px)) if path else ImageFont.load_default()


def split_lines(text: str, max_lines: int = 8) -> list[str]:
    """One line per row; a single-line value may use ``|`` as the row separator."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = text.split("\n") if "\n" in text else text.split("|")
    return [p.strip() for p in parts if p.strip()][:max_lines]


def _measure(lines, px, bold_first, spacing):
    widths, heights = [], []
    for i, line in enumerate(lines):
        f = font(px, bold_first and i == 0)
        widths.append(f.getlength(line))
        ascent, descent = f.getmetrics()
        heights.append(ascent + descent)
    gap = int(px * spacing)
    return max(widths), sum(heights) + gap * (len(lines) - 1), heights, gap


def text_block(lines: list[str], box_w: int, box_h: int, max_px: int, min_px: int,
               bold_first: bool = True, spacing: float = 0.12, align: str = "left") -> Image.Image:
    """
    Render ``lines`` (black on white, grayscale) at the largest font size in
    [min_px, max_px] device pixels that fits ``box_w`` x ``box_h``. If even
    ``min_px`` is too wide, the block is condensed horizontally to fit.
    """
    lines = [ln for ln in lines if ln]
    if not lines or box_w < 8 or box_h < 8:
        return Image.new("L", (8, 1), 255)
    min_px = max(4, min(min_px, max_px))
    lo, hi, best = min_px, max_px, min_px
    while lo <= hi:                          # largest size that fits both ways
        mid = (lo + hi) // 2
        w, h, _, _ = _measure(lines, mid, bold_first, spacing)
        if w <= box_w and h <= box_h:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    w, h, heights, gap = _measure(lines, best, bold_first, spacing)
    while h > box_h and best > 4:            # too many lines even at min size
        best -= 1
        w, h, heights, gap = _measure(lines, best, bold_first, spacing)

    img = Image.new("L", (max(8, int(w) + 2), max(1, h)), 255)
    d = ImageDraw.Draw(img)
    y = 0
    for i, line in enumerate(lines):
        f = font(best, bold_first and i == 0)
        lw = f.getlength(line)
        x = (w - lw) / 2 if align == "center" else (w - lw if align == "right" else 0)
        d.text((x, y), line, fill=0, font=f)
        y += heights[i] + gap
    if img.width > box_w:                    # condense rather than clip
        img = img.resize((box_w, img.height), Image.LANCZOS)
    return img
