"""
Topographic map label (port of topolabel.py): contour lines for a bounding box,
with an interval chosen from the local relief (index lines every 5th, labeled),
the bounding coordinates, a scale bar and a QR to the same spot on CalTopo.

Elevation: AWS Terrain Tiles (Terrarium PNG), global, keyless:
  https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png
  elevation_m = R*256 + G + B/256 - 32768
Tiles are immutable, so they are cached in memory for a day.
"""

from __future__ import annotations

import io
import math
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .. import net
from ..errors import FetchError, LabelError
from ..zpl import qr_modules
from . import Label, RenderContext, RenderResult, register

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
CALTOPO = "https://caltopo.com/map.html#ll={lat:.5f},{lon:.5f}&z={z}&b=mbt"
M2FT = 3.280839895

NICE_M = [10, 20, 25, 40, 50, 100, 200, 250, 500, 1000, 2000]
NICE_FT = [20, 40, 80, 100, 200, 400, 500, 1000, 2000, 5000]


def parse_latlon(s: str) -> tuple[float, float]:
    try:
        a, b = (float(v) for v in str(s).split(","))
    except ValueError:
        raise ValueError(f"bad coordinate {s!r}; use LAT,LON e.g. 46.90,-121.80") from None
    if not (-85 <= a <= 85 and -180 <= b <= 180):
        raise ValueError(f"{s!r} is out of range (lat within +-85, lon within +-180)")
    return a, b


class TopoParams(BaseModel):
    corner1: str = Field("46.90,-121.80", title="Corner 1", description="One corner as LAT,LON")
    corner2: str = Field("46.78,-121.68", title="Corner 2", description="The opposite corner as LAT,LON")
    units: Literal["meters", "feet"] = Field("meters", description="Contour units")
    interval: Optional[float] = Field(None, gt=0, title="Contour interval",
                                      description="Contour interval (default: auto from relief)")
    zoom: Optional[int] = Field(None, ge=3, le=15, title="Tile zoom",
                                description="Terrain tile zoom (higher = finer, more tiles)")
    title: str = Field("TOPOGRAPHIC MAP", max_length=40)
    qr_url: Optional[str] = Field(None, title="QR URL", description="Override the CalTopo URL")

    @field_validator("corner1", "corner2")
    @classmethod
    def _check_corner(cls, v: str) -> str:
        a, b = parse_latlon(v)
        return f"{a:g},{b:g}"


# --------------------------------------------------------------- web mercator

def lon2x(lon, z):
    return (lon + 180.0) / 360.0 * (2 ** z)


def lat2y(lat, z):
    lat = max(-85.05112878, min(85.05112878, lat))
    return (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * (2 ** z)


def x2lon(x, z):
    return x / (2 ** z) * 360.0 - 180.0


def y2lat(y, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (2 ** z)))))


def pick_zoom(west, east, target_px=1400, zmax=14):
    span = (east - west) % 360 or 1e-6
    z = math.log2(target_px / (256.0 * span / 360.0))
    return max(3, min(zmax, int(round(z))))


# ------------------------------------------------------------------ DEM fetch

def _tile_range(south, west, north, east, z):
    xt0, xt1 = int(math.floor(lon2x(west, z))), int(math.floor(lon2x(east, z)))
    yt0, yt1 = int(math.floor(lat2y(north, z))), int(math.floor(lat2y(south, z)))
    return xt0, xt1, yt0, yt1


