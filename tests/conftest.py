import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from fake_printer import FakePrinter
from fakes import FakeNet
from zeprint import net

TZ = ZoneInfo("America/Los_Angeles")
NOW = dt.datetime(2026, 9, 28, 10, 30, tzinfo=TZ)


@pytest.fixture
def fake_net(monkeypatch):
    fake = FakeNet(NOW)
    monkeypatch.setattr(net, "fetch", fake)
    net.cache.clear()
    return fake


@pytest.fixture
def printer():
    fp = FakePrinter()
    yield fp
    fp.close()


@pytest.fixture
def svc(tmp_path, printer, fake_net):
    from zeprint.service import ZePrint
    s = ZePrint(tmp_path / "data", env={"ZEPRINT_PRINTER_URI": printer.uri,
                                        "TZ": "America/Los_Angeles"},
                plugin_dir=tmp_path / "no-plugins")
    s.clock = lambda: NOW
    s.start()
    yield s
    s.stop()


@pytest.fixture
def client(svc):
    from fastapi.testclient import TestClient
    from zeprint.api import create_app
    with TestClient(create_app(svc, token="", start_integrations=False)) as c:
        yield c
