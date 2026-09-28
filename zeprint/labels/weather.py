"""
Weather label (port of weather_report.py): a home weather report plus a plot of
the day's predicted wind at a marina.

Data: Open-Meteo (https://open-meteo.com), free, keyless. Two lookups:
  - home:   current conditions + today + a multi-day outlook
  - marina: hourly wind speed / gusts / direction, plotted in mono for the label

Wind defaults to knots and temperature to Fahrenheit (sailing context). Times are
in the location's own timezone (Open-Meteo ``timezone=auto``).
"""

from __future__ import annotations

import datetime as dt
import math
import urllib.parse
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from .. import net
from ..errors import LabelError
from . import Label, RenderContext, RenderResult, register
from ._text import font
from .topo import parse_latlon

API = "https://api.open-meteo.com/v1/forecast"

WMO = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Light rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ light hail", 99: "Thunderstorm w/ heavy hail",
}
WIND_LABEL = {"kn": "kn", "mph": "mph", "kmh": "km/h", "ms": "m/s"}


class WeatherParams(BaseModel):
    home: str = Field("47.7076,-122.2054", title="Home", description="Home location as LAT,LON")
    home_name: str = Field("Kirkland", max_length=24, title="Home name", description="Name printed in the title")
    marina: str = Field("47.8115,-122.3870", title="Marina", description="Wind-plot location as LAT,LON")
    marina_name: str = Field("Port of Edmonds", max_length=32, title="Marina name")
    wind_unit: Literal["kn", "mph", "kmh", "ms"] = Field("kn", title="Wind unit")
    temp_unit: Literal["fahrenheit", "celsius"] = Field("fahrenheit", title="Temperature unit")
    days: int = Field(4, ge=1, le=7, title="Outlook days", description="Outlook length")
    hours_ahead: Optional[int] = Field(None, ge=2, le=72, title="Wind hours ahead",
                                       description="Plot the next N hours instead of today")

    @field_validator("home", "marina")
    @classmethod
    def _check_ll(cls, v: str) -> str:
        a, b = parse_latlon(v)
        return f"{a:g},{b:g}"


def compass(deg):
    if deg is None:
        return ""
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((deg % 360) / 22.5 + 0.5) % 16]


def fmt_time(iso):
    try:
        return dt.datetime.fromisoformat(iso).strftime("%-I:%M %p")
    except Exception:
        return iso


# ------------------------------------------------------------------- fetch

def fetch(lat, lon, wind_unit, temp_unit, days, want_hourly):
    params = {
        "latitude": f"{lat}", "longitude": f"{lon}",
        "timezone": "auto",
        "temperature_unit": temp_unit,
        "wind_speed_unit": wind_unit,
        "precipitation_unit": "inch" if temp_unit == "fahrenheit" else "mm",
        "current": ",".join([
            "temperature_2m", "relative_humidity_2m", "apparent_temperature",
            "is_day", "precipitation", "weather_code",
            "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"]),
        "daily": ",".join([
            "weather_code", "temperature_2m_max", "temperature_2m_min",
            "precipitation_sum", "precipitation_probability_max",
            "wind_speed_10m_max", "wind_gusts_10m_max",
            "wind_direction_10m_dominant", "sunrise", "sunset", "uv_index_max"]),
        "forecast_days": str(days),
    }
    if want_hourly:
        params["hourly"] = "wind_speed_10m,wind_gusts_10m,wind_direction_10m"
    q = urllib.parse.urlencode(params, safe=",")
    data = net.fetch_json(f"{API}?{q}", source="Open-Meteo", ttl=600)
    if data.get("error"):
        raise LabelError(f"Open-Meteo: {data.get('reason', 'request rejected')}")
    return data


def local_now(resp, ctx: RenderContext) -> dt.datetime:
    """``ctx.now`` in the forecast location's timezone, naive (to match API times)."""
    try:
        tz = ZoneInfo(resp.get("timezone") or "UTC")
    except Exception:
        tz = ctx.tz
    return ctx.now.astimezone(tz).replace(tzinfo=None)


# ------------------------------------------------------------------ report

