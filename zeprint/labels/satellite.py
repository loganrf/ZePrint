"""
Satellite card (port of satlabel.py): fetch a satellite's orbit by NORAD ID, plot
its ground track on a Mercator map and print an orbit/launch card with a QR link
to CelesTrak.

Data: CelesTrak GP element sets + SATCAT (keyless). CelesTrak asks clients not to
re-download an element set more than every couple of hours, so TLEs are cached
on disk for two hours; a stale cache is used if CelesTrak is unreachable.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import math
import time
from functools import lru_cache
from importlib import resources
from typing import Optional

from pydantic import BaseModel, Field

from .. import net
from ..errors import FetchError, LabelError
from ..zpl import qr_modules
from . import Label, RenderContext, RenderResult, register

log = logging.getLogger(__name__)

GP_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={cat}&FORMAT=tle"
SATCAT_URL = "https://celestrak.org/satcat/records.php?CATNR={cat}&FORMAT=JSON"
PAGE_URL = "https://celestrak.org/satcat/table-satcat.php?CATNR={cat}"   # QR target
TLE_MAX_AGE = 2 * 3600

# WGS84
WGS84_A = 6378.137            # km
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)


class SatelliteParams(BaseModel):
    norad: str = Field("25544", title="NORAD ID", pattern=r"^\d{1,9}$",
                       description="NORAD catalog number (25544 = ISS)")
    duration: Optional[float] = Field(None, gt=0, le=1440, title="Track length (min)",
                                      description="Minutes of ground track (default: one orbit)")
    step: float = Field(30.0, ge=5, le=600, title="Track step (s)", description="Track sample step, seconds")
    tle: Optional[str] = Field(None, title="TLE (optional)", description="Use this element set instead of fetching "
                                                 "(optional name line + two TLE lines)")
    qr_url: Optional[str] = Field(None, title="QR URL", description="Override the QR target URL")


# ----------------------------------------------------------------- TLE / fetch

def tle_checksum(line: str) -> int:
    """Modulo-10 checksum of a TLE line (digits sum, '-' counts as 1)."""
    s = 0
    for c in line[:68]:
        if c.isdigit():
            s += int(c)
        elif c == "-":
            s += 1
    return s % 10


def valid_tle_line(line: str) -> bool:
    return len(line) >= 69 and line[68].isdigit() and tle_checksum(line) == int(line[68])


def parse_tle(text: str) -> tuple[str | None, str, str]:
    """Pull (name, line1, line2) out of a blob; tolerate an optional name line."""
    lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    l1 = l2 = name = None
    for i, ln in enumerate(lines):
        if ln.startswith("1 ") and len(ln) >= 69:
            l1 = ln
            if i + 1 < len(lines) and lines[i + 1].startswith("2 "):
                l2 = lines[i + 1]
                if i > 0 and not lines[i - 1].startswith(("1 ", "2 ")):
                    name = lines[i - 1].strip()
            break
    if not (l1 and l2):
        raise LabelError("no valid TLE (a '1 ...' line followed by a '2 ...' line) found")
    for tag, ln in (("1", l1), ("2", l2)):
        if not valid_tle_line(ln):
            raise LabelError(f"TLE line {tag} failed its checksum - the data looks corrupted: {ln}")
    return name, l1, l2


def load_tle(catnr: str, ctx: RenderContext, tle_text: str | None = None):
    """Element set from (in order): literal text, fresh cache, CelesTrak, stale cache."""
    if tle_text:
        return parse_tle(tle_text)
    cache = ctx.cache_path("celestrak", f"{catnr}.tle")
    if cache and cache.exists() and time.time() - cache.stat().st_mtime < TLE_MAX_AGE:
        try:
            return parse_tle(cache.read_text())
        except LabelError:
            pass
    try:
        text = net.fetch_text(GP_URL.format(cat=catnr), source="CelesTrak")
    except FetchError as e:
        if cache and cache.exists():
            log.warning("CelesTrak unreachable (%s); using cached %s", e, cache)
            return parse_tle(cache.read_text())
        raise FetchError(f"{e}. Allowlist celestrak.org, or pass the element set in the 'tle' "
                         f"parameter (from {GP_URL.format(cat=catnr)})") from e
    if "No GP data found" in text or not text.strip():
        raise LabelError(f"CelesTrak has no element set for NORAD {catnr} "
                         "(decayed, classified, or wrong ID?)")
    parsed = parse_tle(text)
    if cache:
        cache.write_text(text)
    return parsed


def load_satcat(catnr: str) -> dict | None:
    """SATCAT record as a dict, or None if unavailable. Never fatal."""
    try:
        text = net.fetch_text(SATCAT_URL.format(cat=catnr), source="CelesTrak SATCAT", ttl=3600)
        try:
            rows = json.loads(text)
            rows = rows if isinstance(rows, list) else [rows]
        except ValueError:
            rows = list(csv.DictReader(io.StringIO(text)))
        rows = [{k: ("" if v is None else str(v)) for k, v in r.items()} for r in rows
                if isinstance(r, dict)]
        for r in rows:
            if r.get("NORAD_CAT_ID", "").strip() == str(catnr):
                return r
        return rows[0] if rows else None
    except Exception as e:
        log.info("SATCAT lookup skipped (%s); deriving what I can from the TLE", e)
        return None


# --------------------------------------------------------------- orbit / track

def intl_designator(l1: str) -> str | None:
    """TLE cols 10-17 -> e.g. '1998-067A'. Two-digit year windowed at 57."""
    raw = l1[9:17].strip()
    if len(raw) < 5 or not raw[:2].isdigit():
        return None
    yy = int(raw[:2])
    year = 1900 + yy if yy >= 57 else 2000 + yy
    return f"{year}-{raw[2:].strip()}"


def epoch_datetime(l1: str) -> dt.datetime:
    """TLE epoch (cols 19-32) -> aware UTC datetime."""
    yy = int(l1[18:20])
    year = 2000 + yy if yy < 57 else 1900 + yy
    doy = float(l1[20:32])
    return dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=doy - 1.0)


def gmst_rad(jd_ut1: float) -> float:
    """Greenwich Mean Sidereal Time (IAU-82), radians. Vallado eq. 3-47."""
    T = (jd_ut1 - 2451545.0) / 36525.0
    sec = (67310.54841 + (876600.0 * 3600.0 + 8640184.812866) * T
           + 0.093104 * T * T - 6.2e-6 * T * T * T)
    return math.radians((sec % 86400.0) / 240.0)   # 1 s = 1/240 deg


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """WGS84 ECEF (km) -> (lat_deg, lon_deg, alt_km). Bowring closed form."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:                                   # over a pole
        lat = math.copysign(math.pi / 2, z)
        return math.degrees(lat), math.degrees(lon), abs(z) - WGS84_A * (1 - WGS84_F)
    b = WGS84_A * (1 - WGS84_F)
    ep2 = (WGS84_A ** 2 - b ** 2) / b ** 2
    th = math.atan2(z * WGS84_A, p * b)
    lat = math.atan2(z + ep2 * b * math.sin(th) ** 3,
                     p - WGS84_E2 * WGS84_A * math.cos(th) ** 3)
    N = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - N
    return math.degrees(lat), math.degrees(lon), alt


