import datetime as dt
import re

import pytest

from conftest import NOW, TZ
from fakes import ISS_EPOCH_NOW, ISS_TLE
from zeprint import labels
from zeprint.errors import FetchError, LabelError
from zeprint.labels import RenderContext
from zeprint.zpl import get_size, render_preview

CASES = [(cls, size, dpi) for cls in labels.all_labels() for size in cls.sizes
         for dpi in (203, 300, 600)]


def ctx_for(cls_id, size="4x6", dpi=300, tmp_path=None):
    now = ISS_EPOCH_NOW if cls_id == "satellite" else NOW
    return RenderContext(get_size(size), dpi=dpi, tz=TZ, now=now, cache_dir=tmp_path,
                         printer_name="Test Zebra")


@pytest.mark.parametrize("cls,size,dpi", CASES,
                         ids=[f"{c.id}-{s}-{d}" for c, s, d in CASES])
def test_every_label_renders_in_bounds(cls, size, dpi, fake_net, tmp_path):
    result = cls().render(cls.Params(), ctx_for(cls.id, size, dpi, tmp_path))
    pw, ll = get_size(size).dots(dpi)
    assert f"^PW{pw}" in result.zpl and f"^LL{ll}" in result.zpl
    for x, y in re.findall(r"\^FO(\d+),(\d+)", result.zpl):
        assert 0 <= int(x) < pw and 0 <= int(y) < ll, (x, y)
    assert render_preview(result.zpl).size == (pw, ll)


def test_builtin_labels_registered():
    ids = {c.id for c in labels.all_labels()}
    assert {"satellite", "tides", "topo", "weather", "test"} <= ids


# ------------------------------------------------------------------ satellite

def test_tle_checksum_and_parse():
    from zeprint.labels.satellite import parse_tle, valid_tle_line
    name, l1, l2 = parse_tle(ISS_TLE)
    assert name == "ISS (ZARYA)" and valid_tle_line(l1) and valid_tle_line(l2)
    corrupted = ISS_TLE.replace("51.6416", "51.6417")
    with pytest.raises(LabelError, match="checksum"):
        parse_tle(corrupted)
    with pytest.raises(LabelError):
        parse_tle("nothing here")


def test_satellite_caches_tle(fake_net, tmp_path):
    from zeprint.labels.satellite import SatelliteLabel, SatelliteParams
    lbl = SatelliteLabel()
    r = lbl.render(SatelliteParams(), ctx_for("satellite", tmp_path=tmp_path))
    assert r.data["name"] == "ISS (ZARYA)" and "Launch site: TYMSC" in r.report
    gp = [u for u in fake_net.urls if "/NORAD/elements/" in u]
    lbl.render(SatelliteParams(), ctx_for("satellite", tmp_path=tmp_path))
    assert len([u for u in fake_net.urls if "/NORAD/elements/" in u]) == len(gp) == 1


def test_satellite_falls_back_to_stale_cache(fake_net, tmp_path, monkeypatch):
    import os
    from zeprint.labels.satellite import SatelliteLabel, SatelliteParams
    SatelliteLabel().render(SatelliteParams(), ctx_for("satellite", tmp_path=tmp_path))
    cache = tmp_path / "celestrak" / "25544.tle"
    os.utime(cache, (0, 0))                                 # make it stale
    fake_net.fail = ("celestrak.org",)
    r = SatelliteLabel().render(SatelliteParams(), ctx_for("satellite", tmp_path=tmp_path))
    assert r.data["norad"] == "25544"


def test_satellite_without_network_or_cache(fake_net, tmp_path):
    from zeprint.labels.satellite import SatelliteLabel, SatelliteParams
    fake_net.fail = ("celestrak.org",)
    with pytest.raises(FetchError, match="tle"):
        SatelliteLabel().render(SatelliteParams(), ctx_for("satellite", tmp_path=tmp_path))
    r = SatelliteLabel().render(SatelliteParams(tle=ISS_TLE), ctx_for("satellite", tmp_path=tmp_path))
    assert r.data["name"] == "ISS (ZARYA)"


# ---------------------------------------------------------------------- tides