def build_report(home, name, wind_unit, temp_unit, days):
    tu = "F" if temp_unit == "fahrenheit" else "C"
    c, d = home["current"], home["daily"]
    cond = WMO.get(c.get("weather_code"), f"code {c.get('weather_code')}")
    when = dt.datetime.fromisoformat(c["time"])
    p_in = "in" if temp_unit == "fahrenheit" else "mm"
    L = [f"# Weather - {name}",
         f"_As of {when:%a %b %-d, %-I:%M %p} local_", "",
         f"**Now:** {c['temperature_2m']:.0f}°{tu} (feels {c['apparent_temperature']:.0f}°{tu}) "
         f"- {cond}. Wind {compass(c.get('wind_direction_10m'))} {c['wind_speed_10m']:.0f} "
         f"{wind_unit}, gusts {c['wind_gusts_10m']:.0f} {wind_unit}. "
         f"Humidity {c['relative_humidity_2m']:.0f}%.", "",
         f"**Today:** hi {d['temperature_2m_max'][0]:.0f} / lo {d['temperature_2m_min'][0]:.0f}°{tu}. "
         f"Precip {d['precipitation_probability_max'][0] or 0:.0f}% "
         f"({d['precipitation_sum'][0] or 0:.2f} {p_in}). "
         f"Wind to {d['wind_speed_10m_max'][0]:.0f} {wind_unit} "
         f"(gusts {d['wind_gusts_10m_max'][0]:.0f}). UV max {d['uv_index_max'][0] or 0:.0f}.",
         f"**Sun:** up {fmt_time(d['sunrise'][0])}, down {fmt_time(d['sunset'][0])}.", "",
         "## Outlook", "",
         "| Day | Conditions | Hi/Lo | Wind | Precip |",
         "|-----|-----------|-------|------|--------|"]
    for i in range(min(days, len(d["time"]))):
        day = dt.date.fromisoformat(d["time"][i][:10])
        L.append(f"| {'Today' if i == 0 else day.strftime('%a %b %-d')} | "
                 f"{WMO.get(d['weather_code'][i], '-')} | "
                 f"{d['temperature_2m_max'][i]:.0f}/{d['temperature_2m_min'][i]:.0f}°{tu} | "
                 f"{compass(d['wind_direction_10m_dominant'][i])} "
                 f"{d['wind_speed_10m_max'][i]:.0f} {wind_unit} | "
                 f"{d['precipitation_probability_max'][i] or 0:.0f}% |")
    L += ["", f"_Source: Open-Meteo. {name} {home['latitude']:.4f}, {home['longitude']:.4f}._"]
    return "\n".join(L) + "\n"


# -------------------------------------------------------------------- plots

def _wind_series(marina, now, hours_ahead=None):
    h = marina["hourly"]
    times = [dt.datetime.fromisoformat(t) for t in h["time"]]
    if hours_ahead:
        keep = [i for i, t in enumerate(times) if 0 <= (t - now).total_seconds() <= hours_ahead * 3600]
    else:
        today = now.date()
        keep = [i for i, t in enumerate(times) if t.date() == today]
    if len(keep) < 2:
        raise LabelError("not enough hourly wind data for that window")
    return ([times[i] for i in keep], [h["wind_speed_10m"][i] for i in keep],
            [h["wind_gusts_10m"][i] for i in keep], [h["wind_direction_10m"][i] for i in keep])


def plot_wind(ctx, marina, marina_name, wind_unit, now, hours_ahead=None,
              size=(1140, 930), dpi=150):
    """Mono wind plot for the label: solid sustained, dashed gusts, SCA line in knots."""
    import matplotlib.dates as mdates

    times, spd, gst, drc = _wind_series(marina, now, hours_ahead)
    ink = accent = gustc = sca = "black"
    grid = "#c8c8c8"
    fig = ctx.figure(*size, dpi=dpi)
    ax = fig.subplots()
    ax.set_facecolor("white")
    ax.plot(times, gst, color=gustc, lw=1.8, ls=(0, (4, 3)), label="Gusts", zorder=3)
    ax.plot(times, spd, color=accent, lw=2.6, label="Sustained", zorder=4)

    ymax = max(max(gst), 21) * 1.18
    ax.set_ylim(0, ymax)
    ax.set_xlim(times[0], times[-1])
    if wind_unit == "kn":            # small-craft-advisory reference (NWS: ~21-33 kn)
        ax.axhline(21, color=sca, lw=1.0, ls=":", zorder=2)
        ax.text(times[0], 21.4, "small craft advisory (21 kn)", color=sca, fontsize=8,
                va="bottom")

    step = max(1, len(times) // 8)   # direction labels along the top
    for i in range(0, len(times), step):
        ax.annotate(compass(drc[i]), (times[i], ymax * 0.94), ha="center", va="center",
                    fontsize=8, color=ink,
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec=grid, lw=0.6))
    if times[0] <= now <= times[-1]:
        ax.axvline(now, color=ink, lw=1.0, alpha=0.5)
        ax.text(now, ymax * 0.02, " now", color=ink, fontsize=8, alpha=0.7)

    span = (f"{times[0]:%a %b %-d}" if hours_ahead is None
            else f"next {hours_ahead} h")
    ax.set_title(f"{marina_name} - Wind for {span}", fontsize=13, color=ink, loc="left",
                 pad=26, weight="bold")
    ax.set_ylabel(f"Wind speed ({WIND_LABEL[wind_unit]})", color=ink, fontsize=10)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-I%p"))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3 if len(times) <= 30 else 6))
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(grid)
    ax.grid(True, color=grid, lw=0.8, zorder=0)
    ax.tick_params(colors=ink, labelsize=9)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=2, frameon=False, fontsize=9)
    fig.tight_layout()
    peak = max(range(len(gst)), key=lambda i: gst[i])
    return ctx.image(fig), {"max_sustained": max(spd), "max_gust": max(gst),
                            "peak_time": times[peak].isoformat(), "peak_dir": compass(drc[peak])}


