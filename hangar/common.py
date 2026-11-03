"""Piezas compartidas por las rutas web: plantillas, seguridad y utilidades."""
from __future__ import annotations

import os
import secrets
import shutil
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
from .drivers.base import human_bytes

BASE = Path(__file__).resolve().parent
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["drivers"] = DRIVERS
templates.env.globals["modules"] = db.MODULES
templates.env.filters["human"] = human_bytes


def host_stats() -> dict[str, float | int | str]:
    """Estado del host para la portada: memoria, carga y disco.

    Se lee de /proc para no añadir dependencias (psutil). Si algo no se puede
    leer, se devuelve 0 y la portada simplemente no lo enseña.
    """
    mem: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                key, _, value = line.partition(":")
                mem[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass

    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", mem.get("MemFree", 0))
    used = max(total - available, 0)
    cpus = os.cpu_count() or 1
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    # /var/lib es donde viven las imágenes de libvirt, los rootfs de LXC y
    # Docker; si no existe, el sistema de archivos raíz.
    disk_path = Path("/var/lib") if Path("/var/lib").exists() else Path("/")
    try:
        disk = shutil.disk_usage(disk_path)
        disk_total, disk_used = disk.total, disk.used
    except OSError:
        disk_total = disk_used = 0

    return {
        "mem_total": total, "mem_available": available, "mem_used": used,
        "mem_pct": round(used / total * 100) if total else 0,
        "cpus": cpus, "load": load, "load_pct": min(round(load / cpus * 100), 100),
        "disk_total": disk_total, "disk_used": disk_used,
        "disk_pct": round(disk_used / disk_total * 100) if disk_total else 0,
        "disk_path": str(disk_path),
    }


def _render_markdown(text: str) -> Markup:
    # Sin HTML en bruto: lo que se escriba como <etiqueta> se muestra tal cual
    md = markdown.Markdown(extensions=["fenced_code", "tables"])
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    return Markup(md.convert(text))


templates.env.filters["markdown"] = _render_markdown


# Clave de acceso: la pone el lanzador de escritorio (./hangar-app) para que solo
# su ventana pueda usar Hangar. Sin ella (./run.sh) no se pide.
ACCESS_TOKEN = os.environ.get("HANGAR_TOKEN", "")
TOKEN_COOKIE = "hangar_token"


def token_ok(value: str | None) -> bool:
    return not ACCESS_TOKEN or secrets.compare_digest(value or "", ACCESS_TOKEN)


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
