"""Print jobs: a single worker thread renders and sends them in order."""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from .errors import NotFoundError, ZePrintError
from .events import EventBus

log = logging.getLogger(__name__)

Status = Literal["queued", "rendering", "sending", "done", "error"]
Work = Callable[[Callable[[str], None]], int]   # work(progress) -> bytes sent


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    kind: Literal["label", "raw"] = "label"
    label: Optional[str] = None
    title: str = ""
    printer: str
    size: Optional[str] = None
    copies: int = 1
    params: dict[str, Any] = Field(default_factory=dict)
    source: str = "api"
    status: Status = "queued"
    error: Optional[str] = None
    bytes: Optional[int] = None
    created: datetime = Field(default_factory=_now)
    started: Optional[datetime] = None
    finished: Optional[datetime] = None


class JobQueue:
    def __init__(self, events: EventBus, history: int = 200):
        self.events = events
        self.history = history
        self._q: queue.Queue = queue.Queue()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._done: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="zeprint-jobs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout)

    def submit(self, job: Job, work: Work) -> Job:
        with self._lock:
            self._jobs[job.id] = job
            self._done[job.id] = threading.Event()
            while len(self._jobs) > self.history:
                old, _ = self._jobs.popitem(last=False)
                self._done.pop(old, None)
        self._emit(job)
        self._q.put((job.id, work))
        return job.model_copy()

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise NotFoundError(f"no job {job_id!r}")
            return job.model_copy()

    def list(self, limit: int = 50) -> list[Job]:
        with self._lock:
            return [j.model_copy() for j in reversed(self._jobs.values())][:limit]

    def wait(self, job_id: str, timeout: float) -> Job:
        with self._lock:
            ev = self._done.get(job_id)
        if ev is not None:
            ev.wait(timeout)
        return self.get(job_id)

    # ------------------------------------------------------------ internals

    def _set(self, job_id: str, **changes) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for k, v in changes.items():
                setattr(job, k, v)
            snapshot = job.model_copy()
        self._emit(snapshot)
        return snapshot

    def _emit(self, job: Job) -> None:
        self.events.emit("job", job.model_dump(mode="json"))

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                continue
            job_id, work = item
            self._set(job_id, status="rendering", started=_now())
            try:
                sent = work(lambda status: self._set(job_id, status=status))
                self._set(job_id, status="done", bytes=sent, finished=_now())
            except ZePrintError as e:
                log.warning("job %s failed: %s", job_id, e)
                self._set(job_id, status="error", error=str(e), finished=_now())
            except Exception as e:
                log.exception("job %s crashed", job_id)
                self._set(job_id, status="error", error=f"internal error: {e}", finished=_now())
            finally:
                with self._lock:
                    ev = self._done.get(job_id)
                if ev:
                    ev.set()
