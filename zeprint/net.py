"""
HTTP fetching for label data sources.

One place for the user agent, the CA bundle (certifi, so slim Python builds can
verify TLS), friendly errors, and a small in-memory TTL cache - so previewing a
label and then printing it doesn't hit NOAA / S3 twice.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from functools import lru_cache

from . import __version__
from .errors import FetchError

USER_AGENT = f"ZePrint/{__version__} (+https://github.com/loganrf/ZePrint)"
CACHE_MAX_BYTES = 64 << 20


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    # SSL_CERT_FILE wins (TLS-inspecting corporate proxies); else certifi's bundle,
    # since slim / pyenv Pythons often ship without one
    cafile = os.environ.get("SSL_CERT_FILE")
    if not cafile:
        try:
            import certifi
            cafile = certifi.where()
        except Exception:
            cafile = None
    return ssl.create_default_context(cafile=cafile)


class _TTLCache:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires, data = item
            if expires < time.monotonic():
                self._drop(key)
                return None
            self._items.move_to_end(key)
            return data

    def put(self, key: str, data: bytes, ttl: float) -> None:
        if len(data) > self.max_bytes // 4:
            return
        with self._lock:
            if key in self._items:
                self._drop(key)
            self._items[key] = (time.monotonic() + ttl, data)
            self._size += len(data)
            while self._size > self.max_bytes and self._items:
                self._drop(next(iter(self._items)))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0

    def _drop(self, key: str) -> None:
        _, data = self._items.pop(key)
        self._size -= len(data)


cache = _TTLCache(CACHE_MAX_BYTES)


def fetch(url: str, *, source: str | None = None, timeout: float = 20, ttl: float = 0,
          max_bytes: int | None = None) -> bytes:
    """GET ``url``. ``ttl`` > 0 caches the body for that many seconds; ``max_bytes``
    rejects larger bodies."""
    if ttl:
        hit = cache.get(url)
        if hit is not None:
            return hit
    host = urllib.parse.urlsplit(url).hostname or url
    name = source or host
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            data = r.read() if max_bytes is None else r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise FetchError(f"{name} answered HTTP {e.code} ({e.reason}) for {url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        if "CERTIFICATE_VERIFY" in str(reason):
            raise FetchError(f"TLS certificate verification failed reaching {name} ({reason})") from e
        raise FetchError(f"could not reach {name} ({reason}); check that this host's network "
                         f"and DNS allow {host}") from e
    if max_bytes is not None and len(data) > max_bytes:
        raise FetchError(f"{name} sent more than {max_bytes >> 20} MiB for {url}")
    if ttl:
        cache.put(url, data, ttl)
    return data


def fetch_text(url: str, **kw) -> str:
    return fetch(url, **kw).decode("utf-8", "replace")


def fetch_json(url: str, **kw):
    data = fetch(url, **kw)
    try:
        return json.loads(data)
    except ValueError as e:
        name = kw.get("source") or urllib.parse.urlsplit(url).hostname
        raise FetchError(f"{name} returned something that isn't JSON") from e