def propagate(l1: str, l2: str, when: dt.datetime, minutes: float, step_s: float) -> dict:
    """
    track: list of (lat, lon, alt_km) from ``when`` forward ``minutes``.
    now:   (lat, lon, alt_km, speed_km_s) at ``when``.
    """
    from sgp4.api import SGP4_ERRORS, Satrec, jday

    sat = Satrec.twoline2rv(l1, l2)

    def state(t):
        jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond / 1e6)
        e, r, v = sat.sgp4(jd, fr)
        if e != 0:
            raise LabelError(f"SGP4 error {e}: {SGP4_ERRORS.get(e, 'unknown')} "
                             "(the element set may be too old for this date)")
        g = gmst_rad(jd + fr)
        cg, sg = math.cos(g), math.sin(g)
        # TEME -> ECEF (PEF): rotate by -GMST about Z
        xe = r[0] * cg + r[1] * sg
        ye = -r[0] * sg + r[1] * cg
        lat, lon, alt = ecef_to_geodetic(xe, ye, r[2])
        return lat, ((lon + 180) % 360) - 180, alt, math.sqrt(sum(c * c for c in v))

    track = []
    n = max(2, int(minutes * 60 / step_s) + 1)
    for i in range(n):
        la, lo, al, _ = state(when + dt.timedelta(seconds=i * step_s))
        track.append((la, lo, al))
    return {"track": track, "now": state(when), "sat": sat}


