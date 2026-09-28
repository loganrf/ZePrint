"""
Persistent settings: the printers, which one is the default, the timezone, and
per-label default parameters. Stored as JSON in the data directory.

On first start (no config file yet) the store is seeded from the environment,
so a docker-compose file can describe the printer:

    ZEPRINT_PRINTER_URI     tcp://192.168.1.50:9100 | usb:///dev/usb/lp0 | ipp://...
    ZEPRINT_PRINTER_NAME    display name            (default "Zebra GX430t")
    ZEPRINT_PRINTER_ID      short id                (default "zebra")
    ZEPRINT_PRINTER_DPI     203 | 300 | 600         (default 300)
    ZEPRINT_LABEL_SIZE      4x6 | 2x1               (default 4x6)
    ZEPRINT_DARKNESS        0-30                    (default 22)
    ZEPRINT_SPEED           print speed, ips        (default 2)
    ZEPRINT_MEDIA           direct | ribbon         (default direct)

After that, the web UI / API own the settings. ``TZ`` (or ``ZEPRINT_TIMEZONE``)
is used whenever no timezone is set explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .printing import parse_uri
from .zpl.builder import get_size

log = logging.getLogger(__name__)

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"


def valid_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except Exception:
        raise ValueError(f"unknown timezone {name!r} (use an IANA name like America/Los_Angeles)")
    return name


class PrinterConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., pattern=ID_PATTERN,
                    description="Short identifier used in URLs, API calls and MQTT topics")
    name: str = Field(..., min_length=1, max_length=64)
    uri: str = Field(..., description="tcp://host:9100, usb:///dev/usb/lp0, or "
                                      "ipp://host:631/printers/QUEUE")
    dpi: Literal[203, 300, 600] = Field(300, description="Print head resolution")
    label_size: str = Field("4x6", description="Label stock loaded in the printer (4x6 or 2x1)")
    darkness: int = Field(22, ge=0, le=30, description="Print darkness (~SD), higher = darker")
    speed: int = Field(2, ge=1, le=14, description="Print speed, inches per second")
    media: Literal["direct", "ribbon"] = Field("direct", description="Direct thermal or ribbon")

    @field_validator("uri")
    @classmethod
    def _uri(cls, v: str) -> str:
        parse_uri(v)
        return v.strip()

    @field_validator("label_size")
    @classmethod
    def _size(cls, v: str) -> str:
        return get_size(v).id


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    default_printer: Optional[str] = None
    timezone: Optional[str] = Field(None, description="IANA timezone; unset = follow TZ")
    printers: list[PrinterConfig] = Field(default_factory=list)
    label_defaults: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        return valid_timezone(v) if v else None

    @model_validator(mode="after")
    def _consistent(self):
        ids = [p.id for p in self.printers]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate printer id(s): {', '.join(sorted(dup))}")
        if self.default_printer not in ids:
            self.default_printer = ids[0] if ids else None
        return self

    def printer(self, printer_id: str | None = None) -> PrinterConfig | None:
        pid = printer_id or self.default_printer
        return next((p for p in self.printers if p.id == pid), None)


def env_timezone(env=os.environ) -> str:
    for key in ("ZEPRINT_TIMEZONE", "TZ"):
        v = (env.get(key) or "").strip().lstrip(":")
        if v:
            try:
                return valid_timezone(v)
            except ValueError:
                log.warning("ignoring %s=%r: not an IANA timezone", key, v)
    return "UTC"


def seed_from_env(env=os.environ) -> Settings:
    printers = []
    uri = (env.get("ZEPRINT_PRINTER_URI") or "").strip()
    if uri:
        try:
            printers.append(PrinterConfig(
                id=env.get("ZEPRINT_PRINTER_ID", "zebra"),
                name=env.get("ZEPRINT_PRINTER_NAME", "Zebra GX430t"),
                uri=uri,
                dpi=int(env.get("ZEPRINT_PRINTER_DPI", 300)),
                label_size=env.get("ZEPRINT_LABEL_SIZE", "4x6"),
                darkness=int(env.get("ZEPRINT_DARKNESS", 22)),
                speed=int(env.get("ZEPRINT_SPEED", 2)),
                media=env.get("ZEPRINT_MEDIA", "direct"),
            ))
        except (ValidationError, ValueError) as e:
            log.error("ignoring the ZEPRINT_PRINTER_* environment: %s", e)
    return Settings(printers=printers)


class ConfigStore:
    """Thread-safe settings with atomic writes."""

    def __init__(self, path: Path | str, env=os.environ):
        self.path = Path(path)
        self.env = env
        self._lock = threading.RLock()
        self._settings = self._load()

    def _load(self) -> Settings:
        if self.path.exists():
            try:
                return Settings.model_validate_json(self.path.read_text())
            except (ValidationError, ValueError) as e:
                backup = self.path.with_name(f"{self.path.name}.bad-{int(time.time())}")
                self.path.replace(backup)
                log.error("config %s was unreadable (%s); moved it to %s and started fresh",
                          self.path, e, backup)
        settings = seed_from_env(self.env)
        self._write(settings)
        return settings

    def _write(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(settings.model_dump(mode="json"), indent=2) + "\n")
        os.replace(tmp, self.path)

    @property
    def settings(self) -> Settings:
        with self._lock:
            return self._settings.model_copy(deep=True)

    def update(self, fn: Callable[[Settings], Settings | None]) -> Settings:
        """Apply ``fn`` to a copy, re-validate, persist, swap in. Returns the new settings."""
        with self._lock:
            draft = self._settings.model_copy(deep=True)
            result = fn(draft)
            new = Settings.model_validate((result or draft).model_dump())
            self._write(new)
            self._settings = new
            return new.model_copy(deep=True)

    def timezone(self) -> ZoneInfo:
        s = self._settings
        return ZoneInfo(s.timezone or env_timezone(self.env))
