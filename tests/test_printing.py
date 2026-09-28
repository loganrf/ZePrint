import http.server
import struct
import threading

import pytest

from fake_printer import HS_HEAD_OPEN, HS_READY, HI
from zeprint.errors import PrinterError
from zeprint.printing import (DeviceTransport, IppTransport, TcpTransport,
                              parse_host_identification, parse_host_status, parse_uri)
from zeprint.printing.ipp import encode_print_job


@pytest.mark.parametrize("uri,expect", [
    ("tcp://10.0.0.5:9100", TcpTransport("10.0.0.5", 9100)),
    ("10.0.0.5", TcpTransport("10.0.0.5", 9100)),
    ("zebra.local:6101", TcpTransport("zebra.local", 6101)),
    ("usb:///dev/usb/lp0", DeviceTransport("/dev/usb/lp0")),
    ("/dev/usb/lp1", DeviceTransport("/dev/usb/lp1")),
    ("cups://mac.local/GX430t", IppTransport("http://mac.local:631/printers/GX430t",
                                             "ipp://mac.local:631/printers/GX430t")),
    ("ipps://h:443/printers/Q?insecure", IppTransport("https://h:443/printers/Q",
                                                      "ipps://h:443/printers/Q", False)),
])
def test_parse_uri(uri, expect):
    assert parse_uri(uri) == expect


@pytest.mark.parametrize("uri", ["", "ftp://x", "usb://printer", "ipp://host", "cups://host",
                                 "tcp://:9100", "tcp://h:notaport"])
def test_parse_uri_rejects(uri):
    with pytest.raises(ValueError):
        parse_uri(uri)


def test_host_status():
    s = parse_host_status(HS_READY)
    assert s["state"] == "ready" and s["label_length_dots"] == 1800
    assert parse_host_status(HS_HEAD_OPEN)["state"] == "head_open"
    ribbon = HS_READY.replace(b"000,0,0,0,0,2", b"000,0,0,1,0,2")
    assert parse_host_status(ribbon, "direct")["state"] == "ready"      # no ribbon in DT mode
    assert parse_host_status(ribbon, "ribbon")["state"] == "ribbon_out"
    with pytest.raises(ValueError):
        parse_host_status(b"\x02garbage\x03")


def test_host_identification():
    assert parse_host_identification(HI) == {
        "model": "GX430t-300dpi", "firmware": "V61.17.17Z", "dots_per_mm": 12, "dpi": 300,
        "memory": "8176KB"}


def test_tcp_send_and_query(printer):
    t = parse_uri(printer.uri)
    t.send(b"^XA^XZ")
    assert printer.wait_for(1)[0] == b"^XA^XZ"
    assert parse_host_status(t.query(b"~HS", frames=3))["state"] == "ready"


def test_tcp_unreachable():
    with pytest.raises(PrinterError, match="could not reach"):
        TcpTransport("127.0.0.1", 9, timeout=2).send(b"x")


def test_device_transport(tmp_path):
    dev = tmp_path / "lp0"
    dev.write_bytes(b"")
    DeviceTransport(str(dev)).send(b"^XA^XZ")
    assert dev.read_bytes() == b"^XA^XZ"
    with pytest.raises(PrinterError, match="--device"):
        DeviceTransport(str(tmp_path / "missing")).send(b"x")


def test_ipp_encoding():
    body = encode_print_job("ipp://h:631/printers/Q", "job", b"^XA^XZ", request_id=7)
    assert body[:8] == struct.pack(">BBHI", 1, 1, 2, 7)
    assert b"application/vnd.cups-raw" in body and b"ipp://h:631/printers/Q" in body
    assert body.endswith(b"\x03^XA^XZ")


class _Cups(http.server.BaseHTTPRequestHandler):
    status = 0x0000
    last = {}

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Cups.last = {"path": self.path, "type": self.headers["Content-Type"], "body": body}
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.end_headers()
        self.wfile.write(struct.pack(">BBHI", 1, 1, _Cups.status, 1) + b"\x03")

    def log_message(self, *a):
        pass


@pytest.fixture
def cups():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Cups)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_ipp_print(cups):
    _Cups.status = 0x0000
    t = parse_uri(f"cups://127.0.0.1:{cups.server_port}/GX430t")
    t.send(b"^XA^XZ", "hello")
    assert _Cups.last["path"] == "/printers/GX430t"
    assert _Cups.last["type"] == "application/ipp"
    assert _Cups.last["body"].endswith(b"^XA^XZ")


def test_ipp_refused(cups):
    _Cups.status = 0x0506
    with pytest.raises(PrinterError, match="not-accepting-jobs"):
        parse_uri(f"ipp://127.0.0.1:{cups.server_port}/printers/Q").send(b"x")
