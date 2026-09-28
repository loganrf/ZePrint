"""
Print an image or PDF - typically a carrier's shipping label (UPS, FedEx, USPS,
Pirate Ship...), but any PNG/JPG/GIF/BMP/TIFF/WebP/PDF works.

The art is trimmed of white margins (which isolates a 4x6 label on a letter-size
page), turned to match the label's orientation, scaled to fit, and thresholded
to crisp black and white so barcodes scan. Multi-page PDFs print one label per
page. For photos or gray artwork, turn on dithering.
"""

from __future__ import annotations

import io
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from ..errors import LabelError
from . import Label, RenderContext, RenderResult, register

MAX_PAGES = 50
MAX_RENDER_SIDE = 8000                   # px, per rendered PDF page
NAMED_CROPS = {                          # where carriers put a 4x6 on a letter-size page
    "top": "0,0,1,0.5", "bottom": "0,0.5,1,1", "left": "0,0,0.5,1", "right": "0.5,0,1,1",
    "top-left": "0,0,0.5,0.5", "top-right": "0.5,0,1,0.5",
    "bottom-left": "0,0.5,0.5,1", "bottom-right": "0.5,0.5,1,1",
}


class ImageParams(BaseModel):
    image: str = Field("", title="Image or PDF",
                       description="Upload a file here, or give an http(s) URL",
                       json_schema_extra={"format": "zeprint-file"})
    pages: str = Field("1", title="PDF pages",
                       description="1, 2-3, 1,3 or all - one label per page")
    rotate: Literal["auto", "0", "90", "180", "270"] = Field(
        "auto", description="Clockwise degrees; auto turns art to match the label's orientation")
    trim: bool = Field(True, title="Trim margins",
                       description="Crop away white space first (finds the label on a full page)")
    crop: Optional[str] = Field(None, title="Crop",
                                description="Keep part of the page first: top, bottom, left, "
                                            "right, top-left... or fractions left,top,right,"
                                            "bottom (0,0,1,0.5 = top half)")
    fit: Literal["contain", "stretch"] = Field("contain", description="Keep the aspect ratio, "
                                                                      "or fill the label")
    dither: bool = Field(False, description="For photos and grays; leave off for barcodes/text")
    threshold: int = Field(150, ge=1, le=254,
                           description="Gray level below which a dot prints black")
    margin: float = Field(0.03, ge=0, le=0.5, title="Margin (in)")

    @field_validator("pages")
    @classmethod
    def _pages(cls, v: str) -> str:
        v = v.strip().lower().replace(" ", "")
        if v != "all" and not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", v):
            raise ValueError("use a page number, a range like 2-3, a list like 1,3, or all")
        return v

    @field_validator("crop")
    @classmethod
    def _crop(cls, v):
        if v is None or not v.strip():
            return None
        v = NAMED_CROPS.get(v.strip().lower().replace(" ", "-"), v)
        try:
            box = [float(x) for x in v.split(",")]
        except ValueError:
            raise ValueError("crop is top/bottom/left/right/top-left/... or four fractions "
                             "left,top,right,bottom") from None
        if len(box) != 4 or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
            raise ValueError("crop is four fractions 0-1 with left<right and top<bottom")
        return ",".join(f"{x:g}" for x in box)


def page_numbers(spec: str, count: int) -> list[int]:
    """1-based spec -> 0-based page indexes, validated against ``count``."""
    if spec == "all":
        idx = list(range(count))
    else:
        idx = []
        for part in spec.split(","):
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b or a)
            if lo < 1 or hi < lo:
                raise LabelError(f"bad page range {part!r}")
            idx.extend(range(lo - 1, hi))
    bad = [i + 1 for i in idx if i >= count]
    if bad:
        raise LabelError(f"page {bad[0]} doesn't exist (the document has {count})")
    if len(idx) > MAX_PAGES:
        raise LabelError(f"at most {MAX_PAGES} pages per print")
    return idx


