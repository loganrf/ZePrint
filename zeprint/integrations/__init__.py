"""
Optional integrations, started next to the web service when configured.

An integration is any object with ``name``, ``start()`` and ``stop()`` that
talks to the core through the :class:`~zeprint.service.ZePrint` API and its
event bus (``svc.events``). Add new ones here.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def start_all(svc, env) -> list:
    bridges = []
    if env.get("MQTT_HOST"):
        try:
            from .mqtt import MqttBridge
            bridge = MqttBridge.from_env(svc, env)
            bridge.start()
            bridges.append(bridge)
        except Exception:
            log.exception("the MQTT / Home Assistant bridge failed to start")
    return bridges
