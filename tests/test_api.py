import json

import pytest
from fastapi.testclient import TestClient

from conftest import NOW
from zeprint.api import create_app
from zeprint.config import ConfigStore


def test_health_and_info(client):
    assert client.get("/api/health").json()["status"] == "ok"
    info = client.get("/api/info").json()
    assert info["default_printer"] == "zebra" and info["timezone"] == "America/Los_Angeles"
    assert {s["id"] for s in info["sizes"]} == {"4x6", "2x1"}


def test_web_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "ZePrint" in r.text
    assert client.get("/app.js").status_code == 200


def test_labels_listing(client):
    labels = {l["id"]: l for l in client.get("/api/labels").json()}
    assert labels["tides"]["effective_defaults"]["station"] == "9447130"
    assert "station" in labels["tides"]["schema"]["properties"]


def test_print_flat_params_and_wait(client, printer):
    r = client.post("/api/labels/tides/print?wait=30",
                    json={"station": "9446484", "size": "2x1", "copies": 2})
    job = r.json()
    assert r.status_code == 200 and job["status"] == "done", job
    assert job["params"]["station"] == "9446484" and job["size"] == "2x1"
    data = printer.wait_for(1)[0]
    assert b"^PW600" in data and b"^PQ2" in data


def test_print_nested_params(client, printer):
    r = client.post("/api/labels/test/print?wait=10", json={"params": {"title": "Hi"}})
    assert r.json()["status"] == "done"
    assert b"^FDHi^FS" in printer.wait_for(1)[0]


def test_print_validation_errors(client):
    assert client.post("/api/labels/tides/print", json={"station": "12"}).status_code == 422
    r = client.post("/api/labels/tides/print", json={"bogus": 1})
    assert r.status_code == 422 and "bogus" in r.json()["detail"]
    assert client.post("/api/labels/nope/print").status_code == 404
    assert client.post("/api/labels/tides/print", json={"size": "3x5"}).status_code == 422
    assert client.post("/api/labels/tides/print", json={"printer": "ghost"}).status_code == 404


def test_preview_render_and_get_preview(client):
    r = client.post("/api/labels/weather/preview", json={"size": "2x1"})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    r = client.get("/api/labels/tides/preview.png?size=2x1&station=9446484&dpi=203")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    out = client.post("/api/labels/weather/render?preview=true", json={}).json()
    assert out["zpl"].startswith("~SD") and out["report"].startswith("# Weather")
    assert out["preview_png"] and out["data"]["location"] == "Seattle"


def test_label_defaults_apply(client, printer):
    assert client.put("/api/labels/tides/defaults", json={"station": "9446484"}).status_code == 200
    assert client.get("/api/labels/tides").json()["effective_defaults"]["station"] == "9446484"
    job = client.post("/api/labels/tides/print?wait=30").json()
    assert job["params"]["station"] == "9446484"
    assert client.put("/api/labels/tides/defaults", json={"station": "x"}).status_code == 422
    assert client.put("/api/labels/tides/defaults", json={"nope": 1}).status_code == 422


def test_printer_crud_and_default(client):
    r = client.post("/api/printers", json={"id": "garage", "name": "Garage",
                                           "uri": "10.0.0.9", "dpi": 203, "label_size": "1x2"})
    assert r.status_code == 201 and r.json()["label_size"] == "2x1"
    assert client.post("/api/printers", json={"id": "garage", "name": "x",
                                              "uri": "10.0.0.9"}).status_code == 422
    assert client.post("/api/printers", json={"id": "Bad Id", "name": "x",
                                              "uri": "10.0.0.9"}).status_code == 422
    assert client.post("/api/printers", json={"id": "u", "name": "x",
                                              "uri": "ftp://x"}).status_code == 422
    r = client.patch("/api/printers/garage", json={"darkness": 25, "default": True})
    assert r.json()["darkness"] == 25 and r.json()["default"] is True
    assert client.get("/api/settings").json()["default_printer"] == "garage"
    assert client.delete("/api/printers/garage").status_code == 204
    assert client.get("/api/settings").json()["default_printer"] == "zebra"
    assert client.delete("/api/printers/garage").status_code == 404