def orbital_elements(l2: str) -> dict:
    """Human-facing orbital parameters from TLE line 2."""
    mm = float(l2[52:63])                          # mean motion, rev/day
    ecc = float("0." + l2[26:33].strip())
    period_min = 1440.0 / mm if mm else float("nan")
    mu = 398600.4418
    a = (mu * (period_min * 60 / (2 * math.pi)) ** 2) ** (1 / 3.0)   # km
    return {"mean_motion": mm, "ecc": ecc, "inc": float(l2[8:16]), "raan": float(l2[17:25]),
            "argp": float(l2[34:42]), "period_min": period_min, "sma_km": a,
            "apogee_km": a * (1 + ecc) - WGS84_A, "perigee_km": a * (1 - ecc) - WGS84_A,
            "rev": int(l2[63:68])}


# ------------------------------------------------------------------- map image

@lru_cache(maxsize=1)
def world_rings() -> list:
    """Natural Earth 1:110m land polygons (public domain)."""
    with resources.files(__package__).joinpath("data/world_land.json").open() as f:
        return json.load(f)["rings"]


def _mercator_y(lat_deg: float, cap: float) -> float:
    lat = math.radians(max(-cap, min(cap, lat_deg)))
    return math.log(math.tan(math.pi / 4 + lat / 2))


def render_map(ctx: RenderContext, track, now, box_w: float, box_h: float):
    """
    Mercator map sized to fill (box_w x box_h) design px as closely as its aspect
    allows, rendered at device resolution so thin lines survive 1-bit
    thresholding. The latitude cap adapts to the track so a polar orbit isn't
    clipped and a low-inclination orbit isn't lost in a mostly-empty map.
    """
    from matplotlib.collections import LineCollection

    peak = max(abs(la) for la, _, _ in track)
    cap = min(84.0, max(55.0, math.ceil(peak) + 6))
    ymax = _mercator_y(cap, cap)
    aspect = (2 * ymax) / (2 * math.pi)                 # h/w in projection units
    w = min(box_w, int(box_h / aspect))
    h = int(round(w * aspect))
    w = int(w) & ~7

    fig = ctx.figure(w, h)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(-180, 180)
    ax.set_ylim(-ymax, ymax)
    ax.axis("off")

    def yproj(lat):
        return _mercator_y(lat, cap)

    for lon in range(-180, 181, 30):                    # faint graticule
        ax.plot([lon, lon], [-ymax, ymax], color="0.75", lw=0.5, zorder=0)
    for lat in range(-60, 61, 30):
        ax.plot([-180, 180], [yproj(lat)] * 2, color="0.75", lw=0.5, zorder=0)
    ax.plot([-180, 180], [0, 0], color="0.45", lw=0.8, zorder=1)   # equator

    segs = [[(x, yproj(y)) for x, y in ring] for ring in world_rings()]
    ax.add_collection(LineCollection(segs, colors="0.25", linewidths=0.7, zorder=2))

    seg: list[tuple[float, float]] = []                 # ground track, split at dateline
    for la, lo, _al in track:
        if seg and abs(lo - seg[-1][0]) > 180:
            ax.plot([p[0] for p in seg], [yproj(p[1]) for p in seg],
                    color="black", lw=2.4, solid_capstyle="round", zorder=3)
            seg = []
        seg.append((lo, la))
    if seg:
        ax.plot([p[0] for p in seg], [yproj(p[1]) for p in seg],
                color="black", lw=2.4, solid_capstyle="round", zorder=3)

    la, lo, _al, _spd = now                             # current sub-point
    ax.plot([lo], [yproj(la)], marker="*", ms=24, mfc="black", mec="white", mew=0.8, zorder=5)
    return ctx.image(fig)


# ---------------------------------------------------------------------- report

