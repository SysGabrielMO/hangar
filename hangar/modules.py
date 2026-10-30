"""Acciones sobre un módulo entero: encender/apagar todo y exportar su
documentación en Markdown (para la memoria de la práctica)."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse

from . import db, jobs
from .common import list_all
from .drivers import DRIVERS, DriverError

router = APIRouter()


def _machines_of(module: str):
    if module not in db.MODULES:
        raise HTTPException(404, f"Módulo desconocido: {module}")
    machines, _ = list_all()
    meta = db.all_meta()
    return [(m, meta[(m.kind, m.id)]) for m in sorted(machines, key=lambda m: (m.kind, m.name))
            if (m.kind, m.id) in meta and meta[(m.kind, m.id)].module == module]


@router.post("/modules/{module}/{action}")
def power_module(module: str, action: str):
    if action not in ("start", "stop"):
        raise HTTPException(404)
    targets = _machines_of(module)
    wanted = "stopped" if action == "start" else "running"

    def work(log):
        todo = [m for m, _ in targets if m.state == wanted or
                (action == "start" and m.state == "paused")]
        if not todo:
            log("No había nada que " + ("encender." if action == "start" else "apagar."))
        failed = 0
        for m in todo:
            try:
                getattr(DRIVERS[m.kind], action)(m.id)
                log(f"✓ {m.name} ({DRIVERS[m.kind].label})")
            except DriverError as e:
                failed += 1
                log(f"✗ {m.name}: {e}")
        if failed:
            raise DriverError(f"{failed} de {len(todo)} máquinas fallaron; mira el registro.")
        return ""

    verb = "Encender" if action == "start" else "Apagar"
    job = jobs.submit("", f"{verb} todas las máquinas de {module}", work,
                      link=f"/?module={quote(module)}")
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.get("/modules/{module}/export.md")
def export_module(module: str):
    targets = _machines_of(module)
    lines = [f"# {module} · {db.MODULES[module]}", "",
             f"Laboratorio exportado desde Hangar el {datetime.now():%d/%m/%Y %H:%M}.", "",
             "| Máquina | Tipo | Estado | CPU | Memoria | IPs |",
             "|---|---|---|---|---|---|"]
    state_es = {"running": "encendida", "stopped": "apagada", "paused": "pausada"}
    details = []
    for m, meta in targets:
        driver = DRIVERS[m.kind]
        lines.append(f"| {m.name} | {driver.label} | {state_es.get(m.state, m.state)} | "
                     f"{m.cpus} | {m.memory} | {', '.join(m.ips) or '—'} |")
        details += ["", f"## {m.name}", ""]
        if meta.description:
            details += [meta.description, ""]
        if meta.tag_list:
            details += [f"Etiquetas: {', '.join(meta.tag_list)}", ""]
        try:
            ifaces = driver.interfaces(m.id)
        except DriverError:
            ifaces = []
        if ifaces:
            details += ["**Red**", ""]
            details += [f"- {i.network}"
                        + (f" · MAC `{i.mac}`" if i.mac else "")
                        + (f" · IP {', '.join(i.ips)}" if i.ips else "")
                        + f" · {i.ip_config}" for i in ifaces]
            details.append("")
        if meta.notes:
            details += ["**Notas**", "", meta.notes, ""]
    if not targets:
        lines.append("| — | | | | | |")
    body = "\n".join(lines + details) + "\n"
    filename = f"hangar-{module.lower().replace(' ', '-')}.md"
    return PlainTextResponse(body, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"'})
