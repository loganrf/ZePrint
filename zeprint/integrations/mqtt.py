"""
MQTT bridge with Home Assistant discovery.

Enabled by setting ``MQTT_HOST``. Home Assistant (with its MQTT integration)
then finds a "ZePrint" device with:

  * a button per label       press = print it with its saved defaults on the
                             default printer
  * "Last print job" sensor  state = queued/rendering/sending/done/error, the
                             job as attributes
  * a status sensor per      ready / paper_out / head_open / paused / offline...
    printer                  from ``~HS`` (TCP and USB printers), polled

Command topics (``<base>`` defaults to ``zeprint``):

  <base>/print/<label>       payload empty / "PRESS" -> print with defaults, or a
                             JSON object: {"station": "9446484", "size": "2x1",
                             "printer": "zebra", "copies": 2}
  <base>/print               JSON with a "label" key plus the same fields
  <base>/raw/<printer>       raw ZPL to send as-is

State topics: <base>/status (online/offline, retained LWT), <base>/job,
<base>/printer/<id>, <base>/error.

Environment:
  MQTT_HOST, MQTT_PORT (1883), MQTT_USERNAME, MQTT_PASSWORD, MQTT_TLS (false),
  MQTT_BASE_TOPIC (zeprint), MQTT_DISCOVERY_PREFIX (homeassistant),
  MQTT_NODE_ID (zeprint), MQTT_STATUS_INTERVAL (60 s, 0 = off),
  ZEPRINT_URL (shown as the device's configuration link in HA)
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from .. import __version__, labels
from ..errors import ZePrintError

log = logging.getLogger(__name__)

_RESERVED = {"params", "printer", "size", "copies", "label"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


class MqttBridge:
    name = "mqtt"

    def __init__(self, svc, host: str, port: int = 1883, username: str | None = None,
                 password: str | None = None, tls: bool = False, base: str = "zeprint",
                 discovery_prefix: str = "homeassistant", node_id: str = "zeprint",
                 status_interval: float = 60, url: str | None = None):
        self.svc = svc
        self.host, self.port = host, port
        self.username, self.password, self.tls = username, password, tls
        self.base = base.rstrip("/")
        self.prefix = discovery_prefix.rstrip("/")
        self.node = _slug(node_id)
        self.status_interval = status_interval
        self.url = url
        self.client = None
        self._published_printers: set[str] = set()
        self._stop = threading.Event()
        self._unsub = None
        self._poller: threading.Thread | None = None

    @classmethod
    def from_env(cls, svc, env) -> "MqttBridge":
        return cls(svc, env["MQTT_HOST"], int(env.get("MQTT_PORT", 1883)),
                   env.get("MQTT_USERNAME") or None, env.get("MQTT_PASSWORD") or None,
                   env.get("MQTT_TLS", "").lower() in ("1", "true", "yes"),
                   env.get("MQTT_BASE_TOPIC", "zeprint"),
                   env.get("MQTT_DISCOVERY_PREFIX", "homeassistant"),
                   env.get("MQTT_NODE_ID", "zeprint"),
                   float(env.get("MQTT_STATUS_INTERVAL", 60)),
                   env.get("ZEPRINT_URL") or None)

    # ------------------------------------------------------------ topics

    @property
    def availability_topic(self) -> str:
        return f"{self.base}/status"

    def device(self) -> dict:
        d = {"identifiers": [f"zeprint_{self.node}"], "name": "ZePrint",
             "manufacturer": "ZePrint", "model": "Zebra label service", "sw_version": __version__}
        if self.url:
            d["configuration_url"] = self.url
        return d

    def _common(self, object_id: str) -> dict:
        return {"unique_id": f"{self.node}_{object_id}",
                "availability_topic": self.availability_topic, "device": self.device()}

    def discovery_messages(self) -> list[tuple[str, dict | None]]:
        """(topic, payload) for every entity; payload None removes a stale entity."""
        msgs: list[tuple[str, dict | None]] = []
        for cls in labels.all_labels():
            oid = f"print_{_slug(cls.id)}"
            msgs.append((f"{self.prefix}/button/{self.node}/{oid}/config", {
                **self._common(oid), "name": f"Print {cls.name}",
                "command_topic": f"{self.base}/print/{cls.id}", "payload_press": "PRESS",
                "icon": cls.icon}))
        msgs.append((f"{self.prefix}/sensor/{self.node}/last_job/config", {
            **self._common("last_job"), "name": "Last print job", "icon": "mdi:printer-pos",
            "state_topic": f"{self.base}/job", "value_template": "{{ value_json.status }}",
            "json_attributes_topic": f"{self.base}/job"}))
        current = set()
        for p in self.svc.settings.printers:
            current.add(p.id)
            oid = f"printer_{_slug(p.id)}"
            msgs.append((f"{self.prefix}/sensor/{self.node}/{oid}/config", {
                **self._common(oid), "name": f"{p.name} status", "icon": "mdi:printer",
                "state_topic": f"{self.base}/printer/{p.id}",
                "value_template": "{{ value_json.state }}",
                "json_attributes_topic": f"{self.base}/printer/{p.id}"}))
        for gone in self._published_printers - current:
            msgs.append((f"{self.prefix}/sensor/{self.node}/printer_{_slug(gone)}/config", None))
        self._published_printers = current
        return msgs

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"zeprint-{self.node}")
        if self.username:
            c.username_pw_set(self.username, self.password)
        if self.tls:
            c.tls_set()
        c.will_set(self.availability_topic, "offline", qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.reconnect_delay_set(1, 60)
        self.client = c
        c.connect_async(self.host, self.port, keepalive=60)
        c.loop_start()
        self._unsub = self.svc.events.subscribe(self._on_event)
        if self.status_interval > 0:
            self._poller = threading.Thread(target=self._poll, name="zeprint-mqtt-status",
                                            daemon=True)
            self._poller.start()
        log.info("MQTT bridge connecting to %s:%s (base topic %s)", self.host, self.port, self.base)

    def stop(self) -> None:
        self._stop.set()
        if self._unsub:
            self._unsub()
        if self.client:
            try:
                self.client.publish(self.availability_topic, "offline", qos=1, retain=True)
                self.client.disconnect()
            finally:
                self.client.loop_stop()

    # ----------------------------------------------------------- handlers

    def _publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        if self.client is None:
            return
        if payload is None:
            data = ""
        elif isinstance(payload, (dict, list)):
            data = json.dumps(payload)
        else:
            data = str(payload)
        self.client.publish(topic, data, qos=1, retain=retain)

    def publish_discovery(self) -> None:
        for topic, payload in self.discovery_messages():
            self._publish(topic, payload, retain=True)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("MQTT connected")
        client.subscribe([(f"{self.base}/print", 1), (f"{self.base}/print/+", 1),
                          (f"{self.base}/raw/+", 1), (f"{self.prefix}/status", 1)])
        self._publish(self.availability_topic, "online", retain=True)
        self.publish_discovery()
        # status queries can take seconds; keep them off paho's network thread
        threading.Thread(target=self._poll_once, daemon=True).start()

    def _on_message(self, client, userdata, msg):
        try:
            self.handle(msg.topic, msg.payload)
        except Exception:
            log.exception("MQTT message on %s failed", msg.topic)

    def handle(self, topic: str, payload: bytes) -> None:
        """Route one inbound message (public for tests)."""
        text = payload.decode("utf-8", "replace").strip() if payload else ""
        if topic == f"{self.prefix}/status":
            if text == "online":            # Home Assistant restarted: re-announce
                self.publish_discovery()
            return
        try:
            if topic.startswith(f"{self.base}/raw/"):
                printer = topic[len(self.base) + 5:]
                self.svc.print_raw(payload, printer, title="raw ZPL (mqtt)", source="mqtt")
                return
            if topic == f"{self.base}/print":
                body = self._json(text)
                label = body.pop("label", None)
                if not label:
                    raise ValueError(f"{topic} needs a JSON object with a \"label\" key")
            elif topic.startswith(f"{self.base}/print/"):
                label = topic[len(self.base) + 7:]
                body = {} if text in ("", "PRESS") else self._json(text)
            else:
                return
            params = {**{k: v for k, v in body.items() if k not in _RESERVED},
                      **(body.get("params") or {})}
            self.svc.print_label(label, params, printer_id=body.get("printer"),
                                 size=body.get("size"), copies=int(body.get("copies", 1)),
                                 source="mqtt")
        except (ZePrintError, ValueError) as e:
            log.warning("MQTT command on %s rejected: %s", topic, e)
            self._publish(f"{self.base}/error", {"topic": topic, "error": str(e)})
            self._publish(f"{self.base}/job", {"status": "error", "error": str(e),
                                               "source": "mqtt"}, retain=True)

    @staticmethod
    def _json(text: str) -> dict:
        try:
            body = json.loads(text) if text else {}
        except ValueError:
            raise ValueError("payload is not JSON") from None
        if not isinstance(body, dict):
            raise ValueError("payload must be a JSON object")
        return body

    def _on_event(self, event: str, payload: Any) -> None:
        if event == "job":
            self._publish(f"{self.base}/job", payload, retain=True)
        elif event == "config":
            self.publish_discovery()

    def _poll_once(self) -> None:
        for p in self.svc.settings.printers:
            if self._stop.is_set():
                return
            try:
                status = self.svc.printer_status(p.id)
            except Exception as e:
                status = {"state": "error", "error": str(e)}
            self._publish(f"{self.base}/printer/{p.id}", status, retain=True)

    def _poll(self) -> None:
        while not self._stop.wait(self.status_interval):
            if self.client is not None and self.client.is_connected():
                self._poll_once()