def build_report(name, catnr, l1, sc, elem, now, epoch, when):
    la, lo, al, spd = now
    intl = intl_designator(l1)
    age_h = (when - epoch).total_seconds() / 3600.0

    def g(key):
        v = (sc or {}).get(key, "")
        return v.strip() or None

    launch_date = g("LAUNCH_DATE")
    launch_year = launch_date[:4] if launch_date else (intl.split("-")[0] if intl else "?")
    meta = {"intl": intl, "launch_date": launch_date, "launch_year": launch_year,
            "launch_site": g("LAUNCH_SITE"), "owner": g("OWNER"), "status": g("OPS_STATUS_CODE"),
            "otype": g("OBJECT_TYPE")}

    L = [f"# {name or 'NORAD ' + str(catnr)}",
         f"NORAD {catnr}   Intl {intl or '?'}   ({meta['otype'] or 'object'})", "",
         "## Launch",
         f"- Launch date: {launch_date or launch_year + ' (from designator)'}",
         f"- Launch site: {meta['launch_site'] or 'n/a'}",
         f"- Owner / operator: {meta['owner'] or 'n/a'}",
         f"- Operational status: {meta['status'] or 'n/a'}", "",
         "## Orbit (from current TLE)",
         f"- Epoch: {epoch:%Y-%m-%d %H:%M:%S} UTC  (age {age_h:.1f} h)",
         f"- Period: {elem['period_min']:.1f} min ({1440 / elem['period_min']:.2f} rev/day)",
         f"- Inclination: {elem['inc']:.2f} deg",
         f"- Eccentricity: {elem['ecc']:.6f}",
         f"- Apogee / perigee: {elem['apogee_km']:.0f} / {elem['perigee_km']:.0f} km",
         f"- Semi-major axis: {elem['sma_km']:.0f} km",
         f"- RAAN / arg perigee: {elem['raan']:.1f} / {elem['argp']:.1f} deg", "",
         "## Current sub-satellite point",
         f"- Time: {when:%Y-%m-%d %H:%M:%S} UTC",
         f"- Lat / lon: {la:.3f}, {lo:.3f}",
         f"- Altitude: {al:.0f} km    Speed: {spd:.2f} km/s", "",
         f"Source: CelesTrak GP + SATCAT.  Page: {PAGE_URL.format(cat=catnr)}"]
    return "\n".join(L) + "\n", meta


# ------------------------------------------------------------------ layouts