def wind_sparkline(ctx, marina, now, hours_ahead=None, w_px=576, h_px=88):
    """Minimal all-black sustained+gust sparkline (no axes) for the 2x1 card."""
    times, spd, gst, _ = _wind_series(marina, now, hours_ahead)
    fig = ctx.figure(w_px, h_px)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.plot(times, gst, color="black", lw=1.2, ls=(0, (3, 2)))
    ax.plot(times, spd, color="black", lw=2.0)
    ax.set_xlim(times[0], times[-1])
    ax.set_ylim(0, max(max(gst), 1) * 1.12)
    if times[0] <= now <= times[-1]:      # dark enough to survive thresholding at 203 dpi
        ax.axvline(now, color="black", lw=1.0, alpha=0.7)
    return ctx.image(fig)


# ------------------------------------------------------------------ layouts

def _trim_to(text: str, height: float, width: float) -> str:
    """Cut ``text`` to fit ``width`` design units as ``^A0`` text of ``height`` (measured
    like the preview draws it: DejaVu Sans at 0.82 x height)."""
    f = font(max(6, int(height * 0.82)))
    while text and f.getlength(text) > width:     # drop whole words, else characters
        text = text.rsplit(" ", 1)[0].rstrip() if " " in text.strip() else text[:-1]
    return text


def layout_2x1(z, ctx, p, home, marina, now, tzabbr):
    M = 12
    W, H = z.W, z.H
    wu = WIND_LABEL[p.wind_unit]
    tu = "F" if p.temp_unit == "fahrenheit" else "C"
    c, d = home["current"], home["daily"]
    cond = WMO.get(c.get("weather_code"), "-")
    z.text(M, 8, 32, _trim_to(f"{p.home_name.upper()[:14]}  {c['temperature_2m']:.0f}{tu}  {cond}",
                              32, W - 2 * M))
    z.text(M, 46, 22, f"Wind {compass(c.get('wind_direction_10m'))} {c['wind_speed_10m']:.0f} {wu} "
                      f"g{c['wind_gusts_10m']:.0f}   Hum {c['relative_humidity_2m']:.0f}%")
    z.text(M, 76, 22, f"Today Hi {d['temperature_2m_max'][0]:.0f} / Lo {d['temperature_2m_min'][0]:.0f}"
                      f"{tu}   Precip {d['precipitation_probability_max'][0] or 0:.0f}%")
    span = f"next {p.hours_ahead} h" if p.hours_ahead else "today"
    z.text(M, 110, 18, f"{p.marina_name} wind ({wu}), {span}")
    z.image(M, 132, wind_sparkline(ctx, marina, now, p.hours_ahead))
    z.text(M, H - 24, 18, f"Open-Meteo  -  {now:%Y-%m-%d %H:%M} {tzabbr}")


