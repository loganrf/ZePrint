"""
Command line.

    python -m zeprint serve                         # web UI + API on :8080
    python -m zeprint labels                        # list labels and their parameters
    python -m zeprint render tides -p station=9446484 --size 2x1 --preview t.png
    python -m zeprint render satellite -p norad=25544 --zpl iss.zpl --dpi 203
    python -m zeprint print weather --printer zebra
    python -m zeprint print image -p image=@ups-label.pdf -p pages=all
    python -m zeprint print address -p "to=Jane Doe|1 Main St|Seattle, WA 98101"
    python -m zeprint print topo -p corner1=46.9,-121.8 -p corner2=46.78,-121.68 \\
        --uri tcp://192.168.1.50:9100                # one-off printer, no config needed

Settings live in $ZEPRINT_DATA_DIR (default ./data), shared with the server.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .schemas import MAX_COPIES


def _params(pairs: list[str], svc=None) -> dict:
    """-p KEY=VALUE pairs. KEY=@path uploads a local file and passes its id
    (e.g. -p image=@label.pdf)."""
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"bad -p {pair!r}; use KEY=VALUE")
        k, v = pair.split("=", 1)
        if v.startswith("@") and svc is not None:
            path = os.path.expanduser(v[1:])
            try:
                with open(path, "rb") as f:
                    v = svc.uploads.put(f.read(), os.path.basename(path))["id"]
            except OSError as e:
                raise SystemExit(f"can't read {path}: {e}") from None
        out[k.strip()] = v
    return out


def cmd_serve(args) -> int:
    import uvicorn
    uvicorn.run("zeprint.api:app_from_env", factory=True, host=args.host, port=args.port,
                log_level=args.log_level, proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_labels(args, svc) -> int:
    from . import labels
    for cls in labels.all_labels():
        print(f"{cls.id:10s} {cls.name}  [{', '.join(cls.sizes)}]")
        print(f"{'':10s} {cls.description}")
        for name, f in cls.Params.model_fields.items():
            default = f.default if f.default is not None else ""
            print(f"{'':11s}-p {f'{name}={default}':<28} {f.description or ''}")
        print()
    return 0


def cmd_render(args, svc) -> int:
    from .zpl import render_png
    result = svc.render(args.label, _params(args.param, svc), printer_id=args.printer,
                        size=args.size, dpi=args.dpi, copies=args.copies)
    if args.zpl:
        with open(args.zpl, "w") as f:
            f.write(result.zpl)
        print(f"ZPL -> {args.zpl} ({len(result.zpl) / 1024:.0f} KiB)", file=sys.stderr)
    if args.preview:
        with open(args.preview, "wb") as f:
            f.write(render_png(result.zpl))
        print(f"preview -> {args.preview}", file=sys.stderr)
    if args.report and result.report:
        print(result.report)
    if args.data:
        print(json.dumps(result.data, indent=2, default=str))
    if not (args.zpl or args.preview or args.report or args.data):
        sys.stdout.write(result.zpl)
    return 0


def cmd_print(args, svc) -> int:
    printer_id = args.printer
    if args.uri:
        from pydantic import ValidationError
        from .config import PrinterConfig
        from .service import format_validation
        current = svc.settings.printer(printer_id)
        # the configured printer's settings (media, darkness, speed...) unless overridden
        base = current.model_dump() if current else {}
        try:
            adhoc = PrinterConfig(**{**base, "id": "cli", "name": "command line", "uri": args.uri,
                                     "dpi": args.dpi or base.get("dpi", 300),
                                     "label_size": args.size or base.get("label_size", "4x6")})
        except ValidationError as e:
            print(f"error: {format_validation(e)}", file=sys.stderr)
            return 2
        result = svc.render(args.label, _params(args.param, svc), printer=adhoc,
                            size=adhoc.label_size, copies=args.copies)
        n = svc.send(adhoc, result.zpl, result.title)
        print(f"sent {n} bytes to {args.uri}", file=sys.stderr)
        return 0
    svc.start()
    try:
        job = svc.print_label(args.label, _params(args.param, svc), printer_id=printer_id,
                              size=args.size, copies=args.copies, source="cli")
        job = svc.jobs.wait(job.id, 300)
    finally:
        svc.stop()
    if job.status != "done":
        print(f"print failed: {job.error or job.status}", file=sys.stderr)
        return 1
    print(f"printed {job.title} on {job.printer} ({job.bytes} bytes)", file=sys.stderr)
    return 0


def _copies(v: str) -> int:
    try:
        n = int(v)
    except ValueError:
        n = 0
    if not 1 <= n <= MAX_COPIES:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {MAX_COPIES}")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zeprint", description="Label printing for Zebra printers.")
    ap.add_argument("--data-dir", default=os.environ.get("ZEPRINT_DATA_DIR", "data"))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the web UI + REST API (default)")
    s.add_argument("--host", default=os.environ.get("ZEPRINT_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("ZEPRINT_PORT", 8080)))
    s.add_argument("--log-level", default=os.environ.get("ZEPRINT_LOG_LEVEL", "info"))

    sub.add_parser("labels", help="list labels and their parameters")

    for name, helptext in (("render", "render a label to ZPL / PNG without printing"),
                           ("print", "render and print a label")):
        r = sub.add_parser(name, help=helptext)
        r.add_argument("label")
        r.add_argument("-p", "--param", action="append", metavar="KEY=VALUE",
                       help="label parameter (repeatable)")
        r.add_argument("--size", help="4x6 or 2x1 (default: the printer's stock)")
        r.add_argument("--dpi", type=int, choices=[203, 300, 600])
        r.add_argument("--printer", help="printer id (default: the default printer)")
        r.add_argument("--copies", type=_copies, default=1, help=f"1-{MAX_COPIES}")
        if name == "render":
            r.add_argument("--zpl", metavar="FILE", help="write the ZPL here")
            r.add_argument("--preview", metavar="PNG", help="write a PNG preview here")
            r.add_argument("--report", action="store_true", help="print the text report")
            r.add_argument("--data", action="store_true", help="print the label's data as JSON")
        else:
            r.add_argument("--uri", help="print to this printer URI instead of a configured one")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.environ["ZEPRINT_DATA_DIR"] = args.data_dir
    if args.cmd in (None, "serve"):
        if args.cmd is None:
            args = ap.parse_args(["serve"])
        return cmd_serve(args)

    from .errors import ZePrintError
    from .service import ZePrint
    svc = ZePrint(args.data_dir)
    try:
        return {"labels": cmd_labels, "render": cmd_render, "print": cmd_print}[args.cmd](args, svc)
    except ZePrintError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
