"""
A tiny in-process event bus: the seam integrations hang off.

Events emitted today:
    job        a job was queued or changed state   payload: Job.model_dump(mode="json")
    config     settings changed                    payload: Settings.model_dump(mode="json")
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

Handler = Callable[[str, Any], None]


class EventBus:
    def __init__(self):
        self._handlers: list[Handler] = []
        self._lock = threading.Lock()

    def subscribe(self, handler: Handler) -> Callable[[], None]:
        with self._lock:
            self._handlers.append(handler)

        def unsubscribe():
            with self._lock:
                if handler in self._handlers:
                    self._handlers.remove(handler)
        return unsubscribe

    def emit(self, event: str, payload: Any = None) -> None:
        with self._lock:
            handlers = list(self._handlers)
        for h in handlers:
            try:
                h(event, payload)
            except Exception:
                log.exception("event handler %r failed on %s", h, event)
