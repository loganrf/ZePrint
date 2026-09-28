# ZePrint

A small Docker service for Zebra label printers (built around a **GX430t**) that
turns data into printed labels. It includes a web UI, a REST API, a CLI and Home
Assistant integration.

| Label | What prints | Data source |
|---|---|---|
| **Satellite** | Ground-track map, orbit and launch facts, QR to CelesTrak | CelesTrak GP + SATCAT |
| **Tides** | Day's tide curve, highs/lows, live level, sun and moon, QR to the station | NOAA CO-OPS |
| **Topographic map** | Contour map of a bounding box, scale bar, coordinates, QR to CalTopo | AWS Terrain Tiles |
| **Weather** | Current conditions, outlook, and the day's wind at a marina | Open-Meteo |
| **Address label** | Mailing label from typed addresses, with an optional banner, reference line and barcode. On 4x6: from + to. On 2x1: one address per label. | – |
| **Image / shipping label** | Any PNG/JPG/GIF/TIFF/WebP/PDF, e.g. a UPS/FedEx/USPS label. It's trimmed, rotated and scaled to fit, with crisp barcodes. | upload or URL |
| **Test label** | Registration marks, inch ruler, 1/2/4/8-dot hairlines, Code 128 | – |

### Mailing labels

- **Address label.** Addresses are typed one line per row, or separated by `|`.
  - **On 4x6**, the return address sits small at the top and the recipient large
    below, with an optional banner (FRAGILE, PRIORITY), reference line and Code
    128. Set **Print** to `to` or `from` to print one address alone.
  - **On 2x1**, each label holds one address. `both` prints two labels in one
    job: the return address, then the recipient. The banner, reference and
    barcode go on the recipient's label (or the only one).

  Text is auto-sized to fill the space, so long addresses still fit. Save your
  return address with **Save as defaults** and you only enter the recipient.
- **Image / shipping label.** Upload a carrier label (or pass a URL) and print it
  on 4x6.
  - **Pages:** multi-page PDFs print one label per page (`pages=all`, `2-3`, …).
  - **Letter-size pages:** if the label shares the page with instructions, set
    `crop=top` (or `bottom`, `left`, `top-left`, …) and it's isolated.
  - **Orientation:** landscape art turns to portrait automatically. Set `rotate`
    if a carrier's label comes out upside down.
  - **Dither** is for photos and logos. Leave it off for barcodes.
  - **ZPL from the carrier:** if the carrier offers a ZPL download, send it raw
    instead (**ZPL** tab, or `/api/printers/{id}/raw`). Carrier ZPL is usually
    203 dpi, so it prints at about two-thirds size on a 300-dpi printer. The
    image route scales to fit.

Every label has a **4x6** (portrait) and a **2x1** (small landscape) layout. It
prints correctly on **203, 300 and 600 dpi** printers: layouts are drawn in
300-dpi units and scaled when the ZPL is generated. None of the data sources
need an API key.

## Quick start

```sh
git clone https://github.com/loganrf/ZePrint && cd ZePrint
cp .env.example .env        # set ZEPRINT_PRINTER_URI=tcp://<printer-ip>:9100, DPI, size, TZ
docker compose up -d --build
open http://localhost:3231
```

The `ZEPRINT_PRINTER_*` variables only seed the **first** start. After that,
manage printers in the web UI (**Printers** tab). Settings live in
`config.json` in the `zeprint-data` volume.

### Deploying as a stack (Dockhand, Portainer, Dockge…)

`deploy/compose.yaml` pulls the prebuilt multi-arch image
`ghcr.io/loganrf/zeprint` (amd64 + arm64), so it needs no build context. The
image is published by `.github/workflows/publish.yml` on every push to `main`,
on `v*` tags, or when you run the workflow manually.

- **Git stack:** repository `https://github.com/loganrf/ZePrint` (public, so no
  credentials needed), branch `main`, compose file `deploy/compose.yaml`.
  Enable auto-sync or the webhook to redeploy on push.
- **Editor stack:** paste `deploy/compose.yaml` into the stack editor.

In both cases, put the variables from `.env.example` in the stack's environment.
At minimum set `ZEPRINT_PRINTER_URI=tcp://<printer-ip>:9100`, `TZ`, and
optionally the `MQTT_*` variables. If the GHCR package shows as private after
its first publish, either make it public (package settings on GitHub) or add
`ghcr.io` as a registry with a read-only token.

