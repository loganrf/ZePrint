"""
DPI-aware ZPL builder.

Layouts are written in *design units*: dots on a 300 dpi head, the GX430t the
original scripts were drawn for (a 4x6 label is 1200 x 1800 design units). The
builder scales every coordinate, font size, rule, barcode module and QR
magnification to the printer's real DPI as it emits ZPL, so one layout serves
203, 300 and 600 dpi printers. Raster art is the exception: render it at device
resolution (``RenderContext.figure`` does that) and place it with
:meth:`ZPL.image`, which reports its footprint back in design units.
"""

from __future__ import annotations

from dataclasses import dataclass

from .qr import qr_modules
from .raster import compress_hex, encode_image

DESIGN_DPI = 300
DPIS = (203, 300, 600)
BAND_ROWS = 300          # ^GFA band height in dot rows; keeps each field small


@dataclass(frozen=True)
class LabelSize:
    id: str
    width_in: float
    height_in: float
    description: str

    @property
    def design_w(self) -> int:
        return round(self.width_in * DESIGN_DPI)

    @property
    def design_h(self) -> int:
        return round(self.height_in * DESIGN_DPI)

    def dots(self, dpi: int) -> tuple[int, int]:
        return round(self.width_in * dpi), round(self.height_in * dpi)


SIZES: dict[str, LabelSize] = {
    "4x6": LabelSize("4x6", 4.0, 6.0, "4 x 6 in, portrait"),
    "2x1": LabelSize("2x1", 2.0, 1.0, "2 x 1 in, landscape"),
}
SIZE_ALIASES = {"6x4": "4x6", "1x2": "2x1"}


def get_size(name: str | LabelSize) -> LabelSize:
    if isinstance(name, LabelSize):
        return name
    key = str(name).strip().lower()
    key = SIZE_ALIASES.get(key, key)
    try:
        return SIZES[key]
    except KeyError:
        raise ValueError(f"unknown label size {name!r}; choose one of: {', '.join(SIZES)}") from None


def field_data(text) -> str:
    """``^FD`` for arbitrary text; switches to ``^FH`` escapes when needed."""
    s = str(text).replace("\r", " ").replace("\n", " ")
    if "^" in s or "~" in s:
        s = s.replace("_", "_5F").replace("^", "_5E").replace("~", "_7E")
        return "^FH^FD" + s
    return "^FD" + s


def media_command(media: str) -> str:
    """``^MT``: direct thermal (default) or thermal transfer / ribbon."""
    return "^MTT" if str(media).lower() in ("ribbon", "transfer", "tt", "t") else "^MTD"


