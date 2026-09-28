"""
Printer transports, selected by URI:

    tcp://192.168.1.50:9100          raw TCP (JetDirect); the port defaults to 9100
    192.168.1.50 / host:6101         shorthand for tcp://
    usb:///dev/usb/lp0               a USB printer via the Linux usblp device node
    /dev/usb/lp0                     shorthand for usb://
    ipp://cups-host:631/printers/Q   a raw CUPS queue on another machine (ipps:// for TLS)
    cups://cups-host/Q               shorthand for ipp://cups-host:631/printers/Q

TCP and USB can also answer queries (``~HS`` status, ``~HI`` identification).
"""

from __future__ import annotations

import os
import select
import socket
import time
import urllib.parse
from dataclasses import dataclass

from ..errors import PrinterError
from . import ipp
from .status import parse_host_identification, parse_host_status

__all__ = ["Transport", "TcpTransport", "DeviceTransport", "IppTransport", "open_transport",
           "parse_uri", "parse_host_status", "parse_host_identification"]


class Transport:
    kind = "?"
    can_query = False

    def send(self, data: bytes, title: str = "zpl") -> None:  # pragma: no cover
        raise NotImplementedError

    def query(self, command: bytes, frames: int = 1, timeout: float = 3.0) -> bytes | None:
        """Send a ``~`` query and collect ``frames`` STX..ETX replies (None if unsupported)."""
        return None

    def describe(self) -> str:  # pragma: no cover
        raise NotImplementedError


@dataclass
class TcpTransport(Transport):
    host: str
    port: int = 9100
    timeout: float = 10.0
    kind = "tcp"
    can_query = True

    def describe(self) -> str:
        return f"{self.host}:{self.port}"

    def _connect(self) -> socket.socket:
        try:
            return socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise PrinterError(
                f"could not reach the printer at {self.host}:{self.port} ({e}); check it is on "
                "the network and the address is right (9100 is Zebra's raw port; some setups "
                "use 6101)") from e

    def send(self, data: bytes, title: str = "zpl") -> None:
        with self._connect() as s:
            try:
                s.sendall(data)
            except OSError as e:
                raise PrinterError(f"lost the connection to {self.describe()} ({e})") from e

    def query(self, command: bytes, frames: int = 1, timeout: float = 3.0) -> bytes | None:
        with self._connect() as s:
            s.sendall(command)
            return _collect(lambda: s.recv(4096), s.settimeout, frames, timeout)


@dataclass
class DeviceTransport(Transport):
    path: str
    kind = "usb"
    can_query = True

    def describe(self) -> str:
        return self.path

    def _error(self, e: OSError) -> PrinterError:
        if isinstance(e, FileNotFoundError):
            return PrinterError(
                f"{self.path} does not exist - is the printer on and plugged in, and the device "
                f"passed to the container (docker run --device {self.path})?")
        if isinstance(e, PermissionError):
            return PrinterError(
                f"no permission to write {self.path}; the container user needs the device's "
                "group (the entrypoint adds it automatically when started as root)")
        return PrinterError(f"could not write to {self.path} ({e})")

    def send(self, data: bytes, title: str = "zpl") -> None:
        try:
            # no O_CREAT: a missing device node must fail, not become a regular file
            fd = os.open(self.path, os.O_WRONLY)
            with os.fdopen(fd, "wb", buffering=0) as f:
                f.write(data)
        except OSError as e:
            raise self._error(e) from e

    def query(self, command: bytes, frames: int = 1, timeout: float = 3.0) -> bytes | None:
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            raise self._error(e) from e
        try:
            os.write(fd, command)

            def recv():
                r, _, _ = select.select([fd], [], [], 0.2)
                if not r:
                    return None
                try:
                    return os.read(fd, 4096)
                except BlockingIOError:
                    return None
            return _collect(recv, None, frames, timeout)
        finally:
            os.close(fd)


@dataclass
class IppTransport(Transport):
    http_url: str
    printer_uri: str
    verify_tls: bool = True
    kind = "ipp"

    def describe(self) -> str:
        return self.printer_uri

    def send(self, data: bytes, title: str = "zpl") -> None:
        ipp.print_job(self.http_url, self.printer_uri, data, title, verify_tls=self.verify_tls)


def _collect(recv, settimeout, frames: int, timeout: float) -> bytes | None:
    buf = b""
    deadline = time.monotonic() + timeout
    while buf.count(b"\x03") < frames:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        if settimeout:
            settimeout(left)
        try:
            chunk = recv()
        except (socket.timeout, TimeoutError):
            break
        if chunk is None:
            continue
        if not chunk:
            break
        buf += chunk
    return buf or None


def parse_uri(uri: str) -> Transport:
    """Validate a printer URI and build its transport (raises ValueError)."""
    uri = (uri or "").strip()
    if not uri:
        raise ValueError("printer URI is empty")
    if uri.startswith("/"):
        uri = "usb://" + uri
    elif "://" not in uri:
        uri = "tcp://" + uri
    u = urllib.parse.urlsplit(uri)
    scheme = u.scheme.lower()
    if scheme in ("usb", "file", "dev"):
        path = u.path or ""
        if not path.startswith("/dev/"):
            raise ValueError(f"USB printers are device paths like usb:///dev/usb/lp0, not {uri!r}")
        return DeviceTransport(path)
    try:
        port = u.port
    except ValueError:
        raise ValueError(f"bad port in {uri!r}") from None
    if not u.hostname:
        raise ValueError(f"no host in printer URI {uri!r}")
    if scheme in ("tcp", "raw", "socket", "jetdirect"):
        return TcpTransport(u.hostname, port or 9100)
    if scheme in ("ipp", "ipps", "cups", "http", "https"):
        tls = scheme in ("ipps", "https")
        path = u.path or ""
        if scheme == "cups":
            queue = path.strip("/")
            if not queue:
                raise ValueError("cups:// URIs need a queue name, e.g. cups://mac.local/GX430t")
            path = f"/printers/{queue}"
        if not path.strip("/"):
            raise ValueError(f"IPP URIs need the queue path, e.g. ipp://{u.hostname}:631/printers/GX430t")
        host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
        p = port or 631
        verify = "insecure" not in urllib.parse.parse_qs(u.query, keep_blank_values=True)
        return IppTransport(f"{'https' if tls else 'http'}://{host}:{p}{path}",
                            f"{'ipps' if tls else 'ipp'}://{host}:{p}{path}", verify)
    raise ValueError(f"unsupported printer URI scheme {scheme!r}; use tcp://, usb:// or ipp://")


def open_transport(uri: str) -> Transport:
    try:
        return parse_uri(uri)
    except ValueError as e:
        raise PrinterError(str(e)) from e
