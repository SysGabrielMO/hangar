"""Piezas compartidas por las rutas web: plantillas, seguridad y utilidades."""
from __future__ import annotations

import os
import secrets
import shutil
import threading
import time
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
# Posición de cada módulo (1…n): decide su color en la interfaz
templates.env.globals["module_index"] = {key: i for i, key in enumerate(db.MODULES, 1)}
templates.env.filters["human"] = human_bytes
NO_MODULE = "_none"              # valor de ?module= para «Sin módulo»
templates.env.globals["no_module"] = NO_MODULE


def static_url(path: str) -> str:
    """URL de un fichero estático con su fecha de modificación como versión:
    tras actualizar Hangar, el navegador no sigue usando el CSS o JS antiguo."""
    try:
        version = int((BASE / "static" / path).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{path}?v={version}"


def icon(name: str, cls: str = "") -> Markup:
    """Icono del sprite static/icons.svg."""
    return Markup(f'<svg class="icon{" " + cls if cls else ""}" aria-hidden="true">'
                  f'<use href="{static_url("icons.svg")}#{name}"/></svg>')


templates.env.globals["static"] = static_url
templates.env.globals["icon"] = icon


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
templates.env.globals["host_stats"] = host_stats   # la barra lateral lo enseña en todas las páginas


# Clave de acceso: la pone el lanzador de escritorio (./hangar-app) para que solo
# su ventana pueda usar Hangar. Sin ella (./run.sh) no se pide.
ACCESS_TOKEN = os.environ.get("HANGAR_TOKEN", "")
TOKEN_COOKIE = "hangar_token"


def token_ok(value: str | None) -> bool:
    return not ACCESS_TOKEN or secrets.compare_digest((value or "").encode(),
                                                      ACCESS_TOKEN.encode())


def host_allowed(host: str) -> bool:
    return host.rsplit(":", 1)[0] in ALLOWED_HOSTS


def same_origin(origin: str | None, host: str) -> bool:
    """Origin ausente (curl, scripts) se acepta; uno de otra web, no."""
    return origin is None or urlsplit(origin).netloc == host


def driver_or_404(kind: str) -> Driver:
    if kind not in DRIVERS:
        raise HTTPException(404, f"Tipo desconocido: {kind}")
    return DRIVERS[kind]


def _list_all_now():
    """Pide la lista a los tres drivers en paralelo; un fallo no tumba a los demás."""
    def safe(d: Driver):
        try:
            return d.list(), None
        except DriverError as e:
            return [], str(e)
        except Exception as e:      # un fallo inesperado de un driver no rompe la portada
            return [], f"Error inesperado: {e!r}"

    with ThreadPoolExecutor() as pool:
        results = dict(zip(DRIVERS, pool.map(safe, DRIVERS.values())))
    machines = [m for ms, _ in results.values() for m in ms]
    errors = {kind: err for kind, (_, err) in results.items() if err}

    # Una IP estática dentro de una VM sin agente no la conoce el hipervisor.
    # Mostramos la última IP vista y, si no hay, la que se haya anotado en Red.
    last_ips = db.all_last_ips()
    configured = db.all_iface_ips()
    for machine in machines:
        key = (machine.kind, machine.id)
        if machine.ips:
            if last_ips.get(key) != machine.ips:
                db.save_last_ips(machine.kind, machine.id, machine.ips)
        else:
            remembered = configured.get(key) or last_ips.get(key)
            if remembered:
                machine.ips = remembered
    return machines, errors


# --- Caché de la lista de máquinas ------------------------------------------
# Listar cuesta (LXC pasa por sudo) y lo piden la portada, su refresco, las
# consolas, las redes y cada ficha abierta. Se guarda un par de segundos y se
# tira en cuanto Hangar cambia algo (cualquier POST o una tarea que termina).
LIST_TTL = 2.0          # segundos que la lista se da por buena
LIST_STALE = 12.0       # hasta aquí se sirve la anterior mientras se refresca por detrás
_list_state_lock = threading.Lock()
_list_refresh_lock = threading.Lock()
_list_cache: dict = {"at": 0.0, "data": None, "generation": 0, "stale_ok": False}


def invalidate_list() -> None:
    """Olvida la lista guardada: la próxima petición vuelve a preguntar."""
    with _list_state_lock:
        _list_cache["generation"] += 1
        _list_cache["at"] = 0.0
        _list_cache["stale_ok"] = False


def _cached_list(max_age: float, stale: bool = False):
    with _list_state_lock:
        data = _list_cache["data"]
        if data is None or (stale and not _list_cache["stale_ok"]):
            return None
        if time.monotonic() - _list_cache["at"] >= max_age:
            return None
        return list(data[0]), dict(data[1])


def _refresh_list():
    """Vuelve a listar y guarda el resultado. Con _list_refresh_lock cogido."""
    with _list_state_lock:
        generation = _list_cache["generation"]
    data = _list_all_now()
    with _list_state_lock:
        # Si algo cambió mientras se listaba, esto ya puede ser viejo: no se guarda
        if generation == _list_cache["generation"]:
            _list_cache.update(at=time.monotonic(), data=data, stale_ok=True)
    return data


def _refresh_list_in_background() -> None:
    if not _list_refresh_lock.acquire(blocking=False):
        return                               # ya hay un refresco en marcha

    def work():
        try:
            _refresh_list()
        except Exception:                    # noqa: BLE001 - el siguiente intento lo reintenta
            pass
        finally:
            _list_refresh_lock.release()

    threading.Thread(target=work, daemon=True).start()


def list_all():
    """Todas las máquinas y los errores por driver: (máquinas, {tipo: error}).

    Si la lista guardada tiene menos de LIST_TTL se devuelve tal cual. Si es algo
    más vieja (y Hangar no ha cambiado nada desde entonces) se devuelve también,
    pero se refresca por detrás: la página sale al instante y el siguiente
    refresco de la interfaz ya trae lo último."""
    cached = _cached_list(LIST_TTL)
    if cached:
        return cached
    cached = _cached_list(LIST_STALE, stale=True)
    if cached:
        _refresh_list_in_background()
        return cached
    with _list_refresh_lock:
        cached = _cached_list(LIST_TTL)      # la refrescó el hilo al que esperábamos
        if cached:
            return cached
        data = _refresh_list()
    return list(data[0]), dict(data[1])


def find_machine(kind: str, machine_id: str):
    """La máquina en la lista (con caché), o None. Barato: para sondeos frecuentes."""
    machines, _ = list_all()
    return next((m for m in machines if m.kind == kind and m.id == machine_id), None)


# Avisos de acciones que terminaron mal en segundo plano (p. ej. un apagado
# que tardó y al final falló). El siguiente sondeo de la interfaz los recoge.
_notices: list[str] = []
_notices_lock = threading.Lock()


def push_notice(text: str) -> None:
    with _notices_lock:
        _notices.append(text)
        del _notices[:-20]


def pop_notices() -> list[str]:
    with _notices_lock:
        notices = list(_notices)
        _notices.clear()
    return notices


# Modificar, Configuración y Borrar son secciones de la pestaña «Ajustes»
SETTINGS_SECTIONS = ("edit", "config", "delete")


def back(kind: str, machine_id: str, tab: str = "info", msg: str = "", error: str = ""):
    anchor = ""
    if tab in SETTINGS_SECTIONS:
        # sec= dice en qué sección enseñar el mensaje; el ancla lleva hasta ella
        tab, anchor = f"settings&sec={tab}", f"#{tab}"
    url = f"/m/{kind}/{quote(machine_id)}?tab={tab}"
    if msg:
        url += f"&msg={quote(msg)}"
    if error:
        url += f"&error={quote(error)}"
    return RedirectResponse(url + anchor, status_code=303)


def safe_next(url: str, default: str = "/") -> str:
    """Solo redirige a rutas de Hangar (evita redirecciones abiertas)."""
    if not url.startswith("/") or url.startswith("//") or "\\" in url \
            or any(ord(c) < 32 for c in url):
        return default
    return url


def machine_name(driver: Driver, machine_id: str) -> str:
    try:
        return driver.get(machine_id).name
    except DriverError:
        return machine_id