def test_tides_data(fake_net):
    from zeprint.labels.tides import TidesLabel, TidesParams
    r = TidesLabel().render(TidesParams(), ctx_for("tides"))
    assert r.data["name"] == "Seattle" and r.data["level_source"] == "observed"
    assert r.data["next_event"]["time"] >= NOW.replace(tzinfo=None).isoformat()
    assert {e["type"] for e in r.data["events"]} == {"high", "low"}


def test_tides_bad_station(fake_net, monkeypatch):
    import json
    from zeprint.labels.tides import TidesLabel, TidesParams
    monkeypatch.setattr("zeprint.net.fetch",
                        lambda url, **kw: json.dumps({"error": {"message": "No data"}}).encode())
    with pytest.raises(LabelError, match="No data"):
        TidesLabel().render(TidesParams(station="1234567"), ctx_for("tides"))


def test_sun_and_moon():
    from zeprint.labels.tides import moon_phase, sun_times
    rise, set_ = sun_times(dt.date(2026, 6, 21), 47.6, -122.3, TZ)
    assert rise.hour == 5 and set_.hour == 21              # Seattle, solstice
    frac, illum, name = moon_phase(dt.date(2026, 1, 3))    # full moon
    assert name == "Full" and illum > 95


# ----------------------------------------------------------------------- topo

def test_topo_validation():
    from pydantic import ValidationError
    from zeprint.labels.topo import TopoParams
    assert TopoParams(corner1="46.900, -121.8").corner1 == "46.9,-121.8"
    with pytest.raises(ValidationError):
        TopoParams(corner1="91,0")
    with pytest.raises(ValidationError):
        TopoParams(corner1="banana")


def test_topo_corners_too_close(fake_net):
    from zeprint.labels.topo import TopoLabel, TopoParams
    with pytest.raises(LabelError, match="too close"):
        TopoLabel().render(TopoParams(corner1="46.9,-121.8", corner2="46.9,-121.8"),
                           ctx_for("topo"))


def test_topo_interval_and_data(fake_net):
    from zeprint.labels.topo import TopoLabel, TopoParams
    r = TopoLabel().render(TopoParams(units="feet", interval=500), ctx_for("topo"))
    assert r.data["interval"] == 500 and r.data["units"] == "feet"
    assert "caltopo.com" in r.data["caltopo_url"]


def test_smooth_matches_shape_and_handles_nan():
    import numpy as np
    from zeprint.labels.topo import smooth
    g = np.arange(100, dtype=float).reshape(10, 10)
    g[3, 3] = np.nan
    out = smooth(g)
    assert out.shape == g.shape and np.isnan(out[3, 3]) and not np.isnan(out[5, 5])


# -------------------------------------------------------------------- weather

def test_weather_report_and_options(fake_net):
    from zeprint.labels.weather import WeatherLabel, WeatherParams
    r = WeatherLabel().render(WeatherParams(home_name="Juanita", hours_ahead=12,
                                            wind_unit="mph", temp_unit="celsius"),
                              ctx_for("weather"))
    assert r.report.startswith("# Weather - Juanita")
    assert r.data["wind_unit"] == "mph" and "WEATHER - JUANITA" in r.zpl
    assert r.data["marina_wind"]["max_gust"] > 0


# -------------------------------------------------------------------- plugins

def test_plugin_directory(tmp_path):
    (tmp_path / "hello.py").write_text('''
from pydantic import BaseModel
from zeprint.labels import Label, RenderResult, register

class P(BaseModel):
    who: str = "world"

@register
class Hello(Label):
    id = "hello-test"
    name = "Hello"
    sizes = ("2x1",)
    Params = P

    def render(self, p, ctx):
        z = ctx.zpl()
        z.text(10, 10, 40, "hi " + p.who)
        return RenderResult(z.build(), title="hello")
''')
    (tmp_path / "broken.py").write_text("raise RuntimeError('nope')")
    loaded = labels.load_plugins(tmp_path)
    assert loaded == ["hello.py"]
    cls = labels.get("hello-test")
    r = cls().render(cls.Params(who="Logan"), RenderContext(get_size("2x1"), dpi=203))
    assert "^FDhi Logan" in r.zpl