def test_rename_printer_keeps_default(client):
    r = client.patch("/api/printers/zebra", json={"id": "gx430t", "name": "GX"})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["default_printer"] == "gx430t"


def test_settings_timezone(client):
    assert client.patch("/api/settings", json={"timezone": "Mars/Base"}).status_code == 422
    s = client.patch("/api/settings", json={"timezone": "Europe/Oslo"}).json()
    assert s["effective_timezone"] == "Europe/Oslo"
    s = client.patch("/api/settings", json={"timezone": ""}).json()
    assert s["timezone"] is None and s["effective_timezone"] == "America/Los_Angeles"


def test_printer_actions(client, printer):
    assert client.get("/api/printers/zebra/status").json()["state"] == "ready"
    assert client.post("/api/printers/zebra/calibrate?wait=10").json()["status"] == "done"
    assert client.post("/api/printers/zebra/raw?wait=10", content=b"^XA^FDraw^FS^XZ").json()["status"] == "done"
    got = printer.wait_for(2)
    assert b"~JC" in got[0] and got[1] == b"^XA^FDraw^FS^XZ"


def test_job_error_when_printer_down(client):
    client.post("/api/printers", json={"id": "dead", "name": "Dead", "uri": "tcp://127.0.0.1:9"})
    r = client.post("/api/labels/test/print?wait=20", json={"printer": "dead"})
    assert r.status_code == 502 and r.json()["status"] == "error"
    assert "could not reach" in r.json()["error"]
    jobs = client.get("/api/jobs").json()
    assert jobs[0]["status"] == "error"
    assert client.get(f"/api/jobs/{jobs[0]['id']}").json()["id"] == jobs[0]["id"]
    assert client.get("/api/printers/dead/status").json()["state"] == "offline"


def test_zpl_preview(client):
    r = client.post("/api/zpl/preview?size=2x1&dpi=203", content=b"^XA^FO10,10^GB50,50,50^FS^XZ")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert client.post("/api/zpl/preview", content=b"hello").status_code == 422
    assert client.post("/api/zpl/preview?dpi=250", content=b"^XA^XZ").status_code == 422


def test_token_auth(svc):
    with TestClient(create_app(svc, token="s3cret", start_integrations=False)) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/printers").status_code == 401
        assert c.get("/api/printers", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/api/printers", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/printers?token=s3cret").status_code == 200
        assert c.get("/").status_code == 200           # the UI shell itself is public


def test_no_printer_configured(tmp_path, fake_net):
    from zeprint.service import ZePrint
    svc = ZePrint(tmp_path, env={}, plugin_dir=tmp_path / "none")
    svc.clock = lambda: NOW
    with TestClient(create_app(svc, token="", start_integrations=False)) as c:
        r = c.post("/api/labels/test/print")
        assert r.status_code == 422 and "no printer" in r.json()["detail"]
        # previews still work (4x6 @ 300 dpi)
        assert c.post("/api/labels/test/preview").status_code == 200


def test_config_seed_and_corrupt_backup(tmp_path):
    env = {"ZEPRINT_PRINTER_URI": "tcp://10.1.1.1", "ZEPRINT_PRINTER_DPI": "203",
           "ZEPRINT_LABEL_SIZE": "1x2", "TZ": "Nowhere/Land"}
    store = ConfigStore(tmp_path / "config.json", env)
    p = store.settings.printer()
    assert (p.id, p.dpi, p.label_size) == ("zebra", 203, "2x1")
    assert str(store.timezone()) == "UTC"               # bad TZ ignored
    # env only seeds the first start
    store2 = ConfigStore(tmp_path / "config.json", {"ZEPRINT_PRINTER_URI": "tcp://9.9.9.9"})
    assert store2.settings.printer().uri == "tcp://10.1.1.1"
    (tmp_path / "config.json").write_text("{not json")
    store3 = ConfigStore(tmp_path / "config.json", {})
    assert store3.settings.printers == []
    assert list(tmp_path.glob("config.json.bad-*"))
    assert json.loads((tmp_path / "config.json").read_text())["printers"] == []
