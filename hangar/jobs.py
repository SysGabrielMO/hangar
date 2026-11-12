"""Tareas en segundo plano (crear máquinas puede tardar minutos).

Viven en memoria: si se reinicia Hangar se pierde el historial de tareas,
pero no lo que hayan creado.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .common import invalidate_list
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
    finished_at: float = 0.0      # time.time() al terminar; 0 mientras sigue en curso

    def log(self, text: str) -> None:
        for line in text.rstrip().splitlines():
            # Las barras de progreso reescriben la misma línea con \r
            self.lines.append(line.rsplit("\r", 1)[-1])
        del self.lines[:-500]


JOBS: dict[str, Job] = {}
MAX_JOBS = 50                     # las más antiguas ya terminadas se olvidan
RECENT_SECONDS = 15 * 60


def recent(limit: int = 5) -> list[Job]:
    """Tareas en curso o recién terminadas, de la más nueva a la más antigua
    (para la portada: una tarea de ayer ya no pinta nada ahí)."""
    now = time.time()
    jobs = [job for job in JOBS.values()
            if job.status == "running" or now - job.finished_at < RECENT_SECONDS]
    return jobs[-limit:][::-1]


def submit(kind: str, title: str, fn: Callable[[Log], str], tab: str = "info",
           link: str = "") -> Job:
    """Ejecuta fn(log) en un hilo; fn devuelve el id de la máquina afectada."""
    job = Job(id=uuid.uuid4().hex[:10], kind=kind, title=title, tab=tab, link=link)
    JOBS[job.id] = job
    for old_id in [k for k, j in JOBS.items() if j.status != "running"][:-MAX_JOBS]:
        del JOBS[old_id]

    def run():
        try:
            job.result_id = fn(job.log) or ""
            job.status = "done"
        except DriverError as e:
            job.error, job.status = str(e), "error"
        except Exception as e:  # que un fallo inesperado no deje la tarea colgada
            job.error, job.status = f"Error inesperado: {e!r}", "error"
        finally:
            job.finished_at = time.time()
            invalidate_list()         # la tarea ha cambiado algo: que se vuelva a listar

    threading.Thread(target=run, daemon=True).start()
    return job
