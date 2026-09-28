"""
Offline stand-ins for the label data sources (NOAA CO-OPS, CelesTrak, Open-Meteo,
AWS Terrain Tiles), so labels render deterministically without a network.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import math
import re
import urllib.parse
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image

from zeprint.errors import FetchError

# The canonical ISS element set from the TLE format documentation (valid checksums).
ISS_TLE = """ISS (ZARYA)
1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927
2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537
"""
ISS_EPOCH_NOW = dt.datetime(2008, 9, 20, 18, 0, tzinfo=dt.timezone.utc)

SATCAT = [{"OBJECT_NAME": "ISS (ZARYA)", "OBJECT_ID": "1998-067A", "NORAD_CAT_ID": 25544,
           "OBJECT_TYPE": "PAY", "OPS_STATUS_CODE": "+", "OWNER": "ISS",
           "LAUNCH_DATE": "1998-11-20", "LAUNCH_SITE": "TYMSC", "DECAY_DATE": None}]


def _tide(t_hours: float) -> float:
    return (6.2 + 4.1 * math.cos(2 * math.pi * (t_hours - 4.0) / 12.42)
            + 2.3 * math.cos(2 * math.pi * (t_hours - 1.0) / 24.84))


def noaa_predictions(date: dt.date, interval: str):
    base = dt.datetime(date.year, date.month, date.day)
    if interval == "6":
        rows = [{"t": (base + dt.timedelta(minutes=6 * i)).strftime("%Y-%m-%d %H:%M"),
                 "v": f"{_tide(i / 10):.3f}"} for i in range(240)]
        return {"predictions": rows}
    fine = [(i / 60, _tide(i / 60)) for i in range(0, 24 * 60)]
    out = []
    for k in range(1, len(fine) - 1):
        (_, a), (h, b), (_, c) = fine[k - 1], fine[k], fine[k + 1]
        if (b > a and b >= c) or (b < a and b <= c):
            out.append({"t": (base + dt.timedelta(hours=h)).strftime("%Y-%m-%d %H:%M"),
                        "v": f"{b:.3f}", "type": "H" if b > a else "L"})
    return {"predictions": out}


def open_meteo(qs: dict, now: dt.datetime) -> dict:
    days = int(qs.get("forecast_days", ["4"])[0])
    start = dt.date(now.year, now.month, now.day)
    d = {k: [] for k in ["time", "weather_code", "temperature_2m_max", "temperature_2m_min",
                         "precipitation_sum", "precipitation_probability_max",
                         "wind_speed_10m_max", "wind_gusts_10m_max",
                         "wind_direction_10m_dominant", "sunrise", "sunset", "uv_index_max"]}
    codes = [2, 61, 3, 80, 0, 1, 95]
    for i in range(days):
        day = start + dt.timedelta(days=i)
        d["time"].append(day.isoformat())
        d["weather_code"].append(codes[i % len(codes)])
        d["temperature_2m_max"].append(68 - i * 2.5)
        d["temperature_2m_min"].append(51 - i)
        d["precipitation_sum"].append(0.04 * i)
        d["precipitation_probability_max"].append(10 + 15 * i)
        d["wind_speed_10m_max"].append(12 + 3 * i)
        d["wind_gusts_10m_max"].append(19 + 4 * i)
        d["wind_direction_10m_dominant"].append(200 + 20 * i)
        d["sunrise"].append(f"{day.isoformat()}T06:58")
        d["sunset"].append(f"{day.isoformat()}T18:51")
        d["uv_index_max"].append(4.5)
    out = {"latitude": float(qs["latitude"][0]), "longitude": float(qs["longitude"][0]),
           "timezone": "America/Los_Angeles", "timezone_abbreviation": "PDT",
           "current": {"time": f"{start.isoformat()}T{now:%H}:00", "temperature_2m": 61.3,
                       "relative_humidity_2m": 72, "apparent_temperature": 59.8, "is_day": 1,
                       "precipitation": 0.0, "weather_code": 2, "wind_speed_10m": 9.4,
                       "wind_direction_10m": 215, "wind_gusts_10m": 16.2},
           "daily": d}
    if "hourly" in qs:
        hrs = [dt.datetime(start.year, start.month, start.day) + dt.timedelta(hours=h)
               for h in range(24 * days)]
        out["hourly"] = {
            "time": [t.strftime("%Y-%m-%dT%H:%M") for t in hrs],
            "wind_speed_10m": [round(8 + 7 * math.sin(math.pi * (h % 24 - 6) / 14) ** 2, 1)
                               for h in range(len(hrs))],
            "wind_gusts_10m": [round(13 + 11 * math.sin(math.pi * (h % 24 - 5) / 14) ** 2, 1)
                               for h in range(len(hrs))],
            "wind_direction_10m": [(190 + 7 * h) % 360 for h in range(len(hrs))],
        }
    return out


def terrarium_tile(z: int, x: int, y: int) -> bytes:
    """A synthetic volcano: elevation from distance to a peak near Mt Rainier."""
    n = 2 ** z
    px = (x + (np.arange(256) + 0.5) / 256) / n * 360 - 180
    py = y + (np.arange(256) + 0.5) / 256
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * py / n))))
    LON, LAT = np.meshgrid(px, lat)
    dist = np.hypot((LON + 121.76) * 0.68, LAT - 46.853)
    elev = 600 + 3800 * np.exp(-(dist / 0.045) ** 1.3) + 90 * np.sin(LON * 180) * np.cos(LAT * 140)
    v = elev + 32768
    r = np.floor(v / 256)
    g = np.floor(v - r * 256)
    b = np.floor((v - r * 256 - g) * 256)
    rgb = np.stack([r, g, b], axis=-1).clip(0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG")
    return buf.getvalue()


class FakeNet:
    """Callable replacement for ``zeprint.net.fetch``; records the URLs it saw."""

    def __init__(self, now: dt.datetime, fail: tuple[str, ...] = ()):
        # Open-Meteo (timezone=auto) starts its series at local midnight
        self.now = now.astimezone(ZoneInfo("America/Los_Angeles")) if now.tzinfo else now
        self.fail = fail
        self.urls: list[str] = []

    def __call__(self, url: str, **kw) -> bytes:
        self.urls.append(url)
        host = urllib.parse.urlsplit(url).hostname or ""
        if any(f in host for f in self.fail):
            raise FetchError(f"could not reach {host} (test)")
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "tidesandcurrents" in host:
            if "/mdapi/" in url:
                return json.dumps({"stations": [{"name": "Seattle", "lat": 47.6026,
                                                 "lng": -122.3393}]}).encode()
            date = dt.datetime.strptime(qs["begin_date"][0], "%Y%m%d").date()
            if qs["product"][0] == "water_level":
                return json.dumps({"data": [{"t": f"{date} 10:24", "v": "7.412"}]}).encode()
            return json.dumps(noaa_predictions(date, qs["interval"][0])).encode()
        if host == "celestrak.org":
            if "/NORAD/elements/" in url:
                return ISS_TLE.encode()
            return json.dumps(SATCAT).encode()
        if host == "api.open-meteo.com":
            return json.dumps(open_meteo(qs, self.now)).encode()
        m = re.search(r"/terrarium/(\d+)/(\d+)/(\d+)\.png", url)
        if m:
            return terrarium_tile(*map(int, m.groups()))
        raise FetchError(f"unexpected URL in test: {url}")