### Connecting the printer

| How it's attached | Printer URI | Notes |
|---|---|---|
| Network (ZebraNet / Ethernet) | `tcp://192.168.1.50:9100` | Raw port 9100 (some setups use 6101). Works everywhere. |
| USB on a Linux Docker host | `usb:///dev/usb/lp0` | Add `devices: ["/dev/usb/lp0:/dev/usb/lp0"]`. The entrypoint grants the container user access to the device. |
| USB on a Mac (or any CUPS box) | `ipp://host.docker.internal:631/printers/GX430t` | Docker Desktop can't pass USB through, so share the raw CUPS queue instead (see below). |

**Mac + USB.** Create the raw queue once (`zebra_print.py` did this;
`lpadmin -p GX430t -E -v <usb uri> -m raw` does too). Then share it:
`cupsctl --share-printers` and `lpadmin -p GX430t -o printer-is-shared=true`.
The container reaches it at `host.docker.internal`. `cups://host/QUEUE` is
shorthand for `ipp://host:631/printers/QUEUE`. Use `ipps://…?insecure` for
CUPS with a self-signed certificate.

### Printer settings

Each printer has a **DPI** (203/300/600), the **label size loaded** (4x6 or 2x1),
darkness (`~SD`, 0-30), speed and media (direct thermal or ribbon). One printer
is the **default**. Prints use the printer's loaded size unless a request asks
for another one. Use **Calibrate** after changing stock. It saves the geometry,
then runs the gap-sensor calibration (`~JC`).

**Status** (`~HS`/`~HI`) reports ready, paper out, head open, paused, and so on,
plus the model and the DPI the printer reports. It works for TCP and USB
printers.

## Environment

| Variable | Default | |
|---|---|---|
| `TZ` / `ZEPRINT_TIMEZONE` | UTC | Used for "now" markers and timestamps. Can be overridden in Settings. |
| `ZEPRINT_PRINTER_URI`, `_NAME`, `_ID`, `_DPI` | – | First-run printer. |
| `ZEPRINT_LABEL_SIZE`, `ZEPRINT_DARKNESS`, `ZEPRINT_SPEED`, `ZEPRINT_MEDIA` | 4x6, 22, 2, direct | First-run printer. |
| `ZEPRINT_API_TOKEN` | – | Require `Authorization: Bearer <token>` on `/api/*`. |
| `ZEPRINT_DATA_DIR` | `/data` | Config, caches, `plugins/`. |
| `ZEPRINT_PORT` | 8080 | Port inside the container. |
| `ZEPRINT_HTTP_PORT` | 3231 | Host port in the compose files. |
| `PUID` / `PGID` | 1000 | Owner of the data directory. |
| `MQTT_HOST` (+ `MQTT_*`) | – | Enables Home Assistant discovery; see [docs/home-assistant.md](docs/home-assistant.md). |

## REST API

Interactive docs are served at `/docs`. The essentials:

```sh
# print with a label's defaults on the default printer
curl -X POST localhost:3231/api/labels/tides/print

# parameters can be flat or nested under "params"; ?wait=N blocks until done
curl -X POST 'localhost:3231/api/labels/tides/print?wait=30' \
     -H 'Content-Type: application/json' -d '{"station": "9446484", "size": "2x1"}'

# preview as PNG (POST with a body, or GET with query params for image cards)
curl -o tide.png 'localhost:3231/api/labels/tides/preview.png?size=2x1&station=9446484'

# render without printing: ZPL + markdown report + machine-readable data
curl -X POST localhost:3231/api/labels/weather/render -d '{}' -H 'Content-Type: application/json'

# raw ZPL straight to a printer
curl -X POST localhost:3231/api/printers/zebra/raw --data-binary @label.zpl

# a carrier's shipping label PDF, in one call (every page, label in the top half)
curl --data-binary @usps.pdf 'localhost:3231/api/print-file?pages=all&crop=top&wait=30'

# an address label; save your return address once as the label's default
curl -X PUT localhost:3231/api/labels/address/defaults -H 'Content-Type: application/json' \
     -d '{"sender": "A. Sender|2 Elm St|Tacoma, WA 98402"}'
curl -X POST localhost:3231/api/labels/address/print -H 'Content-Type: application/json' \
     -d '{"to": "Jane Doe|1 Main St|Seattle, WA 98101", "note": "FRAGILE"}'
```