class ZPL:
    """Accumulates one label. Coordinates and sizes are in design units."""

    def __init__(self, size: LabelSize | str, dpi: int = DESIGN_DPI, *, darkness: int = 22,
                 speed: int = 2, media: str = "direct", copies: int = 1):
        if dpi <= 0:
            raise ValueError("dpi must be positive")
        self.size = get_size(size)
        self.dpi = int(dpi)
        self.k = self.dpi / DESIGN_DPI
        self.W, self.H = self.size.design_w, self.size.design_h
        self.pw, self.ll = self.size.dots(self.dpi)
        self.darkness = max(0, min(30, int(darkness)))
        self.speed = int(speed)
        self.media = media
        self.copies = max(1, int(copies))
        self._fields: list[str] = []

    # ------------------------------------------------------------------ units

    def dots(self, v: float) -> int:
        """Design units -> device dots."""
        return int(round(v * self.k))

    def design(self, dots: float) -> float:
        """Device dots -> design units."""
        return dots / self.k

    def _thick(self, v: float) -> int:
        return max(1, self.dots(v))

    def _fo(self, x: float, y: float) -> str:
        return f"^FO{self.dots(x)},{self.dots(y)}"

    # ------------------------------------------------------------- primitives

    def text(self, x: float, y: float, height: float, text, *, width: float | None = None,
             block: float | None = None, lines: int = 1, align: str = "L") -> None:
        """Scalable font 0 text; ``block`` wraps it in a ``^FB`` of that width."""
        h = self._thick(height)
        w = h if width is None else self._thick(width)
        fb = f"^FB{self.dots(block)},{lines},0,{align},0" if block else ""
        self._fields.append(f"{self._fo(x, y)}^A0N,{h},{w}{fb}{field_data(text)}^FS")

    def hline(self, x: float, y: float, length: float, thickness: float = 3) -> None:
        t = self._thick(thickness)
        self._fields.append(f"{self._fo(x, y)}^GB{max(t, self.dots(length))},{t},{t}^FS")

    def box(self, x: float, y: float, w: float, h: float, thickness: float = 3) -> None:
        """Outlined box; thickness >= min(w, h) gives a solid block."""
        t = self._thick(thickness)
        self._fields.append(
            f"{self._fo(x, y)}^GB{max(t, self.dots(w))},{max(t, self.dots(h))},{t}^FS")

    def qr_mag(self, mag: float) -> int:
        return max(1, min(10, int(round(mag * self.k))))

    def qr_side(self, data: str, mag: float) -> float:
        """Printed footprint of :meth:`qr` incl. a 4-module quiet zone, design units."""
        return self.design((qr_modules(len(data.encode())) + 8) * self.qr_mag(mag))

    def qr(self, x: float, y: float, data: str, mag: float, ec: str = "M") -> None:
        self._fields.append(
            f"{self._fo(x, y)}^BQN,2,{self.qr_mag(mag)}{field_data(f'{ec}A,{data}')}^FS")

    def code128(self, x: float, y: float, data: str, height: float, *, module: float = 2,
                ratio: float = 3, interpretation: bool = True) -> None:
        h = self._thick(height)
        self._fields.append(f"^BY{self._thick(module)},{ratio},{h}")
        self._fields.append(
            f"{self._fo(x, y)}^BCN,{h},{'Y' if interpretation else 'N'},N,N{field_data(data)}^FS")

    def image(self, x: float | None, y: float, img, threshold: int = 128) -> tuple[float, float]:
        """
        Place a raster that is already at device resolution. ``x=None`` centers
        it. Returns the placed (width, height) in design units.
        """
        packed, rb, rows = encode_image(img, threshold)
        w_design = self.design(rb * 8)
        if x is None:
            x = (self.W - w_design) / 2
        X, Y = self.dots(x), self.dots(y)
        for top in range(0, rows, BAND_ROWS):
            n = min(BAND_ROWS, rows - top)
            chunk = packed[top * rb:(top + n) * rb]
            self._fields.append(f"^FO{X},{Y + top}^GFA,{len(chunk)},{len(chunk)},{rb},"
                                f"{compress_hex(chunk, rb)}^FS")
        return w_design, self.design(rows)

    def raw(self, zpl: str) -> None:
        """Append ZPL verbatim (device units - no scaling)."""
        self._fields.append(zpl)

    # ------------------------------------------------------------------ output

    def header(self) -> list[str]:
        return [f"~SD{self.darkness:02d}", "^XA", "^CI28", f"^PW{self.pw}", f"^LL{self.ll}",
                "^LH0,0", media_command(self.media), "^MNY", f"^PR{self.speed}"]

    def build(self) -> str:
        return "\n".join(self.header() + self._fields + [f"^PQ{self.copies}", "^XZ"]) + "\n"


def calibrate_zpl(size: LabelSize | str, dpi: int = DESIGN_DPI, media: str = "direct") -> str:
    """
    Media calibration. Order matters: teach the printer the media geometry and
    persist it, THEN run the sensor calibration that feeds labels and learns the
    gap. Without a good gap profile the printer falls back to a default (often
    ~4 in) label length and either crams tall content toward the leading edge or
    ejects a blank label to re-find the gap.
    """
    pw, ll = get_size(size).dots(dpi)
    return (
        "^XA\n"
        f"{media_command(media)}\n"  # media type: direct thermal or ribbon
        "^MNY\n"                     # tracking: web/gap sensing
        f"^PW{pw}\n"                 # print width
        f"^LL{ll}\n"                 # label length
        f"^ML{ll + round(0.8 * dpi)}\n"  # max label length: sensor headroom past the label
        "^JUS\n"                     # save settings to flash
        "^XZ\n"
        "~JC\n"                      # calibrate media sensor (feeds a few labels)
    )