def load_pages(data: bytes, spec: str, dpi: int) -> tuple[list, int]:
    """Decode the file into grayscale PIL pages. Returns (pages, total page count)."""
    from PIL import Image, ImageSequence

    if data[:4] == b"%PDF":
        import pypdfium2 as pdfium
        try:
            pdf = pdfium.PdfDocument(data)
        except Exception as e:
            raise LabelError(f"that PDF can't be read ({e})") from None
        try:
            total = len(pdf)
            pages = []
            for i in page_numbers(spec, total):
                page = pdf[i]
                w_pt, h_pt = page.get_size()
                scale = min(dpi / 72, MAX_RENDER_SIDE / max(w_pt, h_pt, 1))
                pages.append(page.render(scale=scale, grayscale=True).to_pil().convert("L"))
            return pages, total
        finally:
            pdf.close()
    try:
        img = Image.open(io.BytesIO(data))
        frames = [f.copy() for f in ImageSequence.Iterator(img)]
    except Image.DecompressionBombError:
        raise LabelError("that image is too large") from None
    except Exception as e:
        raise LabelError(f"that file isn't a readable image or PDF ({e})") from None
    return [frames[i] for i in page_numbers(spec, len(frames))], len(frames)


def prepare(img, box_w: int, box_h: int, p: ImageParams):
    """Grayscale page -> black/white image fitted to box_w x box_h device dots."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(white, rgba)
    img = img.convert("L")

    if p.crop:
        l, t, r, b = (float(x) for x in p.crop.split(","))
        img = img.crop((round(l * img.width), round(t * img.height),
                        round(r * img.width), round(b * img.height)))
    if p.trim:
        ink = img.point(lambda v: 255 if v < 245 else 0)
        bbox = ink.getbbox()
        if bbox is None:
            raise LabelError("the page is blank")
        pad = max(2, round(min(img.size) * 0.004))
        img = img.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                        min(img.width, bbox[2] + pad), min(img.height, bbox[3] + pad)))

    if p.rotate == "auto":
        art_wide, box_wide = img.width > img.height * 1.05, box_w > box_h * 1.05
        art_tall, box_tall = img.height > img.width * 1.05, box_h > box_w * 1.05
        if (art_wide and box_tall) or (art_tall and box_wide):
            img = img.rotate(-90, expand=True, fillcolor=255)
    elif p.rotate != "0":
        img = img.rotate(-int(p.rotate), expand=True, fillcolor=255)

    if p.fit == "stretch":
        size = (box_w, box_h)
    else:
        k = min(box_w / img.width, box_h / img.height)
        size = (max(1, round(img.width * k)), max(1, round(img.height * k)))
    # upscaling by 2x+ (e.g. a 203 dpi label on a 600 dpi head): nearest keeps bars square
    k = size[0] / img.width
    img = img.resize(size, Image.NEAREST if k >= 2 and not p.dither else Image.LANCZOS)

    if p.dither:
        return img.convert("1").convert("L")      # Floyd-Steinberg
    return img.point(lambda v: 0 if v < p.threshold else 255)


@register
class ImageLabel(Label):
    id = "image"
    name = "Image / shipping label"
    description = ("Print a PNG, JPG or PDF - e.g. a UPS/FedEx/USPS shipping label - "
                   "trimmed, rotated and scaled to the label.")
    icon = "mdi:file-image-outline"
    Params = ImageParams
    fetch_outside_lock = True

    def render(self, p: ImageParams, ctx: RenderContext) -> RenderResult:
        if not p.image.strip():
            raise LabelError("choose an image or PDF to print")
        if ctx.open_file is None:
            raise LabelError("file access isn't available here")
        data, meta = ctx.open_file(p.image.strip())      # network / disk: outside the lock

        zpl = []
        with ctx.drawing():                               # pdfium isn't thread-safe
            pages, total = load_pages(data, p.pages, ctx.dpi)
            for page in pages:
                z = ctx.zpl()
                m = p.margin * 300                        # inches -> design units
                box_w, box_h = ctx.dots(z.W - 2 * m), ctx.dots(z.H - 2 * m)
                if box_w < 8 or box_h < 8:
                    raise LabelError("the margin leaves no room on this label size")
                img = prepare(page, box_w, box_h, p)
                h = z.design(img.height)
                z.image(None, (z.H - h) / 2, img, threshold=128)
                zpl.append(z.build())
        return RenderResult("".join(zpl), title=f"image-{meta.get('filename') or 'file'}",
                            data={"labels": len(zpl), "page_count": total,
                                  "source": meta.get("filename") or p.image,
                                  "content_type": meta.get("content_type")})
