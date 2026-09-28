"""
Address labels from plain text.

4x6: return address and recipient on one label (or either alone), plus an
     optional banner (FRAGILE, PRIORITY...), reference line and Code 128.
2x1: one address per label; "both" prints two labels - the return address,
     then the recipient - in one job.

Addresses are one line per row (or rows separated by ``|``). Save your return
address as this label's default once, and only the recipient changes per print.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..errors import LabelError
from . import Label, RenderContext, RenderResult, register
from ._text import split_lines, text_block


class AddressParams(BaseModel):
    to: str = Field("", max_length=400, title="To",
                    description="Recipient, one line per row (or rows separated by |)",
                    json_schema_extra={"format": "textarea"})
    sender: str = Field("", max_length=400, title="From (return address)",
                        description="Save it as a default once",
                        json_schema_extra={"format": "textarea"})
    include: Literal["both", "to", "from"] = Field(
        "both", title="Print",
        description="4x6: both on one label. 2x1: one address per label; both = two labels")
    note: str = Field("", max_length=32, title="Banner",
                      description="Optional big line on 4x6, e.g. FRAGILE or PRIORITY")
    reference: str = Field("", max_length=64, title="Reference",
                           description="Optional small line at the bottom of 4x6 (order #, contents)")
    barcode: str = Field("", max_length=40, title="Barcode",
                         description="Optional Code 128 at the bottom of 4x6")
    captions: bool = Field(True, title="Captions", description="Print FROM / SHIP TO captions")


def _block(ctx: RenderContext, z, x, y, lines, box_w, box_h, min_size, max_size,
           center_in: float | None = None) -> float:
    """Fit ``lines`` into a design-unit box at (x, y) - ``x=None`` centers across the
    label; returns the height used."""
    img = text_block(lines, ctx.dots(box_w), ctx.dots(box_h), ctx.dots(max_size),
                     ctx.dots(min_size))
    h = z.design(img.height)
    if center_in is not None:
        y += max(0, (center_in - h) / 2)
    z.image(x, y, img)
    return h


def layout_4x6(z, ctx, p: AddressParams, to, frm) -> None:
    M = 40
    W, H = z.W, z.H
    bottom = H - M
    if p.barcode:
        bottom -= 170
    if p.reference:
        bottom -= 44

    y = M
    if frm and to:                            # return address, top-left, small
        if p.captions:
            z.text(M, y, 28, "FROM:")
            y += 36
        y += _block(ctx, z, M, y, frm, W - 2 * M, 250, 26, 44) + 22
        z.hline(M, y, W - 2 * M, 4)
        y += 30

    if p.note:                                # banner in a heavy box
        z.box(M, y, W - 2 * M, 130, 6)
        _block(ctx, z, None, y + 15, [p.note.upper()], W - 2 * M - 40, 100, 40, 96,
               center_in=100)
        y += 130 + 30

    main, caption = (to, "SHIP TO:") if to else (frm, "FROM:")
    if p.captions:
        z.text(M, y, 40, caption)
        y += 58
    avail = bottom - y - 20
    indent = 40 if p.captions else 0
    if (frm and to) or p.note:                # recipient: upper part of the space left
        img = text_block(main, ctx.dots(W - 2 * M - indent), ctx.dots(avail),
                         ctx.dots(118), ctx.dots(44))
        h = z.design(img.height)
        z.image(M + indent, y + max(0, (avail - h) / 3), img)
    else:                                     # one address alone: center it
        _block(ctx, z, M + indent, y, main, W - 2 * M - indent, avail, 44, 130,
               center_in=avail)

    yb = H - M
    if p.reference:
        yb -= 32
        z.text(M, yb, 30, p.reference)
    if p.barcode:
        yb -= 170
        z.code128(M, yb + 10, p.barcode, 110, module=3)


def layout_2x1(z, ctx, p: AddressParams, lines, caption) -> None:
    M = 16
    W, H = z.W, z.H
    y = M
    if p.captions:
        z.text(M, M - 6, 20, caption)
        y += 20
    _block(ctx, z, M, y, lines, W - 2 * M, H - y - M, 18, 70, center_in=H - y - M)


@register
class AddressLabel(Label):
    id = "address"
    name = "Address label"
    description = ("Mailing label from plain addresses: to + from on one 4x6, or one "
                   "address per 2x1.")
    icon = "mdi:email-outline"
    Params = AddressParams

    def render(self, p: AddressParams, ctx: RenderContext) -> RenderResult:
        to = split_lines(p.to)
        frm = split_lines(p.sender)
        if p.include == "to":
            frm = []
        elif p.include == "from":
            to = []
        if not (to or frm):
            wanted = {"to": "a To address", "from": "a From (return) address",
                      "both": "a To and/or From address"}[p.include]
            raise LabelError(f"enter {wanted}")

        if ctx.size.id == "2x1":
            blocks = [(frm, "FROM"), (to, "TO")]
            zpl = ""
            for lines, caption in blocks:
                if lines:
                    z = ctx.zpl()
                    layout_2x1(z, ctx, p, lines, caption)
                    zpl += z.build()
            n = sum(1 for lines, _ in blocks if lines)
        else:
            z = ctx.zpl()
            layout_4x6(z, ctx, p, to, frm)
            zpl, n = z.build(), 1
        return RenderResult(zpl, title="address", data={
            "labels": n, "to": to, "from": frm})