def fetch_dem(south, west, north, east, zoom=None, max_tiles=48, target_px=1400):
    """
    Returns (elev_m 2D array, (south, west, north, east) actually covered, zoom).
    North is row 0. Lowers the zoom if the box needs too many tiles.
    """
    import numpy as np
    from PIL import Image

    z = zoom or pick_zoom(west, east, target_px)
    xt0, xt1, yt0, yt1 = _tile_range(south, west, north, east, z)
    while z > 3 and (xt1 - xt0 + 1) * (yt1 - yt0 + 1) > max_tiles:
        z -= 1
        xt0, xt1, yt0, yt1 = _tile_range(south, west, north, east, z)

    nx, ny = xt1 - xt0 + 1, yt1 - yt0 + 1
    mosaic = np.full((ny * 256, nx * 256), np.nan, dtype=float)
    got = 0
    first_error = None
    for j in range(ny):
        for i in range(nx):
            try:
                data = net.fetch(TILE_URL.format(z=z, x=xt0 + i, y=yt0 + j),
                                 source="AWS Terrain Tiles", timeout=30, ttl=86400)
            except FetchError as e:
                first_error = first_error or e
                if got == 0 and i == 0 and j == 0:
                    raise FetchError(f"{e} (this network may block s3.amazonaws.com)") from e
                continue
            im = np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype(float)
            mosaic[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = (
                im[:, :, 0] * 256 + im[:, :, 1] + im[:, :, 2] / 256 - 32768)
            got += 1
    if got == 0:
        raise FetchError(f"no terrain tiles returned for that box ({first_error})")

    def px(lon):
        return (lon2x(lon, z) - xt0) * 256

    def py(lat):
        return (lat2y(lat, z) - yt0) * 256

    c0, c1 = max(0, int(round(px(west)))), min(mosaic.shape[1], int(round(px(east))))
    r0, r1 = max(0, int(round(py(north)))), min(mosaic.shape[0], int(round(py(south))))
    if c1 - c0 < 4 or r1 - r0 < 4:
        raise LabelError("that box is too small for the terrain resolution; widen it or "
                         "raise the zoom")
    crop = mosaic[r0:r1, c0:c1]
    cov = (y2lat((r1 + yt0 * 256) / 256.0, z), x2lon((c0 + xt0 * 256) / 256.0, z),
           y2lat((r0 + yt0 * 256) / 256.0, z), x2lon((c1 + xt0 * 256) / 256.0, z))
    return crop, cov, z


# -------------------------------------------------------------------- contours

def smooth(grid, sigma: float = 1.0):
    """Gaussian blur (scipy's default truncate=4, reflect edges) in plain numpy."""
    import numpy as np
    r = int(4 * sigma + 0.5)
    x = np.arange(-r, r + 1)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    k /= k.sum()
    nan = np.isnan(grid)
    g = np.where(nan, np.nanmean(grid), grid) if nan.any() else np.asarray(grid, dtype=float)
    ny, nx = g.shape
    p = np.pad(g, r, mode="symmetric")
    p = sum(k[i] * p[i:i + ny, :] for i in range(2 * r + 1))
    p = sum(k[i] * p[:, i:i + nx] for i in range(2 * r + 1))
    if nan.any():
        p[nan] = np.nan
    return p


def nice_interval(relief, units, target_lines=14):
    table = NICE_FT if units == "feet" else NICE_M
    raw = max(relief, 1e-6) / target_lines
    for step in table:
        if step >= raw:
            return step
    return table[-1]


def render_topo(ctx, elev_m, box_w, box_h, units="meters", interval=None, label_size=8,
                target_lines=14):
    """
    Contour map at device resolution (thin lines survive 1-bit thresholding).
    Index contours (every 5th) are heavier and labeled. Returns (image, info).
    """
    import numpy as np

    grid = smooth(elev_m * (M2FT if units == "feet" else 1.0))   # tame terrace stair-steps
    lo, hi = float(np.nanmin(grid)), float(np.nanmax(grid))
    relief = hi - lo
    ci = interval or nice_interval(relief, units, target_lines)
    idx = ci * 5

    k0, k1 = math.floor(lo / ci), math.ceil(hi / ci)
    levels = [k * ci for k in range(k0, k1 + 1) if lo <= k * ci <= hi]
    if len(levels) > 400:
        raise LabelError(f"a {ci:g} {units} interval would draw {len(levels)} contours; "
                         "use a larger interval")

    def is_index(lv):
        return abs(lv / idx - round(lv / idx)) < 1e-6
    indices = [lv for lv in levels if is_index(lv)]
    minors = [lv for lv in levels if not is_index(lv)]

    ny, nx = grid.shape
    aspect = ny / nx
    w = min(box_w, int(box_h / aspect))
    h = int(round(w * aspect))
    fig = ctx.figure(int(w) & ~7, h)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.set_xlim(0, nx)
    ax.set_ylim(ny, 0)                                   # row 0 = north at top
    X, Y = np.meshgrid(np.arange(nx), np.arange(ny))
    if minors:
        ax.contour(X, Y, grid, levels=sorted(minors), colors="black", linewidths=0.8)
    if indices:
        cs = ax.contour(X, Y, grid, levels=sorted(indices), colors="black", linewidths=1.8)
        if label_size:
            ax.clabel(cs, fmt=lambda v: f"{int(round(v))}", fontsize=label_size, inline=True,
                      inline_spacing=4)
    return ctx.image(fig), {"ci": ci, "idx": idx, "lo": lo, "hi": hi, "relief": relief,
                            "units": units}


# ------------------------------------------------------------- scale + labels

def ground_width_m(cov):
    south, west, north, east = cov
    latm = math.radians((south + north) / 2)
    return abs(east - west) * math.radians(1) * 6378137.0 * math.cos(latm)


def nice_scale(width_m):
    """Largest round distance that fits in ~45% of the map width; (meters, label)."""
    cap = width_m * 0.45
    pick = 50
    for d in [50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 50000]:
        if d <= cap:
            pick = d
    return pick, (f"{pick} m" if pick < 1000 else f"{pick / 1000:g} km")


# ------------------------------------------------------------------ layouts

def layout_4x6(z, ctx, elev, cov, p, caltopo_url):
    south, west, north, east = cov
    clat, clon = (south + north) / 2, (west + east) / 2
    M = 40
    W, H = z.W, z.H

    img, info = render_topo(ctx, elev, W - 80, 1180, units=p.units, interval=p.interval)
    u = "ft" if info["units"] == "feet" else "m"
    z.text(M, M, 60, p.title[:26])
    z.text(M, M + 66, 32, f"Contour interval {info['ci']:g} {u}   index {info['idx']:g} {u}")
    z.hline(M, M + 108, W - 2 * M)

    y_map = M + 126
    mw, mh = z.image(None, y_map, img)
    y_after = y_map + mh + 8
    z.hline(M, y_after, W - 2 * M)

    # scale bar (left) under the map
    wm = ground_width_m(cov)
    bar_m, bar_lbl = nice_scale(wm)
    bar = int(mw * bar_m / wm) if wm else 100
    bar = max(60, min(bar, W - 2 * M - 200))
    ys = y_after + 18
    z.box(M, ys + 22, bar, 6, 6)
    z.box(M, ys + 22, 4, 20, 4)
    z.box(M + bar, ys + 22, 4, 20, 4)
    z.text(M + bar + 14, ys + 10, 30, bar_lbl)
    # next to the scale label, not at the far right: a tall map pushes the QR
    # caption up into that corner
    z.text(M + bar + 14 + 18 * len(bar_lbl) + 40, ys + 10, 30, "N (up)")

    # bounding coordinates (decimal), bottom-left. The separator spans only the
    # left column so it doesn't run under the QR caption on the right.
    yb = ys + 70
    z.hline(M, yb, 760)
    z.text(M, yb + 12, 34, "BOUNDING BOX (decimal)")
    z.text(M, yb + 54, 30, f"NW {north:.5f}, {west:.5f}")
    z.text(M, yb + 92, 30, f"SE {south:.5f}, {east:.5f}")
    z.text(M, yb + 130, 30, f"Center {clat:.5f}, {clon:.5f}")
    z.text(M, yb + 168, 28, f"Elev {info['lo']:.0f}-{info['hi']:.0f} {u}   "
                            f"relief {info['relief']:.0f} {u}")

    # QR to CalTopo, bottom-right, with guaranteed clearance above the footer
    footer_y = H - M - 22
    gap = 46
    mods = qr_modules(len(caltopo_url)) + 8
    mag = max(4, min(8, 300 // mods))
    side = z.qr_side(caltopo_url, mag)
    qr_x, qr_y = W - M - side, footer_y - gap - side
    z.text(qr_x, qr_y - 30, 26, "CalTopo")
    z.qr(qr_x, qr_y, caltopo_url, mag)
    z.text(M, footer_y, 22, f"Terrain: AWS Terrain Tiles (SRTM/other) - generated "
                            f"{ctx.now:%Y-%m-%d %H:%M %Z} - not for navigation")
    return info


def layout_2x1(z, ctx, elev, cov, p, caltopo_url):
    """Small card: contour map on the left, key numbers + QR on the right."""
    south, west, north, east = cov
    M = 12
    W, H = z.W, z.H
    img, info = render_topo(ctx, elev, 300, H - 2 * M, units=p.units, interval=p.interval,
                            label_size=0, target_lines=8)
    u = "ft" if info["units"] == "feet" else "m"
    mw, _ = z.image(M, M, img)
    x = M + mw + 12
    tw = W - x - M
    z.text(x, M, 24, p.title[:18], block=tw)
    z.text(x, M + 32, 18, f"CI {info['ci']:g} {u}  idx {info['idx']:g} {u}")
    z.text(x, M + 56, 18, f"NW {north:.4f},{west:.4f}")
    z.text(x, M + 78, 18, f"SE {south:.4f},{east:.4f}")
    z.text(x, M + 100, 18, f"{info['lo']:.0f}-{info['hi']:.0f} {u}")
    mods = qr_modules(len(caltopo_url)) + 8
    mag = max(2, min(3, (H - M - 130) // mods))
    side = z.qr_side(caltopo_url, mag)
    z.qr(W - M - side, H - M - side, caltopo_url, mag)
    z.text(x, H - M - 20, 16, "not for nav")
    return info


# -------------------------------------------------------------------- label

@register
class TopoLabel(Label):
    id = "topo"
    name = "Topographic map"
    description = "Contour map for a bounding box, with scale, coordinates and a CalTopo QR."
    icon = "mdi:terrain"
    Params = TopoParams

    def render(self, p: TopoParams, ctx: RenderContext) -> RenderResult:
        (la1, lo1), (la2, lo2) = parse_latlon(p.corner1), parse_latlon(p.corner2)
        south, north = sorted((la1, la2))
        west, east = sorted((lo1, lo2))
        if north - south < 1e-4 or east - west < 1e-4:
            raise LabelError("those corners are too close together to make a map")
        box_px = ctx.dots(1120 if ctx.size.id == "4x6" else 300)
        elev, cov, zoom = fetch_dem(south, west, north, east, zoom=p.zoom,
                                    target_px=int(box_px * 1.25),
                                    max_tiles=48 if ctx.size.id == "4x6" else 16)
        clat, clon = (cov[0] + cov[2]) / 2, (cov[1] + cov[3]) / 2
        caltopo = p.qr_url or CALTOPO.format(lat=clat, lon=clon, z=min(16, zoom + 2))

        z = ctx.zpl()
        if ctx.size.id == "2x1":
            info = layout_2x1(z, ctx, elev, cov, p, caltopo)
        else:
            info = layout_4x6(z, ctx, elev, cov, p, caltopo)
        data = {"bbox": {"south": cov[0], "west": cov[1], "north": cov[2], "east": cov[3]},
                "zoom": zoom, "interval": info["ci"], "units": info["units"],
                "elev_min": round(info["lo"], 1), "elev_max": round(info["hi"], 1),
                "caltopo_url": caltopo}
        return RenderResult(z.build(), title="topo", data=data)
