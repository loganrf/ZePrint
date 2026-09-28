"""
Address labels from plain text.

4x6: return address and recipient on one label (or either alone), plus an
     optional banner (FRAGILE, PRIORITY...), reference line and Code 128.
2x1: one address per label; "both" prints two labels - the return address,
     then the recipient - in one job. The banner, reference and barcode go on
     the recipient's label (or the only one).

Addresses are one line per row (or rows separated by ``|``). Save your return
address as this label's default once, and only the recipient changes per print.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..errors import LabelError
from . import Label, RenderContext, RenderResult, register
from ._text import fits, split_lines, text_block

MAX_LINES = 8


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
                      description="Optional big line, e.g. FRAGILE or PRIORITY")
    reference: str = Field("", max_length=64, title="Reference",
                           description="Optional small line at the bottom (order #, contents)")
    barcode: str = Field("", max_length=40, title="Barcode",
                         description="Optional Code 128 at the bottom")
    captions: bool = Field(True, title="Captions", description="Print FROM / SHIP TO captions")


def _block(ctx: RenderContext, z, x, y, lines, box_w, box_h, min_size, max_size,
           center_in: float | None = None, bold_first: bool = True) -> float:
    """Fit ``lines`` into a design-unit box at (x, y) - ``x=None`` centers across the
    label; returns the height used."""
    img = text_block(lines, ctx.dots(box_w), ctx.dots(box_h), ctx.dots(max_size),
                     ctx.dots(min_size), bold_first=bold_first)
    h = z.design(img.height)
    if center_in is not None:
        y += max(0, (center_in - h) / 2)
    z.image(x, y, img)
    return h


def _code128(z, x, y, data: str, height, width, interpretation: bool = True) -> None:
    """Code 128 with the widest module (3, else 2 design units) that fits ``width``."""
    modules = 11 * (len(data) + 3) + 2            # start + data + check + stop, subset B
    for module in (3, 2):
        if modules * max(1, z.dots(module)) <= z.dots(width):
            z.code128(x, y, data, height, module=module, interpretation=interpretation)
            return
    raise LabelError(f"the barcode is too long for a {z.size.id} label "
                     f"({len(data)} characters)")


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
        _code128(z, M, yb + 10, p.barcode, 110, W - 2 * M)


def layout_2x1(z, ctx, p: AddressParams, lines, caption, extras: bool) -> None:
    """One address; ``extras`` adds the banner, reference and barcode."""
    M = 16
    W, H = z.W, z.H
    note, ref, code = (p.note, p.reference, p.barcode) if extras else ("", "", "")
    y, bottom = M, H - M
    if code:                                  # bottom up: barcode, then reference
        bottom -= 40
        _code128(z, M, bottom, code, 40, W - 2 * M, interpretation=False)
        bottom -= 6
    if ref:
        bottom -= 24
        _block(ctx, z, M, bottom, [ref], W - 2 * M, 24, 12, 22, bold_first=False)
        bottom -= 6
    if note:                                  # banner in a heavy box
        z.box(M, y, W - 2 * M, 52, 4)
        _block(ctx, z, None, y + 8, [note.upper()], W - 2 * M - 24, 36, 16, 36, center_in=36)
        y += 52 + 10
    if p.captions:
        z.text(M, y - 6, 20, caption)
        y += 20
    if (note or ref or code) and not fits(lines, ctx.dots(18), ctx.dots(bottom - y)):
        raise LabelError("the address has too many lines for a 2x1 label with a banner, "
                         "reference or barcode: leave those out, or print on 4x6")
    _block(ctx, z, M, y, lines, W - 2 * M, bottom - y, 18, 70, center_in=bottom - y)


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
        for lines, which in ((to, "To"), (frm, "From")):
            if len(lines) > MAX_LINES:
                raise LabelError(f"the {which} address has {len(lines)} lines "
                                 f"({MAX_LINES} at most)")
        if not (to or frm):
            wanted = {"to": "a To address", "from": "a From (return) address",
                      "both": "a To and/or From address"}[p.include]
            raise LabelError(f"enter {wanted}")

        if ctx.size.id == "2x1":
            blocks = [(lines, caption) for lines, caption in ((frm, "FROM"), (to, "TO"))
                      if lines]
            zpl = ""
            for i, (lines, caption) in enumerate(blocks):
                z = ctx.zpl()
                layout_2x1(z, ctx, p, lines, caption, extras=i == len(blocks) - 1)
                zpl += z.build()
            n = len(blocks)
        else:
            z = ctx.zpl()
            layout_4x6(z, ctx, p, to, frm)
            zpl, n = z.build(), 1
        return RenderResult(zpl, title="address", data={
            "labels": n, "to": to, "from": frm})
