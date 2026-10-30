"""Piezas compartidas por las rutas web: plantillas, seguridad y utilidades."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlsplit

import markdown
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import db
from .drivers import DRIVERS, Driver, DriverError

BASE = Path(__file__).resolve().parent
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["drivers"] = DRIVERS
templates.env.globals["modules"] = db.MODULES


def _render_markdown(text: str) -> Markup:
    # Sin HTML en bruto: lo que se escriba como <etiqueta> se muestra tal cual
    md = markdown.Markdown(extensions=["fenced_code", "tables"])
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    return Markup(md.convert(text))


templates.env.filters["markdown"] = _render_markdown


def host_allowed(host: str) -> bool:
    return host.rsplit(":", 1)[0] in ALLOWED_HOSTS


def same_origin(origin: str | None, host: str) -> bool:
    """Origin ausente (curl, scripts) se acepta; uno de otra web, no."""
    return origin is None or urlsplit(origin).netloc == host


def driver_or_404(kind: str) -> Driver:
    if kind not in DRIVERS:
        raise HTTPException(404, f"Tipo desconocido: {kind}")
    return DRIVERS[kind]


def list_all():
    """Pide la lista a los tres drivers en paralelo; un fallo no tumba a los demás."""
    def safe(d: Driver):
        try:
            return d.list(), None
        except DriverError as e:
            return [], str(e)

    with ThreadPoolExecutor() as pool:
        results = dict(zip(DRIVERS, pool.map(safe, DRIVERS.values())))
    machines = [m for ms, _ in results.values() for m in ms]
    errors = {kind: err for kind, (_, err) in results.items() if err}
    return machines, errors


def back(kind: str, machine_id: str, tab: str = "info", msg: str = "", error: str = ""):
    url = f"/m/{kind}/{quote(machine_id)}?tab={tab}"
    if msg:
        url += f"&msg={quote(msg)}"
    if error:
        url += f"&error={quote(error)}"
    return RedirectResponse(url, status_code=303)


def safe_next(url: str, default: str = "/") -> str:
    """Solo redirige a rutas de Hangar (evita redirecciones abiertas)."""
    return url if url.startswith("/") and not url.startswith("//") else default


def machine_name(driver: Driver, machine_id: str) -> str:
    try:
        return driver.get(machine_id).name
    except DriverError:
        return machine_id
