"""
The label plugin contract.

A label is a class with an ``id``, a pydantic ``Params`` model (which doubles as
the API/UI/Home Assistant form schema) and a ``render`` method that turns params
+ a :class:`RenderContext` into ZPL. Register it with ``@register`` and it shows
up everywhere: REST API, web UI, CLI and MQTT discovery.

    from pydantic import BaseModel, Field
    from zeprint.labels import Label, RenderResult, register

    class HelloParams(BaseModel):
        name: str = Field("world", description="Who to greet")

    @register
    class Hello(Label):
        id = "hello"
        name = "Hello"
        description = "Prints a greeting."
        Params = HelloParams

        def render(self, p, ctx):
            z = ctx.zpl()
            z.text(40, 40, 80, f"Hello, {p.name}!")    # design units (300 dpi dots)
            return RenderResult(z.build(), title="hello")
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, ClassVar

from pydantic import BaseModel

from ..zpl.builder import DESIGN_DPI, ZPL, LabelSize

# matplotlib isn't thread-safe (shared font objects), so figure work is serialized.
# pdfium isn't either: the upload store takes this lock to read PDFs too.
_DRAW_LOCK = threading.RLock()


@dataclass
class RenderContext:
    """Everything a label needs to know about where and when it will print."""

    size: LabelSize
    dpi: int = DESIGN_DPI
    darkness: int = 22
    speed: int = 2
    media: str = "direct"
    copies: int = 1
    tz: tzinfo = timezone.utc
    now: datetime | None = None
    cache_dir: Path | None = None
    printer_name: str | None = None
    # resolves an upload id or http(s) URL to (bytes, {"filename", "content_type", ...})
    open_file: Callable[[str], tuple[bytes, dict]] | None = None

    def __post_init__(self):
        if self.now is None:
            self.now = datetime.now(self.tz)
        elif self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=self.tz)
        else:
            self.now = self.now.astimezone(self.tz)

    @property
    def k(self) -> float:
        """Device dots per design unit."""
        return self.dpi / DESIGN_DPI

    def drawing(self):
        """
        Context manager to hold while building and rasterizing matplotlib figures.
        Only needed by labels that set ``fetch_outside_lock = True``; otherwise the
        service holds it for the whole render.
        """
        return _DRAW_LOCK

    def zpl(self) -> ZPL:
        return ZPL(self.size, self.dpi, darkness=self.darkness, speed=self.speed,
                   media=self.media, copies=self.copies)

    def dots(self, v: float) -> int:
        return int(round(v * self.k))

    def figure(self, w: float, h: float, dpi: float = 100):
        """
        A matplotlib Figure that is ``w`` x ``h`` design px, rendered at device
        resolution. ``dpi`` is the design-space DPI the plot was tuned at (the
        original scripts used 100, or 150 for the weather plot) - it keeps font
        and line sizes in points physically the same on any printer.
        """
        from matplotlib.figure import Figure
        return Figure(figsize=(w / dpi, h / dpi), dpi=dpi * self.k)

    def image(self, fig):
        """Rasterize a Figure to a grayscale PIL image."""
        from PIL import Image
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=fig.dpi, facecolor="white")
        buf.seek(0)
        return Image.open(buf).convert("L")

    def cache_path(self, *parts: str) -> Path | None:
        if self.cache_dir is None:
            return None
        p = Path(self.cache_dir, *parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


@dataclass
class RenderResult:
    zpl: str
    title: str = "label"
    report: str | None = None                    # optional markdown summary
    data: dict[str, Any] = field(default_factory=dict)  # machine-readable facts (HA, API)


class Label:
    id: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str] = ""
    icon: ClassVar[str] = "mdi:label-outline"    # Material Design icon, used by Home Assistant
    sizes: ClassVar[tuple[str, ...]] = ("4x6", "2x1")
    Params: ClassVar[type[BaseModel]]
    # True: render() fetches its data first and takes ``ctx.drawing()`` only around
    # figure work, so a slow data source doesn't stall other renders.
    fetch_outside_lock: ClassVar[bool] = False

    def render(self, params: BaseModel, ctx: RenderContext) -> RenderResult:  # pragma: no cover
        raise NotImplementedError