| | |
|---|---|
| `GET /api/labels` | Labels, their JSON-schema parameters and saved defaults. |
| `PUT /api/labels/{id}/defaults` | Save default parameters (used by buttons, HA and the API). |
| `POST /api/labels/{id}/preview` · `/render` · `/print` | Body `{params…, printer?, size?, copies?}`. |
| `GET /api/jobs[/{id}]` | Job history and status (`?wait=N`). |
| `GET/POST/PATCH/DELETE /api/printers[/{id}]` | Manage printers. |
| `GET /api/printers/{id}/status` | Live `~HS`/`~HI` status. |
| `POST /api/printers/{id}/test` · `/calibrate` · `/raw` | Printer actions. |
| `GET/PATCH /api/settings` | Default printer and timezone. |
| `POST /api/zpl/preview` | Render arbitrary ZPL to PNG. |
| `POST /api/uploads` · `GET/DELETE /api/uploads/{id}` | Store a PDF or image (raw body) for the image label. Uploads expire after 7 days. |
| `POST /api/print-file` | Upload and print a PDF or image in one call. The query string takes image-label options, `printer`, `size`, `copies` and `wait`. |

## Home Assistant

Set `MQTT_HOST`, and HA discovers a **ZePrint** device. It gets a button per
label, a "last print job" sensor and a status sensor per printer. Automations
can publish to `zeprint/print/<label>` with JSON parameters. No MQTT? The REST
API works with `rest_command`. Examples for both are in
[docs/home-assistant.md](docs/home-assistant.md).

## CLI

The container image (and `pip install .`) includes the `zeprint` command:

```sh
zeprint labels                                    # labels and their parameters
zeprint render tides -p station=9446484 --size 2x1 --dpi 203 --preview t.png
zeprint render satellite -p norad=25544 --zpl iss.zpl --report
zeprint print weather                             # default printer
zeprint print test --uri tcp://192.168.1.50:9100  # one-off printer
zeprint print image -p image=@usps.pdf -p pages=all -p crop=top
zeprint print address -p "to=Jane Doe|1 Main St|Seattle, WA 98101" --size 2x1
```

## Adding a label (plugins)

Drop a `.py` file into `data/plugins/` and restart. It appears in the UI, the
API, the CLI and Home Assistant with no other wiring. Coordinates are in
300-dpi "design units", so a 4x6 label is 1200 x 1800 and a 2x1 is 600 x 300.
They are scaled for the printer's real DPI.

```python
from pydantic import BaseModel, Field
from zeprint.labels import Label, RenderResult, register

class Params(BaseModel):
    text: str = Field("Hello", description="What to print")

@register
class Hello(Label):
    id = "hello"
    name = "Hello"
    description = "A greeting."
    icon = "mdi:hand-wave"
    Params = Params

    def render(self, p, ctx):
        z = ctx.zpl()                                  # sized for ctx.size / ctx.dpi
        z.text(40, 40, 90 if ctx.size.id == "4x6" else 50, p.text)
        z.qr(40, 160, "https://example.com", mag=5)
        # matplotlib art: ctx.figure(w, h) -> ctx.image(fig) -> z.image(x, y, img)
        return RenderResult(z.build(), title="hello")
```

The built-in labels in `zeprint/labels/` are fuller examples. The `data` dict on
`RenderResult` shows up in `/render` responses, for use by dashboards and
automations.

## Development

```sh
pip install -e '.[dev]'
pytest                                   # offline: data sources and printer are faked
ZEPRINT_DATA_DIR=./data zeprint serve    # http://localhost:8080
```

Layout: `zeprint/zpl/` holds the ZPL builder, raster encoder and preview
renderer. `zeprint/labels/` holds the label plugins, `zeprint/printing/` the
transports and status parsing, `service.py` the core, `api.py` the REST API
and web UI, and `integrations/` the MQTT/HA bridge.

### Provenance

The labels are ports of the original `satlabel.py`, `tidelabel.py`,
`topolabel.py`, `weather_report.py` and `zebra_print.py` scripts. The
`zebra_image.py` raster helper they imported was reimplemented in
`zeprint/zpl/raster.py`, using Zebra's ASCII compression. `world_land.json` was
regenerated from Natural Earth 1:110m land (public domain).

Tide, map and orbit labels are **not for navigation or operational use**.
