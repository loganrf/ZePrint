"""Regression tests for issues found in code review."""

import datetime as dt
import json
import re
import socket
import struct
import threading
import time

import pytest

from conftest import NOW
from zeprint.jobs import Job, JobQueue
from zeprint.events import EventBus
from zeprint.zpl import ZPL, render_preview


def test_raw_wait_does_not_block_the_event_loop(svc, monkeypatch):
    """An async endpoint waiting on a slow job must not freeze other requests."""
    import httpx
    import uvicorn
    from zeprint.api import create_app

    real_send = svc.send
    monkeypatch.setattr(svc, "send", lambda *a, **k: (time.sleep(2), real_send(*a, **k))[1])
    server = uvicorn.Server(uvicorn.Config(create_app(svc, token="", start_integrations=False),
                                           host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    slow = threading.Thread(target=lambda: httpx.post(f"{base}/api/printers/zebra/raw?wait=10",
                                                      content=b"^XA^XZ", timeout=20))
    slow.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    assert httpx.get(f"{base}/api/health", timeout=5).status_code == 200
    assert time.monotonic() - t0 < 1.0
    slow.join()
    server.should_exit = True


def test_status_survives_connection_reset(svc):
    """A printer that accepts and then resets reports offline, not a 500."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def rst():
        conn, _ = srv.accept()
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()
    threading.Thread(target=rst, daemon=True).start()
    from zeprint.config import PrinterConfig
    svc.save_printer(PrinterConfig(id="rst", name="Reset",
                                   uri=f"tcp://127.0.0.1:{srv.getsockname()[1]}"))
    status = svc.printer_status("rst")
    assert status["state"] == "offline" and status["online"] is False
    srv.close()


@pytest.mark.parametrize("zpl", [
    "^XA^PW60000^LL60000^XZ",
    "^XA^FO0,0^GFA,999999999,999999999,100," + "z" * 5000 + "F^FS^XZ",
    "^XA^FO0,-99999999^GFA,999999999,999999999,1000,zzzzzF^FS^XZ",
    "^XA^BY10^FO0,0^BCN,99999^FD" + "A" * 500_000 + "^FS^XZ",
    "^XA^FO0,0^A0N,99999,99999^FB1200,9999,0,L,0^FD" + "word " * 100_000 + "^FS^XZ",
    "^XA^FO0,0^BQN,2,999^FDMA,hello^FS^XZ",
])
def test_preview_bounds_hostile_zpl(zpl):
    t0 = time.monotonic()
    img = render_preview(zpl)
    assert img.width <= 8000 and img.height <= 8000
    assert time.monotonic() - t0 < 5


def test_darkness_is_two_digits():
    assert ZPL("4x6", darkness=5).build().startswith("~SD05\n")


# ------------------------------------------------------------- tides timezone

def test_us_dst_boundaries():
    from zeprint.labels.tides import station_tz
    utc = dt.timezone.utc

    def off(*args):
        return station_tz(-8, True, dt.datetime(*args, tzinfo=utc)).utcoffset(None)
    # 2026: DST starts Sun Mar 8 02:00 PST (10:00 UTC), ends Sun Nov 1 02:00 PDT (09:00 UTC)
    assert off(2026, 3, 8, 9, 59) == dt.timedelta(hours=-8)
    assert off(2026, 3, 8, 10, 0) == dt.timedelta(hours=-7)
    assert off(2026, 11, 1, 8, 59) == dt.timedelta(hours=-7)
    assert off(2026, 11, 1, 9, 0) == dt.timedelta(hours=-8)
    hawaii = station_tz(-10, False, dt.datetime(2026, 7, 1, tzinfo=utc))
    assert hawaii.utcoffset(None) == dt.timedelta(hours=-10)


def test_tides_use_station_time_not_service_time(fake_net):
    """Service in UTC, 03:00 UTC on the 29th = 20:00 PDT on the 28th at Seattle."""
    from zeprint.labels import RenderContext
    from zeprint.labels.tides import TidesLabel, TidesParams
    from zeprint.zpl import get_size
    now = dt.datetime(2026, 9, 29, 3, 0, tzinfo=dt.timezone.utc)
    ctx = RenderContext(get_size("4x6"), tz=dt.timezone.utc, now=now)
    r = TidesLabel().render(TidesParams(), ctx)
    assert r.data["date"] == "2026-09-28"
    assert "^FDNOW 8:00 PM" in r.zpl
    assert r.data["next_event"] is None or r.data["next_event"]["time"] >= "2026-09-28T20:00"


# ------------------------------------------------------------------ API edges

def test_patch_default_false_hands_default_on(client):
    client.post("/api/printers", json={"id": "b", "name": "B", "uri": "10.0.0.2"})
    r = client.patch("/api/printers/zebra", json={"default": False})
    assert r.json()["default"] is False
    assert client.get("/api/settings").json()["default_printer"] == "b"
    # omitting "default" leaves it alone
    client.patch("/api/printers/b", json={"darkness": 10})
    assert client.get("/api/settings").json()["default_printer"] == "b"


def test_post_default_string_false_is_false(client):
    r = client.post("/api/printers", json={"id": "c", "name": "C", "uri": "10.0.0.3",
                                           "default": "false"})
    assert r.status_code == 201 and r.json()["default"] is False
    assert client.post("/api/printers", json={"id": "d", "name": "D", "uri": "10.0.0.4",
                                              "default": "maybe"}).status_code == 422


def test_raw_body_limit(client):
    r = client.post("/api/printers/zebra/raw", content=b"x",
                    headers={"Content-Length": str(64 << 20)})
    assert r.status_code == 413


# ----------------------------------------------------------------------- jobs

def test_history_never_evicts_unfinished_jobs():
    q = JobQueue(EventBus(), history=2)          # worker not started: jobs stay queued
    jobs = [q.submit(Job(printer="p", title=str(i)), lambda progress: 0) for i in range(4)]
    assert [j.id for j in q.list()] == [j.id for j in reversed(jobs)]
    q.start()
    q.wait(jobs[-1].id, 5)
    q.submit(Job(printer="p", title="new"), lambda progress: 0)
    q.stop()
    assert len(q.list()) <= 3


def test_queued_job_survives_printer_rename(svc, printer):
    gate = threading.Event()
    blocker = svc.jobs.submit(Job(printer="zebra", title="gate"), lambda p: (gate.wait(5), 0)[1])
    job = svc.print_raw(b"^XA^FDafter-rename^FS^XZ", "zebra")
    from zeprint.config import PrinterConfig
    current = svc.printer("zebra").model_dump()
    svc.save_printer(PrinterConfig(**{**current, "id": "gx"}), replace_id="zebra")
    gate.set()
    svc.jobs.wait(blocker.id, 5)
    assert svc.jobs.wait(job.id, 10).status == "done"
    assert printer.wait_for(1)[-1] == b"^XA^FDafter-rename^FS^XZ"


# ----------------------------------------------------------------------- mqtt

@pytest.fixture
def bridge(svc):
    from test_mqtt import FakeClient
    from zeprint.integrations.mqtt import MqttBridge
    b = MqttBridge(svc, "localhost", status_interval=0)
    b.client = FakeClient()
    return b


@pytest.mark.parametrize("payload,match", [
    (b'{"copies": 5000}', "copies"),
    (b'{"copies": null}', "copies"),
    (b'{"params": "nope"}', "params"),
])
def test_mqtt_payloads_validated_like_the_api(bridge, svc, payload, match):
    before = len(svc.jobs.list())
    bridge.handle("zeprint/print/test", payload)
    assert len(svc.jobs.list()) == before
    assert match in json.loads(bridge.client.last("zeprint/error"))["error"]


def test_mqtt_prunes_stale_retained_entities(bridge):
    stale = "homeassistant/sensor/zeprint/printer_oldone/config"
    bridge.handle(stale, b'{"name": "old"}')
    assert bridge.client.last(stale) == ""
    live = "homeassistant/sensor/zeprint/printer_zebra/config"
    bridge.handle(live, b'{"name": "zebra"}')
    assert live not in bridge.client.topics()
    bridge.handle(stale, b"")                      # removal echo: ignored
    assert bridge.client.topics().count(stale) == 1


@pytest.mark.parametrize("width", [8, 382, 403, 406])
def test_raster_keeps_the_rightmost_columns(width):
    """Images whose width isn't a whole byte kept losing up to 7 columns on the
    right: the last glyph of an address line, a shipping label's border."""
    import numpy as np
    from PIL import Image
    from zeprint.zpl import render_all
    img = Image.new("L", (width, 6), 255)
    img.paste(0, (width - 3, 0, width, 6))           # a 3-dot stripe on the right edge
    z = ZPL("2x1", 203)
    z.image(None, 0, img)
    zpl = z.build()
    x = int(re.search(r"\^FO(-?\d+),", zpl).group(1))
    assert x >= 0 and x + width <= 406                 # on the label, whole
    out = np.asarray(render_all(zpl))
    assert (out[:6, x + width - 3:x + width] == 0).all()


def test_tides_next_event_after_the_days_last_tide(fake_net):
    """At 23:30 the day's highs and lows are all past; the next one is tomorrow's."""
    from zeprint.labels import RenderContext
    from zeprint.labels.tides import TidesLabel, TidesParams
    from zeprint.zpl import get_size
    now = dt.datetime(2026, 9, 28, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=-7)))
    r = TidesLabel().render(TidesParams(), RenderContext(get_size("2x1"), now=now))
    assert r.data["date"] == "2026-09-28"
    assert r.data["next_event"]["time"].startswith("2026-09-29")
    assert "^FDNext " in r.zpl and " Tue " in r.zpl                 # labelled with its day
    assert all(e["time"].startswith("2026-09-28") for e in r.data["events"])


