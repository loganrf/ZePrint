"""
The ZePrint core: settings, labels, rendering and printing, independent of any
front end. The REST API, CLI and MQTT bridge are all thin layers over this.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from . import labels
from .config import ConfigStore, PrinterConfig, Settings
from .errors import InvalidRequest, NotFoundError, PrinterError
from .events import EventBus
from .jobs import Job, JobQueue
from .labels import Label, RenderContext, RenderResult
from .printing import open_transport, parse_host_identification, parse_host_status
from .zpl import SIZES, LabelSize, calibrate_zpl, get_size, render_png

log = logging.getLogger(__name__)

PREVIEW_SIZE = "4x6"
PREVIEW_DPI = 300


def format_validation(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        msg = err.get("msg", "invalid").removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


class ZePrint:
    def __init__(self, data_dir: Path | str, env=os.environ, plugin_dir: Path | str | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "cache"
        self.env = env
        self.events = EventBus()
        self.config = ConfigStore(self.data_dir / "config.json", env)
        self.jobs = JobQueue(self.events)
        labels.load_builtin()
        plugins = plugin_dir or env.get("ZEPRINT_PLUGIN_DIR") or self.data_dir / "plugins"
        self.plugins = labels.load_plugins(plugins)
        self.clock: Callable[[], datetime] | None = None   # tests pin "now" here
        self._render_lock = threading.Lock()       # matplotlib is not re-entrant
        self._printer_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    @classmethod
    def from_env(cls, env=os.environ) -> "ZePrint":
        return cls(env.get("ZEPRINT_DATA_DIR", "data"), env)

    def start(self) -> None:
        self.jobs.start()

    def stop(self) -> None:
        self.jobs.stop()

    # -------------------------------------------------------------- settings

    @property
    def settings(self) -> Settings:
        return self.config.settings

    def update_settings(self, fn) -> Settings:
        s = self.config.update(fn)
        self.events.emit("config", s.model_dump(mode="json"))
        return s

    def printer(self, printer_id: str | None = None) -> PrinterConfig:
        s = self.settings
        if printer_id and not any(p.id == printer_id for p in s.printers):
            raise NotFoundError(f"no printer {printer_id!r}")
        p = s.printer(printer_id)
        if p is None:
            raise InvalidRequest("no printer is configured yet; add one in the web UI or set "
                                 "ZEPRINT_PRINTER_URI")
        return p

    def _printer_or_none(self, printer_id: str | None) -> PrinterConfig | None:
        if printer_id:
            return self.printer(printer_id)
        return self.settings.printer()

    def save_printer(self, printer: PrinterConfig, replace_id: str | None = None,
                     make_default: bool = False) -> PrinterConfig:
        def fn(s: Settings):
            if replace_id is not None:
                idx = next((i for i, p in enumerate(s.printers) if p.id == replace_id), None)
                if idx is None:
                    raise NotFoundError(f"no printer {replace_id!r}")
                if printer.id != replace_id and any(p.id == printer.id for p in s.printers):
                    raise InvalidRequest(f"a printer with id {printer.id!r} already exists")
                s.printers[idx] = printer
                if s.default_printer == replace_id:
                    s.default_printer = printer.id
            else:
                if any(p.id == printer.id for p in s.printers):
                    raise InvalidRequest(f"a printer with id {printer.id!r} already exists")
                s.printers.append(printer)
            if make_default or len(s.printers) == 1:
                s.default_printer = printer.id
            return s
        self.update_settings(fn)
        return printer

    def delete_printer(self, printer_id: str) -> None:
        def fn(s: Settings):
            if not any(p.id == printer_id for p in s.printers):
                raise NotFoundError(f"no printer {printer_id!r}")
            s.printers = [p for p in s.printers if p.id != printer_id]
            return s
        self.update_settings(fn)

    # ---------------------------------------------------------------- labels

    def label(self, label_id: str) -> Label:
        cls = labels.get(label_id)
        if cls is None:
            raise NotFoundError(f"no label {label_id!r}")
        return cls()

    def label_params(self, label: Label, overrides: dict[str, Any] | None = None) -> BaseModel:
        """Model defaults < saved label defaults < request overrides."""
        fields = label.Params.model_fields
        saved = self.settings.label_defaults.get(label.id, {})
        merged = {k: v for k, v in saved.items() if k in fields}
        unknown = sorted(set(overrides or {}) - set(fields))
        if unknown:
            raise InvalidRequest(f"unknown parameter(s) for {label.id}: {', '.join(unknown)} "
                                 f"(expected: {', '.join(fields)})")
        merged.update({k: v for k, v in (overrides or {}).items()})
        try:
            return label.Params.model_validate(merged)
        except ValidationError as e:
            raise InvalidRequest(f"{label.id}: {format_validation(e)}") from None

    def save_label_defaults(self, label_id: str, params: dict[str, Any]) -> dict[str, Any]:
        label = self.label(label_id)
        fields = label.Params.model_fields
        unknown = sorted(set(params) - set(fields))
        if unknown:
            raise InvalidRequest(f"unknown parameter(s) for {label_id}: {', '.join(unknown)}")
        try:
            label.Params.model_validate(params)
        except ValidationError as e:
            raise InvalidRequest(f"{label_id}: {format_validation(e)}") from None

        def fn(s: Settings):
            if params:
                s.label_defaults[label_id] = params
            else:
                s.label_defaults.pop(label_id, None)
            return s
        self.update_settings(fn)
        return params

    def _resolve_size(self, label: Label, size: str | None,
                      printer: PrinterConfig | None) -> LabelSize:
        try:
            sz = get_size(size or (printer.label_size if printer else PREVIEW_SIZE))
        except ValueError as e:
            raise InvalidRequest(str(e)) from None
        if sz.id not in label.sizes:
            raise InvalidRequest(f"the {label.name} label has no {sz.id} layout "
                                 f"(available: {', '.join(label.sizes)})")
        return sz

    def render(self, label_id: str, params: dict[str, Any] | None = None, *,
               printer_id: str | None = None, size: str | None = None, dpi: int | None = None,
               copies: int = 1, now: datetime | None = None) -> RenderResult:
        """Render a label. Without a printer it previews at 4x6 / 300 dpi."""
        label = self.label(label_id)
        printer = self._printer_or_none(printer_id)
        sz = self._resolve_size(label, size, printer)
        p = self.label_params(label, params)
        ctx = RenderContext(
            size=sz, dpi=dpi or (printer.dpi if printer else PREVIEW_DPI),
            darkness=printer.darkness if printer else 22, speed=printer.speed if printer else 2,
            media=printer.media if printer else "direct", copies=copies,
            tz=self.config.timezone(), now=now or (self.clock() if self.clock else None),
            cache_dir=self.cache_dir,
            printer_name=printer.name if printer else None)
        with self._render_lock:
            return label.render(p, ctx)

    @staticmethod
    def preview_png(zpl: str) -> bytes:
        return render_png(zpl)

    # -------------------------------------------------------------- printing

    def send(self, printer: PrinterConfig, data: str | bytes, title: str = "zpl") -> int:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        transport = open_transport(printer.uri)
        with self._printer_locks[printer.id]:
            transport.send(raw, title)
        log.info("sent %d bytes (%s) to %s [%s]", len(raw), title, printer.id,
                 transport.describe())
        return len(raw)

    def print_label(self, label_id: str, params: dict[str, Any] | None = None, *,
                    printer_id: str | None = None, size: str | None = None, copies: int = 1,
                    source: str = "api") -> Job:
        printer = self.printer(printer_id)
        label = self.label(label_id)
        sz = self._resolve_size(label, size, printer)
        p = self.label_params(label, params)          # fail fast on bad params (422)
        job = Job(kind="label", label=label.id, title=label.name, printer=printer.id,
                  size=sz.id, copies=copies, params=p.model_dump(mode="json"), source=source)

        def work(progress):
            result = self.render(label.id, params, printer_id=printer.id, size=sz.id,
                                 copies=copies)
            progress("sending")
            return self.send(self.printer(printer.id), result.zpl, result.title)
        return self.jobs.submit(job, work)

    def print_raw(self, zpl: str | bytes, printer_id: str | None = None, title: str = "raw ZPL",
                  source: str = "api") -> Job:
        printer = self.printer(printer_id)
        if not zpl:
            raise InvalidRequest("nothing to print")
        job = Job(kind="raw", title=title, printer=printer.id, source=source)

        def work(progress):
            progress("sending")
            return self.send(self.printer(printer.id), zpl, title)
        return self.jobs.submit(job, work)

    def calibrate(self, printer_id: str | None = None, size: str | None = None,
                  source: str = "api") -> Job:
        printer = self.printer(printer_id)
        try:
            sz = get_size(size or printer.label_size)
        except ValueError as e:
            raise InvalidRequest(str(e)) from None
        return self.print_raw(calibrate_zpl(sz, printer.dpi, printer.media), printer.id,
                              title=f"calibrate {sz.id}", source=source)

    def printer_status(self, printer_id: str | None = None) -> dict[str, Any]:
        """Live ``~HS`` + ``~HI`` query. Never raises for an unreachable printer."""
        printer = self.printer(printer_id)
        out: dict[str, Any] = {"printer": printer.id, "uri": printer.uri}
        try:
            transport = open_transport(printer.uri)
        except PrinterError as e:
            return {**out, "online": False, "state": "error", "error": str(e)}
        out["transport"] = transport.kind
        if not transport.can_query:
            return {**out, "online": None, "state": "unknown",
                    "error": f"{transport.kind} printers can't report status"}
        lock = self._printer_locks[printer.id]
        if not lock.acquire(timeout=10):
            return {**out, "online": True, "state": "printing"}
        try:
            raw = transport.query(b"~HS", frames=3)
            if not raw:
                return {**out, "online": False, "state": "offline",
                        "error": "the printer did not answer ~HS"}
            out.update(parse_host_status(raw, printer.media), online=True)
            ident = transport.query(b"~HI", frames=1, timeout=2)
            if ident:
                try:
                    out["identity"] = parse_host_identification(ident)
                except ValueError:
                    pass
        except PrinterError as e:
            return {**out, "online": False, "state": "offline", "error": str(e)}
        except ValueError as e:
            return {**out, "online": True, "state": "unknown", "error": str(e)}
        finally:
            lock.release()
        return out

    # ---------------------------------------------------------------- facts

    def describe_label(self, cls: type[Label]) -> dict[str, Any]:
        saved = self.settings.label_defaults.get(cls.id, {})
        try:
            effective = self.label_params(cls()).model_dump(mode="json")
        except InvalidRequest:          # saved defaults no longer validate
            effective = None
        return {"id": cls.id, "name": cls.name, "description": cls.description,
                "icon": cls.icon, "sizes": list(cls.sizes),
                "schema": cls.Params.model_json_schema(), "defaults": saved,
                "effective_defaults": effective}

    @staticmethod
    def sizes() -> list[dict[str, Any]]:
        return [{"id": s.id, "width_in": s.width_in, "height_in": s.height_in,
                 "description": s.description} for s in SIZES.values()]