def layout_4x6(z, ctx, p, home, marina, now, tzabbr):
    M = 40
    W, H = z.W, z.H
    wu = WIND_LABEL[p.wind_unit]
    tu = "F" if p.temp_unit == "fahrenheit" else "C"
    p_in = "in" if p.temp_unit == "fahrenheit" else "mm"
    c, d = home["current"], home["daily"]
    cond = WMO.get(c.get("weather_code"), "-")

    z.text(M, M, 58, f"WEATHER - {p.home_name.upper()}"[:28])
    z.text(M, M + 64, 30, f"{now:%a %b %-d, %-I:%M %p} {tzabbr}")
    z.hline(M, M + 104, W - 2 * M)

    y = M + 124

    def line(text, size=34, gap=44):
        nonlocal y
        z.text(M, y, size, text)
        y += gap

    line(f"NOW  {c['temperature_2m']:.0f}{tu} (feels {c['apparent_temperature']:.0f})  {cond}", 38, 48)
    line(f"Wind {compass(c.get('wind_direction_10m'))} {c['wind_speed_10m']:.0f} {wu}"
         f"  gust {c['wind_gusts_10m']:.0f}   Humidity {c['relative_humidity_2m']:.0f}%")
    line(f"TODAY  hi {d['temperature_2m_max'][0]:.0f} / lo {d['temperature_2m_min'][0]:.0f}{tu}"
         f"   precip {d['precipitation_probability_max'][0] or 0:.0f}% "
         f"({d['precipitation_sum'][0] or 0:.2f}{p_in})")
    line(f"Sun {fmt_time(d['sunrise'][0])}-{fmt_time(d['sunset'][0])}   "
         f"UV max {d['uv_index_max'][0] or 0:.0f}")

    y += 6
    z.hline(M, y, W - 2 * M)
    y += 12
    z.text(M, y, 30, "OUTLOOK")
    y += 42
    for i in range(min(p.days, len(d["time"]))):
        day = dt.date.fromisoformat(d["time"][i][:10])
        lbl = "Today" if i == 0 else day.strftime("%a %-m/%-d")
        z.text(M, y, 28, f"{lbl:<9} {WMO.get(d['weather_code'][i], '-')[:16]:<16} "
                         f"{d['temperature_2m_max'][i]:.0f}/{d['temperature_2m_min'][i]:.0f}  "
                         f"{compass(d['wind_direction_10m_dominant'][i])} "
                         f"{d['wind_speed_10m_max'][i]:.0f}{wu}  "
                         f"{d['precipitation_probability_max'][i] or 0:.0f}%")
        y += 36
    y += 14   # the plot carries its own title

    img, stats = plot_wind(ctx, marina, p.marina_name, p.wind_unit, now, p.hours_ahead)
    avail = (H - M - 40) - y             # scale the plot down only if it overflows
    if z.design(img.height) > avail:
        from PIL import Image
        new_h = max(8, z.dots(avail))
        img = img.resize((int(img.width * new_h / img.height), new_h), Image.LANCZOS)
    z.image(None, y, img)
    z.text(M, H - M - 16, 20, f"Open-Meteo - {now:%Y-%m-%d %H:%M} {tzabbr}")
    return stats


# -------------------------------------------------------------------- label

@register
class WeatherLabel(Label):
    id = "weather"
    name = "Weather"
    description = "Current conditions and outlook for home, plus the day's wind at a marina."
    icon = "mdi:weather-partly-cloudy"
    Params = WeatherParams
    fetch_outside_lock = True

    def render(self, p: WeatherParams, ctx: RenderContext) -> RenderResult:
        home_ll = [float(v) for v in p.home.split(",")]
        marina_ll = [float(v) for v in p.marina.split(",")]
        home = fetch(*home_ll, p.wind_unit, p.temp_unit, p.days, want_hourly=False)
        marina_days = max(2, math.ceil((p.hours_ahead or 0) / 24) + 1)
        marina = fetch(*marina_ll, p.wind_unit, p.temp_unit, marina_days, want_hourly=True)
        now = local_now(home, ctx)
        tzabbr = home.get("timezone_abbreviation", "")
        wu = WIND_LABEL[p.wind_unit]

        z = ctx.zpl()
        stats = None
        with ctx.drawing():
            if ctx.size.id == "2x1":
                layout_2x1(z, ctx, p, home, marina, local_now(marina, ctx), tzabbr)
            else:
                stats = layout_4x6(z, ctx, p, home, marina, now, tzabbr)
        c = home["current"]
        data = {"location": p.home_name, "temperature": c["temperature_2m"],
                "apparent_temperature": c["apparent_temperature"],
                "condition": WMO.get(c.get("weather_code"), "-"),
                "humidity": c["relative_humidity_2m"], "wind_speed": c["wind_speed_10m"],
                "wind_gust": c["wind_gusts_10m"], "wind_dir": compass(c.get("wind_direction_10m")),
                "wind_unit": wu, "temp_unit": p.temp_unit, "marina_wind": stats}
        return RenderResult(z.build(), title="weather",
                            report=build_report(home, p.home_name, wu, p.temp_unit, p.days),
                            data=data)
