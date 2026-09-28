"""
REST API + web UI.

Label requests accept parameters either nested or flat - these are equivalent:

    POST /api/labels/tides/print   {"params": {"station": "9446484"}, "size": "2x1"}
    POST /api/labels/tides/print   {"station": "9446484", "size": "2x1"}

Set ZEPRINT_API_TOKEN to require ``Authorization: Bearer <token>`` (or
``?token=``) on everything under /api except /api/health.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from . import __version__, labels
from .config import PrinterConfig, Settings, valid_timezone
from .errors import InvalidRequest, ZePrintError
from .jobs import Job
from .schemas import LabelRequest
from .service import ZePrint, format_validation
from .zpl import DPIS, get_size, render_png

log = logging.getLogger(__name__)

_RESERVED = {"params", "printer", "size", "dpi", "copies", "token"}
MAX_BODY = 32 << 20          # raw ZPL / preview uploads


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_printer: Optional[str] = None
    timezone: Optional[str] = Field(None, description="IANA name, or '' to follow TZ")


class PrinterPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Optional[str] = None
    name: Optional[str] = None
    uri: Optional[str] = None
    dpi: Optional[Literal[203, 300, 600]] = None
    label_size: Optional[str] = None
    darkness: Optional[int] = None
    speed: Optional[int] = None
    media: Optional[Literal["direct", "ribbon"]] = None
    default: Optional[bool] = None


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise HTTPException(413, f"body larger than {MAX_BODY >> 20} MiB")
    data = await request.body()
    if len(data) > MAX_BODY:
        raise HTTPException(413, f"body larger than {MAX_BODY >> 20} MiB")
    return data


def _job_response(svc: ZePrint, job: Job, wait: float) -> JSONResponse:
    """Blocks while waiting - call from sync endpoints or via run_in_threadpool."""
    if wait:
        job = svc.jobs.wait(job.id, wait)
    code = {"done": 200, "error": 502}.get(job.status, 202)
    return JSONResponse(job.model_dump(mode="json"), status_code=code)


def create_app(service: ZePrint | None = None, *, token: str | None = None,
               start_integrations: bool = True) -> FastAPI:
    svc = service or ZePrint.from_env()
    token = token if token is not None else os.environ.get("ZEPRINT_API_TOKEN") or None
    bridges: list = []

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc.start()
        if start_integrations:
            from .integrations import start_all
            bridges.extend(start_all(svc, svc.env))
        yield
        for b in bridges:
            b.stop()
        svc.stop()

    app = FastAPI(title="ZePrint", version=__version__, lifespan=lifespan,
                  description="Label printing service for Zebra printers.")
    app.state.zeprint = svc

    @app.exception_handler(ZePrintError)
    async def _zeprint_error(request: Request, exc: ZePrintError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    async def auth(request: Request):
        if not token:
            return
        header = request.headers.get("authorization", "")
        supplied = (header[7:] if header.lower().startswith("bearer ")
                    else request.headers.get("x-api-key") or request.query_params.get("token"))
        if not supplied or not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "missing or invalid API token",
                                headers={"WWW-Authenticate": "Bearer"})

    public = APIRouter(prefix="/api")
    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])

    @public.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    # ------------------------------------------------------------------ info

    @api.get("/info")
    def info():
        s = svc.settings
        return {"version": __version__, "sizes": svc.sizes(), "dpis": list(DPIS),
                "labels": [c.id for c in labels.all_labels()], "plugins": svc.plugins,
                "default_printer": s.default_printer, "timezone": str(svc.config.timezone()),
                "auth": bool(token), "integrations": [b.name for b in bridges]}

    @api.get("/sizes")
    def sizes():
        return svc.sizes()

    # ---------------------------------------------------------------- labels

    @api.get("/labels")
    def list_labels():
        return [svc.describe_label(c) for c in labels.all_labels()]

    @api.get("/labels/{label_id}")
    def get_label(label_id: str):
        return svc.describe_label(type(svc.label(label_id)))

    @api.put("/labels/{label_id}/defaults")
    def put_label_defaults(label_id: str, params: dict[str, Any] = Body(default_factory=dict)):
        """Saved defaults apply to every print of this label (buttons, HA, API)."""
        return {"label": label_id, "defaults": svc.save_label_defaults(label_id, params)}

    def _render(label_id: str, req: LabelRequest):
        return svc.render(label_id, req.merged(), printer_id=req.printer, size=req.size,
                          dpi=req.dpi, copies=req.copies)

    @api.post("/labels/{label_id}/preview", response_class=Response,
              responses={200: {"content": {"image/png": {}}}})
    def preview(label_id: str, req: Optional[LabelRequest] = Body(None)):
        """Render the label and return it as a PNG (no printing)."""
        result = _render(label_id, req or LabelRequest())
        return Response(render_png(result.zpl), media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @api.get("/labels/{label_id}/preview.png", response_class=Response,
             responses={200: {"content": {"image/png": {}}}})
    def preview_get(label_id: str, request: Request):
        """GET form for image cards: query string = label params (+ printer/size/dpi)."""
        q = dict(request.query_params)
        try:
            req = LabelRequest(printer=q.get("printer"), size=q.get("size"),
                               dpi=int(q["dpi"]) if q.get("dpi") else None,
                               params={k: v for k, v in q.items() if k not in _RESERVED})
        except (ValidationError, ValueError) as e:
            raise InvalidRequest(format_validation(e) if isinstance(e, ValidationError)
                                 else str(e)) from None
        result = _render(label_id, req)
        return Response(render_png(result.zpl), media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @api.post("/labels/{label_id}/render")
    def render(label_id: str, req: Optional[LabelRequest] = Body(None),
               preview: bool = Query(False, description="Include a base64 PNG preview")):
        """Render without printing: ZPL, markdown report and machine-readable data."""
        req = req or LabelRequest()
        result = _render(label_id, req)
        out = {"label": label_id, "title": result.title, "zpl": result.zpl,
               "report": result.report, "data": result.data}
        if preview:
            out["preview_png"] = base64.b64encode(render_png(result.zpl)).decode()
        return out

    @api.post("/labels/{label_id}/print")
    def print_label(label_id: str, req: Optional[LabelRequest] = Body(None),
                    wait: float = Query(0, ge=0, le=300,
                                        description="Seconds to wait for the job to finish")):
        req = req or LabelRequest()
        job = svc.print_label(label_id, req.merged(), printer_id=req.printer, size=req.size,
                              copies=req.copies)
        return _job_response(svc, job, wait)

    # ------------------------------------------------------------------ jobs

    @api.get("/jobs")
    def list_jobs(limit: int = Query(50, ge=1, le=200)):
        return [j.model_dump(mode="json") for j in svc.jobs.list(limit)]

    @api.get("/jobs/{job_id}")
    def get_job(job_id: str, wait: float = Query(0, ge=0, le=300)):
        return _job_response(svc, svc.jobs.get(job_id), wait)

    # -------------------------------------------------------------- settings

    @api.get("/settings")
    def get_settings():
        s = svc.settings.model_dump(mode="json")
        s["effective_timezone"] = str(svc.config.timezone())
        return s

    @api.patch("/settings")
    def patch_settings(patch: SettingsPatch):
        fields = patch.model_fields_set

        def fn(s: Settings):
            if "default_printer" in fields and patch.default_printer is not None:
                if patch.default_printer not in [p.id for p in s.printers]:
                    raise InvalidRequest(f"no printer {patch.default_printer!r}")
                s.default_printer = patch.default_printer
            if "timezone" in fields:
                try:
                    s.timezone = valid_timezone(patch.timezone) if patch.timezone else None
                except ValueError as e:
                    raise InvalidRequest(str(e)) from None
            return s
        svc.update_settings(fn)
        return get_settings()

    # -------------------------------------------------------------- printers

    @api.get("/printers")
    def list_printers():
        s = svc.settings
        return [{**p.model_dump(mode="json"), "default": p.id == s.default_printer}
                for p in s.printers]

    @api.get("/printers/{printer_id}")
    def get_printer(printer_id: str):
        p = svc.printer(printer_id)
        return {**p.model_dump(mode="json"), "default": p.id == svc.settings.default_printer}

    @api.post("/printers", status_code=201)
    def add_printer(body: dict[str, Any] = Body(...)):
        try:
            make_default = TypeAdapter(bool).validate_python(body.pop("default", False))
            printer = PrinterConfig.model_validate(body)
        except ValidationError as e:
            raise InvalidRequest(format_validation(e)) from None
        svc.save_printer(printer, make_default=make_default)
        return get_printer(printer.id)

    @api.patch("/printers/{printer_id}")
    def patch_printer(printer_id: str, patch: PrinterPatch):
        current = svc.printer(printer_id).model_dump()
        changes = patch.model_dump(exclude_unset=True)
        make_default = changes.pop("default", None)       # None = leave as is
        try:
            printer = PrinterConfig.model_validate({**current, **changes})
        except ValidationError as e:
            raise InvalidRequest(format_validation(e)) from None
        svc.save_printer(printer, replace_id=printer_id, make_default=make_default)
        return get_printer(printer.id)

    @api.delete("/printers/{printer_id}", status_code=204)
    def delete_printer(printer_id: str):
        svc.delete_printer(printer_id)
        return Response(status_code=204)

    @api.get("/printers/{printer_id}/status")
    def printer_status(printer_id: str):
        return svc.printer_status(printer_id)

    @api.post("/printers/{printer_id}/test")
    def printer_test(printer_id: str, size: Optional[str] = None,
                     wait: float = Query(0, ge=0, le=300)):
        return _job_response(svc, svc.print_label("test", printer_id=printer_id, size=size), wait)

    @api.post("/printers/{printer_id}/calibrate")
    def printer_calibrate(printer_id: str, size: Optional[str] = None,
                          wait: float = Query(0, ge=0, le=300)):
        """Save media geometry and run the gap-sensor calibration (feeds a few labels)."""
        return _job_response(svc, svc.calibrate(printer_id, size), wait)

    @api.post("/printers/{printer_id}/raw")
    async def printer_raw(printer_id: str, request: Request,
                          wait: float = Query(0, ge=0, le=300)):
        """Send the request body to the printer untouched (ZPL, EPL, SGD commands...)."""
        data = await _read_body(request)
        job = svc.print_raw(data, printer_id, title="raw ZPL")
        return await run_in_threadpool(_job_response, svc, job, wait)

    # ------------------------------------------------------------------ zpl

    @api.post("/zpl/preview", response_class=Response,
              responses={200: {"content": {"image/png": {}}}})
    async def zpl_preview(request: Request, size: str = "4x6", dpi: int = 300):
        """Preview arbitrary ZPL. size/dpi only matter when it has no ^PW/^LL."""
        zpl = (await _read_body(request)).decode("utf-8", "replace")
        if "^XA" not in zpl.upper():
            raise InvalidRequest("that doesn't look like ZPL (no ^XA)")
        if dpi not in DPIS:
            raise InvalidRequest(f"dpi must be one of {', '.join(map(str, DPIS))}")
        try:
            dots = get_size(size).dots(dpi)
        except ValueError as e:
            raise InvalidRequest(str(e)) from None
        png = await run_in_threadpool(render_png, zpl, dots)
        return Response(png, media_type="image/png")

    app.include_router(public)
    app.include_router(api)

    web = resources.files("zeprint").joinpath("web")
    app.mount("/", StaticFiles(directory=str(web), html=True), name="web")
    return app


def app_from_env() -> FastAPI:
    """uvicorn factory: ``uvicorn zeprint.api:app_from_env --factory``."""
    return create_app()


__all__ = ["create_app", "app_from_env", "LabelRequest"]
