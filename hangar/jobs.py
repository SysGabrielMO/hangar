"""Tareas en segundo plano (crear máquinas puede tardar minutos).

Viven en memoria: si se reinicia Hangar se pierde el historial de tareas,
pero no lo que hayan creado.
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .drivers import DriverError

Log = Callable[[str], None]


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = "running"       # "running" | "done" | "error"
    lines: list[str] = field(default_factory=list)
    result_id: str = ""           # id de la máquina creada
    tab: str = "info"             # pestaña de la máquina a la que volver
    link: str = ""                # o una página concreta (p. ej. un módulo)
    error: str = ""
    started: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))

    def log(self, text: str) -> None:
        for line in text.rstrip().splitlines():
            # Las barras de progreso reescriben la misma línea con \r
            self.lines.append(line.rsplit("\r", 1)[-1])
        del self.lines[:-500]


JOBS: dict[str, Job] = {}


def submit(kind: str, title: str, fn: Callable[[Log], str], tab: str = "info",
           link: str = "") -> Job:
    """Ejecuta fn(log) en un hilo; fn devuelve el id de la máquina afectada."""
    job = Job(id=uuid.uuid4().hex[:10], kind=kind, title=title, tab=tab, link=link)
    JOBS[job.id] = job

    def run():
        try:
            job.result_id = fn(job.log)
            job.status = "done"
        except DriverError as e:
            job.error, job.status = str(e), "error"
        except Exception as e:  # que un fallo inesperado no deje la tarea colgada
            job.error, job.status = f"Error inesperado: {e!r}", "error"

    threading.Thread(target=run, daemon=True).start()
    return job
