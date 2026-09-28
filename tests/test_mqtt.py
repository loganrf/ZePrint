import json

import pytest

from zeprint.config import PrinterConfig
from zeprint.integrations.mqtt import MqttBridge


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def is_connected(self):
        return True

    def topics(self, prefix=""):
        return [t for t, _, _ in self.published if t.startswith(prefix)]

    def last(self, topic):
        return next(p for t, p, _ in reversed(self.published) if t == topic)


@pytest.fixture
def bridge(svc):
    b = MqttBridge(svc, "localhost", status_interval=0, url="http://zeprint.local:8080")
    b.client = FakeClient()
    b._unsub = svc.events.subscribe(b._on_event)
    yield b
    b._unsub()


def test_discovery(bridge):
    msgs = dict(bridge.discovery_messages())
    btn = msgs["homeassistant/button/zeprint/print_tides/config"]
    assert btn["command_topic"] == "zeprint/print/tides" and btn["payload_press"] == "PRESS"
    assert btn["unique_id"] == "zeprint_print_tides" and btn["icon"] == "mdi:waves"
    assert btn["device"]["configuration_url"] == "http://zeprint.local:8080"
    assert msgs["homeassistant/sensor/zeprint/printer_zebra/config"]["state_topic"] == \
        "zeprint/printer/zebra"
    assert "homeassistant/sensor/zeprint/last_job/config" in msgs


def test_deleted_printer_entity_is_removed(bridge, svc):
    svc.save_printer(PrinterConfig(id="garage", name="Garage", uri="10.0.0.2"))
    bridge.discovery_messages()
    svc.delete_printer("garage")          # emits "config" -> republish
    assert bridge.client.last("homeassistant/sensor/zeprint/printer_garage/config") == ""


def test_button_press_prints(bridge, svc, printer):
    bridge.handle("zeprint/print/test", b"PRESS")
    job = svc.jobs.list()[0]
    assert svc.jobs.wait(job.id, 10).status == "done" and job.source == "mqtt"
    assert json.loads(bridge.client.last("zeprint/job"))["status"] == "done"


def test_json_payloads(bridge, svc, printer):
    bridge.handle("zeprint/print/tides", b'{"station": "9446484", "size": "2x1"}')
    job = svc.jobs.list()[0]
    assert (job.params["station"], job.size) == ("9446484", "2x1")
    bridge.handle("zeprint/print", b'{"label": "test", "params": {"title": "X"}, "copies": 2}')
    job = svc.jobs.list()[0]
    assert (job.label, job.params["title"], job.copies) == ("test", "X", 2)
    svc.jobs.wait(job.id, 10)


@pytest.mark.parametrize("topic,payload,match", [
    ("zeprint/print/tides", b'{"station": "1"}', "station"),
    ("zeprint/print/tides", b"not json", "JSON"),
    ("zeprint/print", b'{"station": "9446484"}', "label"),
    ("zeprint/print/nope", b"", "no label"),
])
def test_rejected_commands_are_reported(bridge, topic, payload, match):
    bridge.handle(topic, payload)
    err = json.loads(bridge.client.last("zeprint/error"))
    assert err["topic"] == topic and match in err["error"]


def test_ha_restart_republishes(bridge):
    bridge.handle("homeassistant/status", b"online")
    assert len(bridge.client.topics("homeassistant/")) >= 7


def test_status_poll(bridge):
    bridge._poll_once()
    assert json.loads(bridge.client.last("zeprint/printer/zebra"))["state"] == "ready"