def layout_4x6(z, ctx, name, catnr, meta, elem, now, epoch, when, track, page_url, track_min):
    la, lo, al, spd = now
    M = 40
    W, H = z.W, z.H

    z.text(M, M, 66, (name or f"NORAD {catnr}")[:24])
    z.text(M, M + 74, 36, f"NORAD {catnr}   {meta['intl'] or ''}".strip())
    z.hline(M, M + 120, W - 2 * M)

    # map: rendered at final size, so just threshold + pack (resizing here would
    # blur the hairlines back into oblivion)
    y_map = M + 140
    img = render_map(ctx, track, now, box_w=W - 80, box_h=820)
    _, mh = z.image(None, y_map, img, threshold=170)
    y_leg = y_map + mh + 10
    z.text(M, y_leg, 28, f"* current position    ground track: next {track_min:.0f} min")
    z.hline(M, y_leg + 36, W - 2 * M)

    # data card: two columns of native text
    y = y_leg + 54
    lh = 46
    left = [("LAUNCH", None),
            ("Date", meta["launch_date"] or meta["launch_year"]),
            ("Site", meta["launch_site"] or "n/a"),
            ("Owner", meta["owner"] or "n/a"),
            ("Type", meta["otype"] or "n/a"),
            ("Status", meta["status"] or "n/a")]
    right = [("ORBIT", None),
             ("Period", f"{elem['period_min']:.1f} min"),
             ("Incl", f"{elem['inc']:.2f} deg"),
             ("Ecc", f"{elem['ecc']:.5f}"),
             ("Apo/Per", f"{elem['apogee_km']:.0f}/{elem['perigee_km']:.0f} km"),
             ("Epoch age", f"{(when - epoch).total_seconds() / 3600:.1f} h")]

    def column(items, x0, wlabel):
        yy = y
        for k, v in items:
            if v is None:                               # section header
                z.text(x0, yy, 34, k)
                z.hline(x0, yy + 34, 360, 2)
                yy += 44
            else:
                z.text(x0, yy, 30, f"{k}:")
                z.text(x0 + wlabel, yy, 30, str(v)[:20])
                yy += lh

    column(left, M, 150)
    column(right, W // 2 + 10, 170)

    # current sub-point line, full width under both columns
    y_now = y + 44 + 6 * lh + 16
    z.hline(M, y_now, W - 2 * M)
    z.text(M, y_now + 14, 38, f"NOW {when:%Y-%m-%d %H:%MZ}")
    z.text(M, y_now + 60, 32, f"Lat {la:.2f}  Lon {lo:.2f}  Alt {al:.0f}km  {spd:.2f}km/s")
    y_now_end = y_now + 100

    # QR + caption centered between the NOW panel and the footer; sized from the
    # URL length so it is guaranteed to clear the footer.
    footer_y = H - M - 26
    region_top, region_bot = y_now_end + 16, footer_y - 20
    mods = qr_modules(len(page_url)) + 8     # +8 = 4-module quiet zone each side
    mag = max(4, min(9, (min(region_bot - region_top, 320) - 8) // mods))
    qr_side = z.qr_side(page_url, mag)
    qr_y = region_top + max(0, (region_bot - region_top - qr_side) // 2)
    z.qr(M, qr_y, page_url, mag)
    cap_x = M + qr_side + 24
    z.text(cap_x, qr_y + max(0, (qr_side - 140) // 2), 36,
           "Scan for the live CelesTrak page: current elements, upcoming passes, "
           "and decay status.", block=W - cap_x - M, lines=5)
    z.text(M, footer_y, 22, f"Generated {when:%Y-%m-%d %H:%MZ} - SGP4 propagation - "
                            "not for operational use")


def layout_2x1(z, name, catnr, meta, elem, now, when, page_url):
    """Compact card: key facts + a small CelesTrak QR (map omitted at this size)."""
    W, H = z.W, z.H
    M = 12
    la, lo, al, spd = now
    mods = qr_modules(len(page_url)) + 8
    mag = max(2, min(4, (H - 60) // mods))
    side = z.qr_side(page_url, mag)
    qx = W - M - side
    z.text(qx, M, 20, "CelesTrak")
    z.qr(qx, M + 22, page_url, mag)

    tw = qx - M - 8                       # text column width to the left of the QR
    z.text(M, M, 34, (name or "NORAD " + catnr)[:16], block=tw)
    z.text(M, M + 40, 22, f"NORAD {catnr}   {meta['intl'] or ''}")
    z.text(M, M + 70, 22, f"P {elem['period_min']:.1f}m   i {elem['inc']:.1f}deg")
    z.text(M, M + 100, 22, f"Apo/Per {elem['apogee_km']:.0f}/{elem['perigee_km']:.0f} km")
    z.text(M, M + 130, 22, f"Now {la:.1f},{lo:.1f}  {al:.0f} km")
    z.text(M, M + 160, 22, f"Speed {spd:.2f} km/s")
    z.text(M, H - 26, 18, f"{when:%Y-%m-%d %H:%MZ}  SGP4 - not for nav")


# -------------------------------------------------------------------- label

@register
class SatelliteLabel(Label):
    id = "satellite"
    name = "Satellite"
    description = "Ground track, orbit and launch facts for a NORAD catalog number (CelesTrak)."
    icon = "mdi:satellite-variant"
    Params = SatelliteParams

    def render(self, p: SatelliteParams, ctx: RenderContext) -> RenderResult:
        catnr = p.norad.lstrip("0") or "0"
        when = ctx.now.astimezone(dt.timezone.utc)
        page_url = p.qr_url or PAGE_URL.format(cat=catnr)

        name, l1, l2 = load_tle(catnr, ctx, p.tle)
        epoch = epoch_datetime(l1)
        sc = None if p.tle else load_satcat(catnr)
        period = 1440.0 / float(l2[52:63])
        minutes = p.duration or period
        prop = propagate(l1, l2, when, minutes, p.step)
        elem = orbital_elements(l2)
        now = prop["now"]
        report, meta = build_report(name, catnr, l1, sc, elem, now, epoch, when)

        z = ctx.zpl()
        if ctx.size.id == "2x1":
            layout_2x1(z, name, catnr, meta, elem, now, when, page_url)
        else:
            layout_4x6(z, ctx, name, catnr, meta, elem, now, epoch, when, prop["track"],
                       page_url, minutes)
        la, lo, al, spd = now
        data = {"name": name, "norad": catnr, "intl": meta["intl"], "lat": round(la, 4),
                "lon": round(lo, 4), "alt_km": round(al, 1), "speed_km_s": round(spd, 3),
                "period_min": round(elem["period_min"], 2), "inclination": elem["inc"],
                "tle_epoch": epoch.isoformat()}
        return RenderResult(z.build(), title=f"satellite-{catnr}", report=report, data=data)
