"""Hangar: panel web para gestionar máquinas KVM, LXC y Docker del host."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import console, db, jobs, modules, networks
from .common import (ACCESS_TOKEN, BASE, TOKEN_COOKIE, back, driver_or_404, host_allowed,
                     host_stats, list_all, machine_name, safe_next, same_origin, templates,
                     token_ok)
from .drivers import DRIVERS, DriverError
from .drivers.base import SNAPSHOT_NAME_RE

app = FastAPI(title="Hangar")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(console.router)
app.include_router(networks.router)
app.include_router(modules.router)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Hangar controla el host como root, así que:
    - solo responde si se le llama como localhost (evita DNS rebinding), y
    - rechaza POST enviados desde otras webs (evita CSRF: una página
      cualquiera podría mandar un formulario oculto a 127.0.0.1:8090), y
    - abierto como aplicación, exige la clave de su ventana (otros usuarios
      del mismo equipo también llegan a 127.0.0.1).
    Los websockets de la consola hacen las mismas comprobaciones en console.py."""
    host = request.headers.get("host", "")
    if not host_allowed(host):
        return PlainTextResponse("Host no permitido", status_code=400)
    if ACCESS_TOKEN:
        given = request.query_params.get("token")
        if given is not None and token_ok(given):
            # La ventana entra con ?token=…: se guarda en una cookie y se quita de la URL
            response = RedirectResponse(request.url.path, status_code=303)
            response.set_cookie(TOKEN_COOKIE, ACCESS_TOKEN, httponly=True, samesite="strict")
            return response
        if not token_ok(request.cookies.get(TOKEN_COOKIE)):
            return PlainTextResponse("Hangar está abierto como aplicación: úsalo desde su "
                                     "ventana.", status_code=403)
    if request.method not in SAFE_METHODS:
        fetch_site = request.headers.get("sec-fetch-site", "same-origin")
        if not same_origin(request.headers.get("origin"), host) or \
                fetch_site not in ("same-origin", "none"):
            return PlainTextResponse("Petición de otro origen rechazada", status_code=403)
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def html_errors(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(request, "error.html", {
        "status": exc.status_code, "detail": exc.detail,
    }, status_code=exc.status_code)


POWER_MESSAGES = {"start": "Arrancando…", "stop": "Apagando…",
                  "kill": "Apagado forzado.", "restart": "Reiniciando…"}

TABS = {"info": "Características", "network": "Red", "edit": "Modificar",
        "snapshots": "Snapshots", "users": "Usuarios", "notes": "Descripción y notas",
        "config": "Configuración", "delete": "Borrar"}

NO_MODULE = "_none"


@app.get("/")
def index(request: Request, kind: str = "", state: str = "", q: str = "", module: str = "",
          msg: str = "", error: str = ""):
    machines, errors = list_all()
    meta = db.all_meta()
    counts = {k: sum(m.kind == k for m in machines) for k in DRIVERS}
    running = sum(m.state == "running" for m in machines)
    needle = q.lower().strip()

    def meta_of(m):
        return meta.get((m.kind, m.id), db.Meta())

    def matches(m):
        md = meta_of(m)
        haystack = f"{m.name} {md.description} {md.tags} {md.module} {' '.join(m.ips)}".lower()
        return ((not kind or m.kind == kind) and (not state or m.state == state)
                and (not module or md.module == module or (module == NO_MODULE and not md.module))
                and (not needle or needle in haystack))

    rows = [(m, meta_of(m)) for m in machines if matches(m)]
    rows.sort(key=lambda r: (r[0].state != "running", r[0].kind, r[0].name.lower()))

    # Agrupadas por módulo, en el orden del grado y "Sin módulo" al final
    groups = {key: [] for key in [*db.MODULES, ""]}
    for row in rows:
        groups[row[1].module].append(row)
    module_counts = {key: sum(meta_of(m).module == key for m in machines) for key in groups}

    # Capacidad configurada: sirve para avisar antes de encender en masa.
    # El total se compara con la RAM del host y el aviso de cada módulo con la
    # memoria libre ahora mismo. Se calcula sobre todas las máquinas, no solo
    # las visibles con el filtro, porque «Encender todas» actúa sobre el módulo
    # completo.
    host = host_stats()
    alloc_mem = sum(m.memory_mib for m in machines) * 2**20
    alloc_vcpus = sum(m.vcpus for m in machines)
    alloc_pct = round(alloc_mem / host["mem_total"] * 100) if host["mem_total"] else 0
    module_res = {}
    for m in machines:
        key = meta_of(m).module
        res = module_res.setdefault(key, {"count": 0, "mem": 0, "vcpus": 0.0, "need": 0})
        res["count"] += 1
        res["mem"] += m.memory_mib * 2**20
        res["vcpus"] += m.vcpus
        if m.state == "stopped":
            res["need"] += m.memory_mib * 2**20
    for res in module_res.values():
        res["fits"] = not host["mem_available"] or res["need"] <= host["mem_available"]

    return templates.TemplateResponse(request, "index.html", {
        "groups": [(key, items) for key, items in groups.items() if items],
        "total_rows": len(rows), "errors": errors, "counts": counts, "total": len(machines),
        "running": running, "kind": kind, "state": state, "q": q, "module": module,
        "module_counts": module_counts, "no_module": NO_MODULE,
        "jobs": list(jobs.JOBS.values())[-5:][::-1], "msg": msg, "error": error,
        "host": host, "alloc_mem": alloc_mem, "alloc_vcpus": alloc_vcpus,
        "alloc_pct": alloc_pct, "mem_over": bool(host["mem_total"] and alloc_mem > host["mem_total"]),
        "module_res": module_res,
        "here": str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
    })


# --- Crear ----------------------------------------------------------------

def _render_new(request: Request, kind: str, values: dict[str, str] | None = None,
                error: str = "", status_code: int = 200):
    fields = []
    try:
        fields = driver_or_404(kind).create_fields()
    except DriverError as e:
        error = error or str(e)
    if values is not None:  # volver a mostrar lo que escribió el usuario
        for f in fields:
            f.value = values.get(f.key, "") if f.type != "checkbox" else \
                ("1" if f.key in values else "")
    values = values or {}
    return templates.TemplateResponse(request, "new.html", {
        "kind": kind, "fields": fields, "error": error,
        "description": values.get("description", ""),
        "module": values.get("module", request.query_params.get("module", "")),
    }, status_code=status_code)


@app.get("/new/{kind}")
def new_form(request: Request, kind: str):
    return _render_new(request, kind)


@app.post("/new/{kind}")
async def new_submit(request: Request, kind: str):
    driver = driver_or_404(kind)
    values = {k: str(v) for k, v in (await request.form()).items()}
    try:
        spec = driver.prepare_create(values)
    except DriverError as e:
        return _render_new(request, kind, values, str(e), status_code=400)

    description, module = values.get("description", ""), values.get("module", "")

    def work(log):
        machine_id = driver.create(spec, log)
        if description or module:
            db.save_meta(kind, machine_id, description, "", "", module)
        return machine_id

    job = jobs.submit(kind, f"Crear {driver.label} «{spec['name']}»", work)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.get("/jobs/{job_id}")
def job_page(request: Request, job_id: str):
    job = jobs.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Esa tarea no existe (¿se reinició Hangar?)")
    return templates.TemplateResponse(request, "job.html", {"job": job})


# --- Ficha de una máquina -------------------------------------------------

@app.get("/m/{kind}/{machine_id}")
def detail(request: Request, kind: str, machine_id: str, tab: str = "info",
           msg: str = "", error: str = ""):
    driver = driver_or_404(kind)
    try:
        machine = driver.get(machine_id)
    except DriverError as e:
        raise HTTPException(404, str(e)) from e

    ctx = {"users": [], "fields": [], "plan": None, "snaps": [], "ifaces": [],
           "iface_fields": [], "tab_error": None}
    try:
        if tab == "snapshots" and not driver.no_snapshots_reason:
            ctx["snaps"] = driver.snapshots(machine_id)
        elif tab == "users":
            ctx["users"] = sorted(driver.users(machine_id),
                                  key=lambda u: (not u.can_login, u.uid))
        elif tab == "edit":
            ctx["fields"] = driver.editable(machine_id)
        elif tab == "delete":
            ctx["plan"] = driver.delete_plan(machine_id)
        elif tab == "network":
            ctx["ifaces"] = driver.interfaces(machine_id)
            ctx["iface_fields"] = driver.interface_fields(machine_id)
    except DriverError as e:
        ctx["tab_error"] = str(e)

    return templates.TemplateResponse(request, "machine.html", ctx | {
        "m": machine, "meta": db.get_meta(kind, machine_id), "driver": driver,
        "tab": tab if tab in TABS else "info", "tabs": TABS,
        "default_snapshot_name": f"snap-{datetime.now():%Y%m%d-%H%M}",
        "msg": msg, "error": error,
    })


@app.post("/m/{kind}/{machine_id}/meta")
def save_meta(kind: str, machine_id: str, description: str = Form(""),
              notes: str = Form(""), tags: str = Form(""), module: str = Form("")):
    driver_or_404(kind)
    db.save_meta(kind, machine_id, description, notes, tags, module)
    return back(kind, machine_id, "notes", msg="Guardado")


@app.post("/m/{kind}/{machine_id}/module")
def set_module(kind: str, machine_id: str, module: str = Form(""), next: str = Form("/")):
    driver_or_404(kind)
    db.set_module(kind, machine_id, module)
    return RedirectResponse(safe_next(next), status_code=303)


@app.post("/m/{kind}/{machine_id}/edit")
async def edit(request: Request, kind: str, machine_id: str):
    driver = driver_or_404(kind)
    values = {k: str(v) for k, v in (await request.form()).items()}
    try:
        notes = driver.update(machine_id, values)
    except DriverError as e:
        return back(kind, machine_id, "edit", error=str(e))
    return back(kind, machine_id, "edit", msg=" ".join(notes) or "No había cambios.")


# --- Red: interfaces e IPs ------------------------------------------------

@app.post("/m/{kind}/{machine_id}/network/add")
async def iface_add(request: Request, kind: str, machine_id: str):
    driver = driver_or_404(kind)
    values = {k: str(v) for k, v in (await request.form()).items()}
    try:
        notes = driver.add_interface(machine_id, values)
    except DriverError as e:
        return back(kind, machine_id, "network", error=str(e))
    return back(kind, machine_id, "network", msg=" ".join(notes))


@app.post("/m/{kind}/{machine_id}/network/{iface}/remove")
def iface_remove(kind: str, machine_id: str, iface: str):
    driver = driver_or_404(kind)
    try:
        notes = driver.remove_interface(machine_id, iface)
    except DriverError as e:
        return back(kind, machine_id, "network", error=str(e))
    return back(kind, machine_id, "network", msg=" ".join(notes))


@app.post("/m/{kind}/{machine_id}/network/{iface}/ip")
async def iface_ip(request: Request, kind: str, machine_id: str, iface: str):
    driver = driver_or_404(kind)
    values = {k: str(v) for k, v in (await request.form()).items()}

    def work(log):
        driver.set_ip(machine_id, iface, values, log)
        return machine_id

    job = jobs.submit(kind, f"Cambiar IP de {machine_name(driver, machine_id)} ({iface})",
                      work, "network")
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


# --- Snapshots ------------------------------------------------------------

@app.post("/m/{kind}/{machine_id}/snapshots/create")
async def snapshot_create(request: Request, kind: str, machine_id: str):
    driver = driver_or_404(kind)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    description = str(form.get("description", "")).strip()
    if not driver.snapshot_name_auto and not SNAPSHOT_NAME_RE.match(name):
        return back(kind, machine_id, "snapshots", error="Nombre del snapshot: usa letras, "
                    "números, guiones, puntos o guiones bajos (sin espacios).")

    def work(log):
        driver.create_snapshot(machine_id, name, description, log)
        return machine_id

    label = f"«{name}» " if name and not driver.snapshot_name_auto else ""
    job = jobs.submit(kind, f"Snapshot {label}de {machine_name(driver, machine_id)}", work,
                      "snapshots")
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.post("/m/{kind}/{machine_id}/snapshots/{snapshot}/{action}")
def snapshot_action(kind: str, machine_id: str, snapshot: str, action: str):
    driver = driver_or_404(kind)
    if action not in ("revert", "delete"):
        raise HTTPException(404)
    fn = driver.revert_snapshot if action == "revert" else driver.delete_snapshot

    def work(log):
        fn(machine_id, snapshot, log)
        return machine_id

    verb = "Volver a" if action == "revert" else "Borrar snapshot"
    job = jobs.submit(kind, f"{verb} «{snapshot}» de {machine_name(driver, machine_id)}", work,
                      "snapshots")
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


# --- Borrar y encendido ---------------------------------------------------

@app.post("/m/{kind}/{machine_id}/delete")
async def delete(request: Request, kind: str, machine_id: str):
    driver = driver_or_404(kind)
    values = {k: str(v) for k, v in (await request.form()).items()}
    try:
        name = driver.get(machine_id).name
        if values.get("confirm_name", "").strip() != name:
            raise DriverError(f"Para confirmar escribe exactamente «{name}».")
        notes = driver.delete(machine_id, values)
    except DriverError as e:
        return back(kind, machine_id, "delete", error=str(e))
    db.delete_meta(kind, machine_id)
    return RedirectResponse(f"/?msg={quote(' '.join(notes))}", status_code=303)


@app.post("/m/{kind}/{machine_id}/{action}")
def power(request: Request, kind: str, machine_id: str, action: str):
    driver = driver_or_404(kind)
    if action not in POWER_MESSAGES:
        raise HTTPException(404)
    # La portada usa fetch() y espera JSON; la ficha sigue con el redirect de siempre.
    wants_json = "application/json" in request.headers.get("accept", "")
    try:
        getattr(driver, action)(machine_id)
    except DriverError as e:
        if wants_json:
            return JSONResponse({"detail": str(e)}, status_code=400)
        return back(kind, machine_id, error=str(e))
    if wants_json:
        return JSONResponse({"ok": True, "msg": POWER_MESSAGES[action]})
    return back(kind, machine_id, msg=POWER_MESSAGES[action])


# --- API JSON (para scripts o un futuro frontend) -------------------------

@app.get("/api/status")
def api_status():
    """Versión ligera de /api/machines para el refresco automático de la portada."""
    machines, errors = list_all()
    return {
        "machines": [{"kind": m.kind, "id": m.id, "name": m.name, "state": m.state,
                      "ips": m.ips} for m in machines],
        "total": len(machines),
        "running": sum(m.state == "running" for m in machines),
        "counts": {k: sum(m.kind == k for m in machines) for k in DRIVERS},
        "host": host_stats(),
        "errors": errors,
    }


@app.get("/api/machines")
def api_machines():
    machines, errors = list_all()
    meta = db.all_meta()
    return {
        "machines": [asdict(m) | {"meta": asdict(meta.get((m.kind, m.id), db.Meta()))}
                     for m in machines],
        "errors": errors,
    }


@app.get("/api/machines/{kind}/{machine_id}")
def api_machine(kind: str, machine_id: str):
    try:
        m = driver_or_404(kind).get(machine_id)
    except DriverError as e:
        raise HTTPException(404, str(e)) from e
    return asdict(m) | {"meta": asdict(db.get_meta(kind, machine_id))}
