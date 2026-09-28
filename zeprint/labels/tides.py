"""
Tide label (port of tidelabel.py): the day's tide curve for a NOAA station with
high/low marks and a "now" marker, sun times and a drawn moon-phase disc, and a
QR to the NOAA station page.

Data: NOAA CO-OPS Tides & Currents (https://tidesandcurrents.noaa.gov), keyless.
  predictions:  datagetter?product=predictions (6-min curve + hilo)
  live level:   product=water_level&date=latest
  station name: mdapi/prod/webapi/stations/<id>.json
Sun times (NOAA solar algorithm) and moon phase are computed locally.

Times are station-local (NOAA ``lst_ldt``); "now" uses the service timezone, so
keep that set to where your stations are.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Optional

from pydantic import BaseModel, Field

from .. import net
from ..errors import LabelError
from ..zpl import qr_modules
from . import Label, RenderContext, RenderResult, register

DG = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
MD = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{sid}.json"
STATION_PAGE = "https://tidesandcurrents.noaa.gov/stationhome.html?id={sid}"
DEFAULT_LATLON = (47.7076, -122.2054)  # Kirkland/Juanita, sun-time fallback
APP = "zeprint"


class TidesParams(BaseModel):
    station: str = Field("9447130", title="Station", pattern=r"^\d{7}$",
                         description="NOAA CO-OPS station id (9447130 = Seattle)")
    date: Optional[dt.date] = Field(None, description="Day to print (default: today)")


# ------------------------------------------------------------------- fetch

def _dg(station: str, date: dt.date, product: str, extra: str = "", ttl: float = 600):
    ymd = date.strftime("%Y%m%d")
    url = (f"{DG}?product={product}&application={APP}&begin_date={ymd}&end_date={ymd}"
           f"&datum=MLLW&station={station}&time_zone=lst_ldt&units=english&format=json{extra}")
    return net.fetch_json(url, source="NOAA CO-OPS", ttl=ttl)


def _parse(rows):
    out = []
    for r in rows:
        t = dt.datetime.strptime(r["t"], "%Y-%m-%d %H:%M")
        out.append((t, float(r["v"]), r.get("type")))
    return out


def fetch_tides(station: str, date: dt.date):
    """Returns (curve[(t, ft)], events[(t, ft, 'H'/'L')], name, observed, (lat, lon))."""
    curve_raw = _dg(station, date, "predictions", "&interval=6")
    if "predictions" not in curve_raw:
        msg = (curve_raw.get("error") or {}).get("message", "unknown error")
        raise LabelError(f"NOAA returned no predictions for station {station} ({msg}). "
                         "Check the station id.")
    hilo_raw = _dg(station, date, "predictions", "&interval=hilo")
    curve = [(t, v) for t, v, _ in _parse(curve_raw["predictions"])]
    events = _parse(hilo_raw.get("predictions", []))
    if len(curve) < 2:
        raise LabelError(f"NOAA returned too few predictions for station {station}")

    name, latlon = None, (None, None)
    try:
        md = net.fetch_json(MD.format(sid=station), source="NOAA CO-OPS", ttl=86400)
        st = (md.get("stations") or [{}])[0]
        name = st.get("name")
        latlon = (st.get("lat"), st.get("lng"))
    except Exception:
        pass

    observed = None
    try:
        wl = _dg(station, date, "water_level", "&date=latest", ttl=120).get("data") or []
        if wl:
            observed = (dt.datetime.strptime(wl[-1]["t"], "%Y-%m-%d %H:%M"), float(wl[-1]["v"]))
    except Exception:
        pass
    return curve, events, name, observed, latlon


# ---------------------------------------------------------------- sun / moon

def _jd(d: dt.datetime) -> float:
    """UTC datetime -> Julian date."""
    d = d.astimezone(dt.timezone.utc)
    y, m = d.year, d.month
    day = d.day + (d.hour + (d.minute + d.second / 60) / 60) / 24
    if m <= 2:
        y -= 1
        m += 12
    b = 2 - y // 100 + y // 400
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + day + b - 1524.5


def sun_times(date: dt.date, lat: float, lon: float, tz) -> tuple:
    """Local sunrise/sunset datetimes (NOAA sunrise equation). None if no rise/set."""
    d0 = dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc)
    n = math.floor(_jd(d0) - 2451545.0 + 0.0008) + 1
    Js = n - lon / 360.0
    M = (357.5291 + 0.98560028 * Js) % 360
    Mr = math.radians(M)
    C = 1.9148 * math.sin(Mr) + 0.02 * math.sin(2 * Mr) + 0.0003 * math.sin(3 * Mr)
    lam = math.radians((M + C + 180 + 102.9372) % 360)
    Jtr = 2451545.0 + Js + 0.0053 * math.sin(Mr) - 0.0069 * math.sin(2 * lam)
    dec = math.asin(math.sin(lam) * math.sin(math.radians(23.44)))
    latr = math.radians(lat)
    cosw = ((math.sin(math.radians(-0.833)) - math.sin(latr) * math.sin(dec))
            / (math.cos(latr) * math.cos(dec)))
    if cosw < -1 or cosw > 1:
        return None, None
    w = math.degrees(math.acos(cosw))

    def to_local(jd):
        u = dt.datetime(2000, 1, 1, 12, tzinfo=dt.timezone.utc) + dt.timedelta(days=jd - 2451545.0)
        return u.astimezone(tz)
    return to_local(Jtr - w / 360.0), to_local(Jtr + w / 360.0)


def moon_phase(date: dt.date) -> tuple[float, float, str]:
    """(fraction 0..1 through the synodic month, illuminated %, name)."""
    noon = dt.datetime(date.year, date.month, date.day, 12, tzinfo=dt.timezone.utc)
    frac = ((_jd(noon) - 2451550.1) / 29.530588853) % 1.0
    illum = (1 - math.cos(2 * math.pi * frac)) / 2 * 100
    names = ["New", "Waxing crescent", "First quarter", "Waxing gibbous",
             "Full", "Waning gibbous", "Last quarter", "Waning crescent"]
    return frac, illum, names[int((frac * 8 + 0.5) % 8)]


def draw_moon(ctx: RenderContext, phase: float, px: float = 150):
    """Mono moon-phase disc: shadow filled black, lit white, on white paper."""
    import numpy as np
    from matplotlib.patches import Circle

    fig = ctx.figure(px, px)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(-1.1, 1.1)
    ax.set_ylim(-1.1, 1.1)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.add_patch(Circle((0, 0), 1, facecolor="black", edgecolor="none"))
    ys = np.linspace(-1, 1, 160)
    xl = np.sqrt(np.clip(1 - ys * ys, 0, None))
    c = math.cos(2 * math.pi * phase)
    if phase < 0.5:                                   # waxing: lit on the right
        left, right = c * xl, xl
    else:                                             # waning: lit on the left
        left, right = -xl, -c * xl
    ax.fill(np.concatenate([left, right[::-1]]), np.concatenate([ys, ys[::-1]]),
            facecolor="white", edgecolor="none")
    ax.add_patch(Circle((0, 0), 1, fill=False, edgecolor="black", lw=2.0))
    return ctx.image(fig)


# -------------------------------------------------------------------- plots

def render_tide_curve(ctx, curve, events, now, observed, box_w, box_h, units="ft"):
    """Mono tide curve: hatched water fill, hi/lo markers+labels, now marker."""
    import matplotlib.dates as mdates

    t = [p[0] for p in curve]
    v = [p[1] for p in curve]
    fig = ctx.figure(int(box_w) & ~7, box_h)
    ax = fig.add_axes([0.10, 0.16, 0.88, 0.74])
    ax.set_facecolor("white")

    vmin, vmax = min(0.0, min(v)), max(v)
    pad = (vmax - vmin) * 0.16 + 0.5
    ax.set_ylim(vmin - 0.3, vmax + pad)
    ax.set_xlim(t[0], t[-1])

    ax.fill_between(t, v, vmin - 0.3, facecolor="none", edgecolor="black",
                    hatch="....", linewidth=0.0, zorder=1)     # hatched "water"
    ax.plot(t, v, color="black", lw=2.4, zorder=3)             # tide curve
    ax.axhline(0, color="black", lw=0.8, ls=(0, (5, 4)), zorder=2)  # MLLW datum
    ax.text(t[0], 0.05, "MLLW", fontsize=8, va="bottom")

    for te, ve, ty in events:
        if not (t[0] <= te <= t[-1]):
            continue
        ax.plot([te], [ve], marker="o", ms=6, mfc="white", mec="black", mew=1.6, zorder=5)
        ax.annotate(f"{'HIGH' if ty == 'H' else 'LOW'}\n{te:%-I:%M%p}\n{ve:.1f} {units}",
                    (te, ve), textcoords="offset points",
                    xytext=(0, 12 if ty == "H" else -34), ha="center", fontsize=8, zorder=6)

    if t[0] <= now <= t[-1]:
        i = min(range(len(t)), key=lambda k: abs((t[k] - now).total_seconds()))
        ax.axvline(now, color="black", lw=1.0, alpha=0.55, zorder=2)
        ax.plot([now], [observed[1] if observed else v[i]], marker="*", ms=20,
                mfc="black", mec="white", mew=0.8, zorder=7)
        ax.text(now, ax.get_ylim()[0], " now", fontsize=8, va="bottom", alpha=0.7)

    ax.set_ylabel(f"Height ({units}, MLLW)", fontsize=10)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-I%p"))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#cccccc")
    ax.grid(True, color="#dddddd", lw=0.7, zorder=0)
    ax.tick_params(labelsize=9)
    return ctx.image(fig)


def tide_sparkline(ctx, curve, events, now, w_px=576, h_px=104):
    """Minimal all-black tide curve (no axes) with hi/lo dots + now marker."""
    t = [p[0] for p in curve]
    v = [p[1] for p in curve]
    fig = ctx.figure(w_px, h_px)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.plot(t, v, color="black", lw=2.0)
    ax.axhline(0, color="black", lw=0.6, ls=(0, (4, 3)))
    for te, ve, ty in events:
        if t[0] <= te <= t[-1]:
            ax.plot([te], [ve], marker="o", ms=4, mfc="white", mec="black", mew=1.2)
    if t[0] <= now <= t[-1]:
        ax.axvline(now, color="black", lw=0.9, alpha=0.6)
    ax.set_xlim(t[0], t[-1])
    ax.set_ylim(min(0, min(v)) - 0.3, max(v) + 0.6)
    return ctx.image(fig)


# ------------------------------------------------------------------ layouts

def layout_4x6(z, ctx, station, name, date, curve, events, now, observed, now_level,
               sun, moon, page_url):
    M = 40
    W, H = z.W, z.H

    title = (name or f"Station {station}")[:24]
    z.text(M, M, 56, f"TIDES - {title}")
    z.text(M, M + 62, 30, f"Station {station}  -  MLLW  -  {date:%a %b %-d, %Y}")
    z.hline(M, M + 102, W - 2 * M)

    y = M + 118
    _, ch = z.image(None, y, render_tide_curve(ctx, curve, events, now, observed, W - 2 * M, 700))
    y += ch + 10
    z.hline(M, y, W - 2 * M)
    y += 14

    # hi/lo table (two per row)
    z.text(M, y, 32, "HIGHS & LOWS")
    y += 42
    col = 0
    for te, ve, ty in events:
        x = M + (0 if col == 0 else W // 2)
        z.text(x, y, 30, f"{'H' if ty == 'H' else 'L'}  {te:%-I:%M %p}   {ve:+.1f} ft")
        col ^= 1
        if col == 0:
            y += 40
    if col == 1:
        y += 40

    # now / observed
    y += 4
    lvl = observed[1] if observed else now_level
    src = "observed" if observed else "predicted"
    z.text(M, y, 34, f"NOW {now:%-I:%M %p}   {lvl:+.1f} ft ({src})")
    y += 48

    # sun + moon block (moon disc raster on the right, inside the margin)
    z.hline(M, y, W - 2 * M)
    y += 16
    sr, ss = sun
    frac, illum, mname = moon
    z.text(M, y, 32, f"Sun   up {f'{sr:%-I:%M %p}' if sr else 'n/a'}   "
                     f"down {f'{ss:%-I:%M %p}' if ss else 'n/a'}")
    z.text(M, y + 46, 32, f"Moon  {mname}  {illum:.0f}%")
    moon_side = 150
    z.image(W - M - moon_side, y - 8, draw_moon(ctx, frac, px=moon_side))
    block_bottom = max(y + 92, y - 8 + moon_side)

    # QR to the NOAA station page, centered in the space left below the block
    footer_y = H - M - 22
    mods = qr_modules(len(page_url)) + 8
    mag = max(4, min(8, 300 // mods))
    side = z.qr_side(page_url, mag)
    region_top, region_bot = block_bottom + 16, footer_y - 16
    qtop = region_top + max(0, (region_bot - region_top - (side + 30)) // 2)
    qx = W - M - side
    z.text(qx, qtop, 26, "NOAA station")
    z.qr(qx, qtop + 30, page_url, mag)
    z.text(M, footer_y, 22, f"NOAA CO-OPS predictions (MLLW) - generated "
                            f"{ctx.now:%Y-%m-%d %H:%M %Z} - not for navigation")


def layout_2x1(z, ctx, station, name, date, curve, events, now, now_level, observed):
    """Compact card: next hi/lo, now level, tide sparkline."""
    M = 12
    H = z.H
    z.text(M, 8, 30, f"TIDES  {(name or f'Station {station}')[:18]}")
    nxt = next((e for e in events if e[0] >= now), None)
    if nxt:
        z.text(M, 44, 24, f"Next {'HIGH' if nxt[2] == 'H' else 'LOW'} "
                          f"{nxt[0]:%-I:%M %p}   {nxt[1]:+.1f} ft")
    lvl = observed[1] if observed else now_level
    z.text(M, 74, 22, f"Now {lvl:+.1f} ft ({'obs' if observed else 'pred'})   MLLW   {date:%b %-d}")
    z.text(M, 104, 18, "tide, today (ft)")
    z.image(M, 126, tide_sparkline(ctx, curve, events, now))
    z.text(M, H - 24, 18, "NOAA CO-OPS  -  not for navigation")


# -------------------------------------------------------------------- label

@register
class TidesLabel(Label):
    id = "tides"
    name = "Tides"
    description = "Today's tide curve, highs & lows, sun and moon for a NOAA tide station."
    icon = "mdi:waves"
    Params = TidesParams

    def render(self, p: TidesParams, ctx: RenderContext) -> RenderResult:
        date = p.date or ctx.now.date()
        now = ctx.now.replace(tzinfo=None)
        curve, events, name, observed, latlon = fetch_tides(p.station, date)
        lat, lon = latlon if latlon[0] is not None else DEFAULT_LATLON
        lat, lon = float(lat), float(lon)
        page_url = STATION_PAGE.format(sid=p.station)
        ni = min(range(len(curve)), key=lambda k: abs((curve[k][0] - now).total_seconds()))
        now_level = curve[ni][1]

        z = ctx.zpl()
        if ctx.size.id == "2x1":
            layout_2x1(z, ctx, p.station, name, date, curve, events, now, now_level, observed)
        else:
            layout_4x6(z, ctx, p.station, name, date, curve, events, now, observed, now_level,
                       sun_times(date, lat, lon, ctx.tz), moon_phase(date), page_url)

        nxt = next((e for e in events if e[0] >= now), None)
        data = {"station": p.station, "name": name, "date": date.isoformat(),
                "level_ft": observed[1] if observed else now_level,
                "level_source": "observed" if observed else "predicted",
                "next_event": ({"type": "high" if nxt[2] == "H" else "low",
                                "time": nxt[0].isoformat(), "height_ft": nxt[1]} if nxt else None),
                "events": [{"type": "high" if ty == "H" else "low", "time": te.isoformat(),
                            "height_ft": ve} for te, ve, ty in events]}
        return RenderResult(z.build(), title=f"tide-{p.station}", data=data)
