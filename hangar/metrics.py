"""Consumo en vivo de las máquinas para la pestaña Rendimiento.

No hay un hilo muestreando en segundo plano: se mide cuando la pestaña está
abierta y se guarda un histórico pequeño en memoria (se pierde al reiniciar
Hangar). Así no se castiga al host con consultas a los tres runtimes cuando
nadie está mirando.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

from .drivers import DRIVERS, DriverError

HISTORY = 80                 # puntos; a 3 s son unos 4 minutos
CACHE_SECONDS = 1.2

_lock = threading.Lock()
_prev: dict[tuple[str, str], tuple[float, float]] = {}
_history: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=HISTORY))
_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def sample(kind: str, machine_id: str) -> dict:
    """Devuelve el consumo actual y el histórico reciente de una máquina."""
    driver = DRIVERS.get(kind)
    if driver is None:
        raise DriverError(f"Tipo desconocido: {kind}")

    key = (kind, machine_id)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]

    usage = driver.usage(machine_id)
    now = time.monotonic()
    with _lock:
        if usage.state != "running":
            _prev.pop(key, None)
            _history.pop(key, None)
            data = {
                "state": usage.state, "cpu_pct": 0.0, "mem_pct": None,
                "mem_used": 0, "mem_limit": usage.mem_limit, "history": [],
            }
        else:
            cpu_pct = None
            previous = _prev.get(key)
            if previous:
                cpu_pct = 0.0
                elapsed = now - previous[0]
                delta = usage.cpu_time - previous[1]
                if elapsed > 0 and delta > 0:
                    cpu_pct = min(delta / elapsed / (os.cpu_count() or 1) * 100, 100)
            _prev[key] = (now, usage.cpu_time)

            mem_pct = (min(usage.mem_used / usage.mem_limit * 100, 100)
                       if usage.mem_limit else None)
            points = _history[key]
            points.append({
                "cpu": round(cpu_pct, 1) if cpu_pct is not None else None,
                "mem": round(mem_pct, 1) if mem_pct is not None else None,
                "used": usage.mem_used,
            })
            data = {
                "state": usage.state,
                "cpu_pct": round(cpu_pct, 1) if cpu_pct is not None else None,
                "mem_pct": round(mem_pct, 1) if mem_pct is not None else None,
                "mem_used": usage.mem_used, "mem_limit": usage.mem_limit,
                "history": list(points),
            }

        _cache[key] = (now, data)
        return data
