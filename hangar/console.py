"""Consola web: una terminal (xterm.js) conectada por websocket a un
pseudo-terminal del host donde corre virsh console / lxc-attach / docker exec.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import struct
import subprocess
import termios

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse
from starlette.websockets import WebSocketDisconnect

from . import jobs
from .common import (TOKEN_COOKIE, back, driver_or_404, host_allowed, list_all, machine_name,
                     templates, token_ok)
from .drivers import DRIVERS, DriverError

router = APIRouter()


@router.get("/consoles")
def consoles_page(request: Request):
    """Espacio de trabajo con varias consolas en pestañas."""
    machines, errors = list_all()
    catalog = [
        {"kind": m.kind, "id": m.id, "name": m.name, "label": DRIVERS[m.kind].label}
        for m in sorted(machines, key=lambda m: (m.kind, m.name.lower()))
        if m.state == "running"
    ]
    return templates.TemplateResponse(request, "consoles.html", {
        "catalog": catalog, "errors": errors,
    })


@router.get("/m/{kind}/{machine_id}/console")
def console_page(request: Request, kind: str, machine_id: str):
    driver = driver_or_404(kind)
    try:
        machine = driver.get(machine_id)
    except DriverError as e:
        raise HTTPException(404, str(e)) from e
    return templates.TemplateResponse(request, "console.html", {
        "m": machine, "driver": driver, "hint": driver.console_hint,
    })


@router.post("/m/{kind}/{machine_id}/serial")
def enable_serial(kind: str, machine_id: str):
    driver = driver_or_404(kind)

    def work(log):
        driver.enable_serial_console(machine_id, log)
        return machine_id

    job = jobs.submit(kind, f"Activar consola serie de {machine_name(driver, machine_id)}",
                      work, link=f"/m/{kind}/{quote(machine_id)}/console")
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/m/{kind}/{machine_id}/display")
def open_display(kind: str, machine_id: str):
    driver = driver_or_404(kind)
    try:
        driver.open_display(machine_id)
    except DriverError as e:
        return back(kind, machine_id, error=str(e))
    return back(kind, machine_id, msg="Abriendo la pantalla en una ventana de tu escritorio…")


def _spawn(cmd: list[str]) -> tuple[subprocess.Popen, int]:
    """Lanza cmd con un pseudo-terminal como terminal de control.
    `setsid -c` crea una sesión nueva y le asigna el pty (stdin) como
    terminal de control, así bash/sh tienen control de trabajos y Ctrl-C."""
    master, slave = os.openpty()
    env = {**os.environ, "TERM": "xterm-256color"}
    try:
        proc = subprocess.Popen(["setsid", "-c", *cmd], stdin=slave, stdout=slave,
                                stderr=slave, env=env, close_fds=True)
    finally:
        os.close(slave)
    return proc, master


def _stop(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=1)


@router.websocket("/m/{kind}/{machine_id}/console/ws")
async def console_ws(ws: WebSocket, kind: str, machine_id: str):
    # Un websocket no pasa por el middleware HTTP: misma comprobación aquí.
    # Los navegadores siempre envían Origin en websockets, así que se exige.
    host, origin = ws.headers.get("host", ""), ws.headers.get("origin", "")
    if not host_allowed(host) or origin.split("://", 1)[-1] != host or \
            not token_ok(ws.cookies.get(TOKEN_COOKIE)):
        await ws.close(code=1008)
        return
    await ws.accept()

    driver = DRIVERS.get(kind)
    try:
        if driver is None:
            raise DriverError(f"Tipo desconocido: {kind}")
        cmd = driver.console_command(machine_id)
    except DriverError as e:
        await ws.send_text(f"\r\n\x1b[31m{e}\x1b[0m\r\n")
        await ws.close()
        return

    proc, master = _spawn(cmd)
    loop = asyncio.get_running_loop()
    output: asyncio.Queue[bytes] = asyncio.Queue()

    def readable():
        try:
            data = os.read(master, 65536)
        except OSError:          # el proceso terminó y se cerró el pty
            data = b""
        if not data:
            loop.remove_reader(master)
        output.put_nowait(data)

    loop.add_reader(master, readable)

    async def pump_output():
        while data := await output.get():
            await ws.send_bytes(data)
        with contextlib.suppress(RuntimeError):
            await ws.send_text("\r\n\x1b[2m[sesión terminada]\x1b[0m\r\n")
            await ws.close()

    pump = asyncio.create_task(pump_output())
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "input":
                os.write(master, msg["data"].encode())
            elif msg.get("type") == "resize":
                rows, cols = int(msg["rows"]), int(msg["cols"])
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (WebSocketDisconnect, RuntimeError, OSError, ValueError, KeyError):
        pass
    finally:
        loop.remove_reader(master)
        pump.cancel()
        await asyncio.to_thread(_stop, proc)
        os.close(master)