def test_weather_2x1_hours_ahead_and_long_headline(fake_net, monkeypatch):
    """2x1 ignored hours_ahead, and a long place + condition ran off the right edge."""
    import numpy as np
    from zeprint.labels import RenderContext
    from zeprint.labels import weather
    from zeprint.zpl import get_size, render_all
    ctx = lambda: RenderContext(get_size("2x1"), now=NOW)
    today = weather.WeatherLabel().render(weather.WeatherParams(), ctx()).zpl
    ahead = weather.WeatherLabel().render(weather.WeatherParams(hours_ahead=48), ctx()).zpl
    assert "wind (kn), next 48 h" in ahead and ahead != today.replace("today", "next 48 h")
    monkeypatch.setitem(weather.WMO, 2, "Thunderstorm w/ heavy hail")
    r = weather.WeatherLabel().render(weather.WeatherParams(home_name="Port Townsend WA"), ctx())
    headline = np.asarray(render_all(r.zpl))[:44]
    assert (headline[:, 600 - 10:] == 255).all()                  # right margin stays clear


def test_cli_uri_uses_the_configured_printer_settings(tmp_path, printer, monkeypatch):
    """--uri kept dpi and size but printed direct-thermal at default darkness/speed."""
    from zeprint.__main__ import main
    for k, v in {"ZEPRINT_PRINTER_URI": "tcp://10.9.9.9:9100", "ZEPRINT_MEDIA": "ribbon",
                 "ZEPRINT_DARKNESS": "5", "ZEPRINT_SPEED": "4",
                 "ZEPRINT_DATA_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    assert main(["--data-dir", str(tmp_path), "print", "test", "--uri", printer.uri]) == 0
    sent = printer.wait_for(1)[0]
    assert b"^MTT" in sent and b"~SD05" in sent and b"^PR4" in sent
    for bad in ("0", "5000", "two"):
        with pytest.raises(SystemExit):
            main(["--data-dir", str(tmp_path), "print", "test", "--copies", bad])


def test_mqtt_ignores_retained_commands(bridge, svc):
    """A retained print command would be replayed, and reprinted, on every reconnect."""
    from types import SimpleNamespace
    before = len(svc.jobs.list())
    for topic in ("zeprint/print/test", "zeprint/print", "zeprint/raw/zebra"):
        payload = b'{"label": "test"}' if topic == "zeprint/print" else b"PRESS"
        bridge._on_message(None, None, SimpleNamespace(topic=topic, payload=payload, retain=True))
    assert len(svc.jobs.list()) == before
    bridge._on_message(None, None, SimpleNamespace(topic="homeassistant/status",
                                                   payload=b"online", retain=True))
    assert bridge.client.topics()                  # retained HA status still re-announces


def test_pdf_page_count_waits_for_the_render_lock():
    """pdfium isn't thread-safe: counting an upload's pages mid-render corrupted both."""
    from fakes import letter_pdf_with_label
    from zeprint.labels.base import _DRAW_LOCK
    from zeprint.uploads import pdf_page_count
    pdf = letter_pdf_with_label(2)
    held, release, counted = threading.Event(), threading.Event(), threading.Event()

    def render():
        with _DRAW_LOCK:
            held.set()
            release.wait(5)
    threading.Thread(target=render, daemon=True).start()
    assert held.wait(5)
    threading.Thread(target=lambda: pdf_page_count(pdf) == 2 and counted.set(),
                     daemon=True).start()
    assert not counted.wait(0.3)                  # waits while a render holds pdfium
    release.set()
    assert counted.wait(5)
