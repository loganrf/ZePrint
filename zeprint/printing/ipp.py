"""
Minimal IPP/1.1 Print-Job client for raw ZPL to a CUPS queue.

This is the route for a printer attached to another machine's CUPS - e.g. the
Mac the original scripts ran on, reachable from a container as
``ipp://host.docker.internal:631/printers/GX430t`` once the queue is shared.
The document is sent as ``application/vnd.cups-raw`` so CUPS passes the ZPL
through untouched (the same thing ``lp -o raw`` does).
"""

from __future__ import annotations

import itertools
import ssl
import struct
import urllib.error
import urllib.request

from ..errors import PrinterError

_TAG_OPERATION = 0x01
_TAG_END = 0x03
_CHARSET = 0x47
_NATURAL_LANGUAGE = 0x48
_URI = 0x45
_NAME = 0x42
_MIME = 0x49
_PRINT_JOB = 0x0002

STATUS = {
    0x0400: "client-error-bad-request", 0x0401: "client-error-forbidden",
    0x0402: "client-error-not-authenticated", 0x0403: "client-error-not-authorized",
    0x0404: "client-error-not-possible", 0x0406: "client-error-not-found",
    0x040A: "client-error-document-format-not-supported",
    0x0500: "server-error-internal-error", 0x0503: "server-error-service-unavailable",
    0x0506: "server-error-not-accepting-jobs", 0x0507: "server-error-busy",
}

_ids = itertools.count(1)


def _attr(tag: int, name: str, value: str) -> bytes:
    n, v = name.encode(), value.encode()
    return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(v)) + v


def encode_print_job(printer_uri: str, job_name: str, data: bytes, user: str = "zeprint",
                     request_id: int | None = None) -> bytes:
    rid = request_id if request_id is not None else next(_ids)
    body = struct.pack(">BBHI", 1, 1, _PRINT_JOB, rid)
    body += bytes([_TAG_OPERATION])
    body += _attr(_CHARSET, "attributes-charset", "utf-8")
    body += _attr(_NATURAL_LANGUAGE, "attributes-natural-language", "en")
    body += _attr(_URI, "printer-uri", printer_uri)
    body += _attr(_NAME, "requesting-user-name", user)
    body += _attr(_NAME, "job-name", job_name[:255])
    body += _attr(_MIME, "document-format", "application/vnd.cups-raw")
    body += bytes([_TAG_END])
    return body + data


def print_job(http_url: str, printer_uri: str, data: bytes, job_name: str,
              timeout: float = 30, verify_tls: bool = True) -> int:
    """POST a Print-Job; returns the IPP status code or raises PrinterError."""
    req = urllib.request.Request(http_url, data=encode_print_job(printer_uri, job_name, data),
                                 headers={"Content-Type": "application/ipp"}, method="POST")
    handlers: list = [urllib.request.ProxyHandler({})]      # LAN printers: never via a proxy
    if http_url.startswith("https:"):
        ctx = ssl.create_default_context()
        if not verify_tls:                                   # CUPS ships self-signed certs
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as r:
            resp = r.read()
    except urllib.error.HTTPError as e:
        hint = (" - is the queue shared, and does CUPS allow remote access?"
                if e.code in (401, 403) else "")
        raise PrinterError(f"CUPS at {http_url} answered HTTP {e.code}{hint}") from e
    except (urllib.error.URLError, OSError) as e:
        raise PrinterError(f"could not reach CUPS at {http_url} ({getattr(e, 'reason', e)})") from e
    if len(resp) < 8:
        raise PrinterError(f"CUPS at {http_url} sent a truncated IPP response")
    status = struct.unpack(">H", resp[2:4])[0]
    if status >= 0x0400:
        raise PrinterError(f"CUPS refused the job: {STATUS.get(status, hex(status))}")
    return status
