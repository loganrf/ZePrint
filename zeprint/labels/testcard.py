"""Printer test card: registration marks, ruler, hairlines, Code 128 (from zebra_print.py)."""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from . import Label, RenderContext, RenderResult, register


class TestCardParams(BaseModel):
    title: str = Field("", max_length=32,
                       description="Heading; defaults to the printer's name")


@register
class TestCard(Label):
    id = "test"
    name = "Test label"
    description = ("Alignment and print-quality check: corner blocks, inch ruler, "
                   "1/2/4/8-dot hairlines and a Code 128 barcode.")
    icon = "mdi:printer-check"
    Params = TestCardParams

    def render(self, p: TestCardParams, ctx: RenderContext) -> RenderResult:
        z = ctx.zpl()
        title = p.title or ctx.printer_name or "ZEBRA GX430t"
        code = f"TEST-{ctx.now:%H%M%S}"
        stock = "thermal transfer (ribbon)" if ctx.media == "ribbon" else "direct thermal"
        if ctx.size.id == "2x1":
            self._small(z, title, code, ctx)
        else:
            self._large(z, title, code, stock, ctx)
        return RenderResult(z.build(), title="test-label")

    @staticmethod
    def _rules(z, x: float, y0: float, gap: float, length: float, widths=(1, 2, 4, 8)) -> None:
        """Hairlines in exact *device* dots - the point of the check."""
        for i, t in enumerate(widths):
            z.raw(f"^FO{z.dots(x)},{z.dots(y0 + i * gap)}^GB{z.dots(length)},{t},{t}^FS")

    def _large(self, z, title, code, stock, ctx: RenderContext) -> None:
        lw, lh = z.W, z.H
        z.box(20, 20, lw - 40, lh - 40, 4)                       # border
        for bx, by in ((40, 40), (lw - 100, 40), (40, lh - 100), (lw - 100, lh - 100)):
            z.box(bx, by, 60, 60, 60)                            # corner registration blocks
        z.text(140, 90, 90, title[:22])
        z.text(140, 200, 45, f"Test label - {ctx.size.width_in:g} x {ctx.size.height_in:g} in "
                             f"@ {ctx.dpi} dpi")
        z.hline(140, 270, lw - 280, 3)
        z.text(140, 320, 40, f"Printer: {ctx.printer_name or '(preview)'}")
        z.text(140, 380, 40, f"Time:  {ctx.now:%Y-%m-%d %H:%M:%S}")
        z.text(140, 440, 40, f"Stock: {stock}, gap")
        z.code128(140, 560, code, 180, module=4)
        z.text(140, 930, 35, "Quality check: solid + 1/2/4/8 dot rules")
        z.box(140, 990, 240, 120, 120)
        self._rules(z, 440, 990, 36, 300)
        z.text(140, lh - 160, 35, "All four corners + every inch tick")
        z.text(140, lh - 115, 35, "visible = alignment is good.")
        for i in range(1, math.ceil(ctx.size.height_in)):
            y = i * 300                                          # one inch in design units
            z.box(20, y, 90, 4, 4)
            z.text(120, y - 22, 30, f'{i}"')

    def _small(self, z, title, code, ctx: RenderContext) -> None:
        lw, lh = z.W, z.H
        z.box(8, 8, lw - 16, lh - 16, 3)
        for bx, by in ((16, 16), (lw - 56, 16), (16, lh - 56), (lw - 56, lh - 56)):
            z.box(bx, by, 40, 40, 40)
        z.text(70, 26, 40, title[:18])
        z.text(70, 72, 24, f"{ctx.size.width_in:g}x{ctx.size.height_in:g} in @ {ctx.dpi}dpi  "
                           f"{'ribbon' if ctx.media == 'ribbon' else 'direct'}")
        z.box(70, 110, 120, 60, 60)
        self._rules(z, 210, 116, 24, 160, widths=(1, 2, 4))
        z.code128(70, 190, code, 70, module=2)
