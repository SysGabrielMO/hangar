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
from concurrent.futures import ThreadPoolExecutor

from .drivers import DRIVERS, DriverError

HISTORY = 80                 # puntos; a 3 s son unos 4 minutos
CACHE_SECONDS = 1.2
ALL_CACHE_SECONDS = 2.5        # la portada refresca cada 6 s; varias pestañas comparten lectura

_lock = threading.Lock()
_prev: dict[tuple[str, str], tuple[float, float]] = {}
_history: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=HISTORY))
_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def _record(key: tuple[str, str], usage, now: float) -> dict:
    """Convierte una lectura en porcentajes y la apunta en el histórico. Con _lock cogido."""
    if usage.state != "running":
        _prev.pop(key, None)
        _history.pop(key, None)
        data = {"state": usage.state, "cpu_pct": 0.0, "mem_pct": None,
                "mem_used": 0, "mem_limit": usage.mem_limit, "history": []}
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
    with _lock:
        return _record(key, usage, time.monotonic())


_all_cache: tuple[float, dict] = (0.0, {})
_all_lock = threading.Lock()


def sample_all(kinds) -> dict[str, dict]:
    """Consumo de todas las máquinas encendidas de esos tipos, para la portada:
    {"tipo/id": {"cpu": % del host o None, "mem": bytes, "limit": bytes}}.
    Una consulta por tecnología, en paralelo, y guardada un par de segundos."""
    global _all_cache
    kinds = sorted(k for k in kinds if k in DRIVERS)
    with _all_lock:      # si llegan dos peticiones a la vez, la segunda espera a la primera
        at, data = _all_cache
        if time.monotonic() - at < ALL_CACHE_SECONDS:
            return data
        if not kinds:
            return {}

        def read(kind):
            try:
                return kind, DRIVERS[kind].usage_all()
            except Exception:           # noqa: BLE001 - sin consumo de ese tipo, pero sin romper
                return kind, {}

        with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
            readings = list(pool.map(read, kinds))
        now = time.monotonic()
        result = {}
        with _lock:
            for kind, usages in readings:
                for machine_id, usage in usages.items():
                    point = _record((kind, machine_id), usage, now)
                    result[f"{kind}/{machine_id}"] = {
                        "cpu": point["cpu_pct"], "mem": point["mem_used"],
                        "limit": point["mem_limit"]}
        _all_cache = (now, result)
        return result


def forget(kind: str, machine_id: str) -> None:
    """Olvida lo guardado de una máquina (al borrarla)."""
    key = (kind, machine_id)
    with _lock:
        _prev.pop(key, None)
        _history.pop(key, None)
        _cache.pop(key, None)
