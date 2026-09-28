"""
Uploaded files (images / PDFs) that labels can reference by id - e.g. a carrier's
shipping label for the ``image`` label. Stored in ``<data>/uploads`` and pruned
after a week.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import InvalidRequest, NotFoundError

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_BYTES = 25 << 20
TTL_SECONDS = 7 * 86400

_MAGIC = [(b"%PDF", "application/pdf"), (b"\x89PNG", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
          (b"GIF8", "image/gif"), (b"BM", "image/bmp"), (b"II*\x00", "image/tiff"),
          (b"MM\x00*", "image/tiff")]


def sniff(data: bytes) -> str | None:
    """Content type from the file's magic bytes (never trust the client's header)."""
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def pdf_page_count(data: bytes) -> int:
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as e:
        raise InvalidRequest(f"that PDF can't be read ({e})") from None
    try:
        return len(pdf)
    finally:
        pdf.close()


class UploadStore:
    def __init__(self, directory: Path | str, max_bytes: int = MAX_BYTES,
                 ttl_seconds: float = TTL_SECONDS):
        self.dir = Path(directory)
        self.max_bytes = max_bytes
        self.ttl = ttl_seconds
        self._lock = threading.Lock()

    def _paths(self, upload_id: str) -> tuple[Path, Path]:
        if not ID_RE.match(upload_id or ""):
            raise NotFoundError(f"no upload {upload_id!r}")
        return self.dir / f"{upload_id}.bin", self.dir / f"{upload_id}.json"

    def put(self, data: bytes, filename: str | None = None) -> dict[str, Any]:
        if not data:
            raise InvalidRequest("the upload is empty")
        if len(data) > self.max_bytes:
            raise InvalidRequest(f"uploads are limited to {self.max_bytes >> 20} MiB")
        kind = sniff(data)
        if kind is None:
            raise InvalidRequest("unsupported file; upload a PDF or an image "
                                 "(PNG, JPG, GIF, BMP, TIFF, WebP)")
        meta: dict[str, Any] = {
            "id": uuid.uuid4().hex[:16],
            "filename": Path(filename).name[:200] if filename else None,
            "content_type": kind, "size": len(data),
            "created": datetime.now(timezone.utc).isoformat(),
        }
        if kind == "application/pdf":
            meta["pages"] = pdf_page_count(data)
        self.prune()
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            blob, info = self._paths(meta["id"])
            blob.write_bytes(data)
            info.write_text(json.dumps(meta))
        return meta

    def meta(self, upload_id: str) -> dict[str, Any]:
        _, info = self._paths(upload_id)
        try:
            return json.loads(info.read_text())
        except FileNotFoundError:
            raise NotFoundError(f"no upload {upload_id!r} (uploads expire after "
                                f"{self.ttl / 86400:g} days)") from None

    def get(self, upload_id: str) -> tuple[bytes, dict[str, Any]]:
        meta = self.meta(upload_id)
        blob, _ = self._paths(upload_id)
        try:
            return blob.read_bytes(), meta
        except FileNotFoundError:
            raise NotFoundError(f"no upload {upload_id!r}") from None

    def list(self) -> list[dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        out = []
        for info in self.dir.glob("*.json"):
            try:
                out.append(json.loads(info.read_text()))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda m: m.get("created", ""), reverse=True)

    def delete(self, upload_id: str) -> None:
        blob, info = self._paths(upload_id)
        if not info.exists():
            raise NotFoundError(f"no upload {upload_id!r}")
        with self._lock:
            for p in (blob, info):
                p.unlink(missing_ok=True)

    def prune(self) -> int:
        """Delete uploads older than the TTL."""
        if not self.dir.is_dir():
            return 0
        cutoff = time.time() - self.ttl
        removed = 0
        with self._lock:
            for blob in self.dir.glob("*.bin"):
                try:
                    if blob.stat().st_mtime < cutoff:
                        blob.unlink(missing_ok=True)
                        blob.with_suffix(".json").unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
        if removed:
            log.info("pruned %d expired upload(s)", removed)
        return removed
