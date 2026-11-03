"""Acciones sobre un módulo entero: preparar/recoger el laboratorio y exportar
su documentación en Markdown (para la memoria de la práctica)."""
from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse

from . import db, jobs
from .common import host_stats, list_all
from .drivers import DRIVERS, DriverError
from .drivers.base import human_bytes

router = APIRouter()

# Cuánto se espera a que una máquina arrancada tenga IP. Docker la tiene casi
# al instante; una VM con DHCP puede tardar bastante más.
READY_TIMEOUT = {"kvm": 75, "lxc": 45, "docker": 15}
STOP_TIMEOUT = 45
POLL = 2


def _machines_of(module: str):
    if module not in db.MODULES:
        raise HTTPException(404, f"Módulo desconocido: {module}")
    machines, _ = list_all()
    meta = db.all_meta()
    return [(m, meta[(m.kind, m.id)]) for m in sorted(machines, key=lambda m: (m.kind, m.name))
            if (m.kind, m.id) in meta and meta[(m.kind, m.id)].module == module]


def _wait_ready(driver, machine, log) -> bool:
    """Espera a que la máquina esté encendida y, si es posible, con IP."""
    timeout = READY_TIMEOUT.get(driver.kind, 30)
    deadline = time.monotonic() + timeout
    last_state = ""
    while time.monotonic() < deadline:
        try:
            current = driver.get(machine.id)
        except DriverError as e:
            log(f"  {machine.name}: {e}")
            time.sleep(POLL)
            continue
        if current.state == "running" and current.ips:
            log(f"  ✓ {machine.name} lista · {', '.join(current.ips)}")
            return True
        if current.state == "running" and driver.kind == "docker":
            # Un contenedor con --network none no tendrá IP nunca.
            log(f"  ✓ {machine.name} arrancado (sin IP de red)")
            return True
        if current.state != last_state:
            log(f"  {machine.name}: {'arrancando' if current.state == 'running' else current.state}…")
            last_state = current.state
        time.sleep(POLL)
    log(f"  ⚠ {machine.name} sigue sin IP tras {timeout} s (¿no tiene DHCP?)")
    return False


def _wait_stopped(driver, machine, log) -> bool:
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if driver.get(machine.id).state == "stopped":
                return True
        except DriverError:
            pass
        time.sleep(POLL)
    log(f"  ⚠ {machine.name} sigue apagándose tras {STOP_TIMEOUT} s")
    return False


def _prepare(module: str, targets, log) -> str:
    todo = [m for m, _ in targets if m.state in ("stopped", "paused")]
    if not todo:
        log("Ya estaba todo encendido: no hay nada que preparar.")
        return ""

    need = sum(m.memory_mib for m in todo if m.state == "stopped") * 2**20
    host = host_stats()
    if need and host["mem_available"] and need > host["mem_available"]:
        log(f"⚠ Las máquinas apagadas piden {human_bytes(need)} y solo hay "
            f"{human_bytes(host['mem_available'])} libres. Se arrancan de una en una.")

    log(f"Preparando {len(todo)} máquina(s) de {module}…")
    ready = failed = 0
    for index, machine in enumerate(todo, 1):
        driver = DRIVERS[machine.kind]
        log(f"[{index}/{len(todo)}] {machine.name} ({driver.label})")
        try:
            driver.start(machine.id)
        except DriverError as e:
            failed += 1
            log(f"  ✗ {e}")
            continue
        if _wait_ready(driver, machine, log):
            ready += 1

    missing = len(todo) - ready - failed
    log(f"Resumen: {ready} lista(s), {missing} sin IP, {failed} con error.")
    if failed:
        raise DriverError(f"{failed} de {len(todo)} máquinas no arrancaron; mira el registro.")
    return ""


def _collect(module: str, targets, log) -> str:
    todo = [m for m, _ in targets if m.state in ("running", "paused")]
    if not todo:
        log("No había nada encendido: el laboratorio ya estaba recogido.")
        return ""

    log(f"Recogiendo {len(todo)} máquina(s) de {module}…")
    failed = stopped = 0
    for index, machine in enumerate(todo, 1):
        driver = DRIVERS[machine.kind]
        log(f"[{index}/{len(todo)}] {machine.name} ({driver.label})")
        try:
            driver.stop(machine.id)
        except DriverError as e:
            failed += 1
            log(f"  ✗ {e}")
            continue
        if _wait_stopped(driver, machine, log):
            stopped += 1
            log(f"  ✓ {machine.name} apagada")

    log(f"Resumen: {stopped} apagada(s), {failed} con error.")
    if failed:
        raise DriverError(f"{failed} de {len(todo)} máquinas no se apagaron; mira el registro.")
    return ""


@router.post("/modules/{module}/{action}")
def power_module(module: str, action: str):
    if action not in ("start", "stop"):
        raise HTTPException(404)
    targets = _machines_of(module)

    if action == "start":
        title = f"Preparar clase · {module}"
    else:
        title = f"Recoger · {module}"

    def work(log):
        if action == "start":
            return _prepare(module, targets, log)
        return _collect(module, targets, log)

    job = jobs.submit("", title, work, link=f"/?module={quote(module)}")
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
        saved_ips = db.get_iface_ips(m.kind, m.id)
        for iface in ifaces:
            iface.defined_ip = saved_ips.get(iface.key) or iface.defined_ip
        if ifaces:
            details += ["**Red**", ""]
            for iface in ifaces:
                ips = iface.ips or ([iface.defined_ip] if iface.defined_ip else [])
                details.append(f"- {iface.network}"
                               + (f" · MAC `{iface.mac}`" if iface.mac else "")
                               + (f" · IP {', '.join(ips)}" if ips else "")
                               + f" · {iface.ip_config}")
            details.append("")
        if meta.notes:
            details += ["**Notas**", "", meta.notes, ""]
    if not targets:
        lines.append("| — | | | | | |")
    body = "\n".join(lines + details) + "\n"
    filename = f"hangar-{module.lower().replace(' ', '-')}.md"
    return PlainTextResponse(body, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"'})
