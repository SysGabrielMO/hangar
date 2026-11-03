"""Metadatos propios de Hangar (módulo, descripción, notas, etiquetas) en SQLite.

libvirt, LXC y Docker no guardan esta información, así que la enlazamos
con cada máquina por (tipo, id).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "hangar.db"

# Módulos del grado; el orden es el de la portada
MODULES = {
    "ASO": "Administración de Sistemas Operativos",
    "BBDD": "Bases de Datos",
    "IMPWEB": "Implantación de Aplicaciones Web",
    "SERVICIOS": "Servicios de Red e Internet",
    "INFRAESTRUCTURA": "Infraestructura y redes",
    "PROYECTO INTEGRADO": "Proyecto Integrado",
    "SEGURIDAD": "Seguridad y Alta Disponibilidad",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    kind        TEXT NOT NULL,
    id          TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);

CREATE TABLE IF NOT EXISTS iface_ip (
    kind       TEXT NOT NULL,
    id         TEXT NOT NULL,
    iface      TEXT NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, id, iface)
);

CREATE TABLE IF NOT EXISTS last_ip (
    kind       TEXT NOT NULL,
    id         TEXT NOT NULL,
    ips        TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
"""
# Columnas añadidas después; se crean solas en bases de datos antiguas
MIGRATIONS = {"module": "ALTER TABLE meta ADD COLUMN module TEXT NOT NULL DEFAULT ''"}


@dataclass
class Meta:
    description: str = ""
    notes: str = ""          # Markdown: cómo está montada, credenciales de lab, etc.
    tags: str = ""           # separadas por comas
    updated_at: str = ""
    module: str = ""         # clave de MODULES o vacío

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(",") if t.strip()]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(meta)")}
    for column, sql in MIGRATIONS.items():
        if column not in columns:
            conn.execute(sql)
    return conn


def _meta(r: sqlite3.Row) -> Meta:
    return Meta(r["description"], r["notes"], r["tags"], r["updated_at"], r["module"])


def all_meta() -> dict[tuple[str, str], Meta]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM meta").fetchall()
    return {(r["kind"], r["id"]): _meta(r) for r in rows}


def get_meta(kind: str, machine_id: str) -> Meta:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM meta WHERE kind = ? AND id = ?",
                         (kind, machine_id)).fetchone()
    return _meta(r) if r else Meta()


def delete_meta(kind: str, machine_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM meta WHERE kind = ? AND id = ?", (kind, machine_id))


def get_iface_ips(kind: str, machine_id: str) -> dict[str, str]:
    """IPs configuradas a mano o por Hangar, indexadas por MAC/índice."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT iface, ip FROM iface_ip WHERE kind = ? AND id = ?",
            (kind, machine_id)).fetchall()
    return {r["iface"]: r["ip"] for r in rows if r["ip"]}


def save_iface_ip(kind: str, machine_id: str, iface: str, ip: str) -> None:
    ip = ip.strip()
    with _connect() as conn:
        if ip:
            conn.execute(
                """INSERT INTO iface_ip (kind, id, iface, ip, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (kind, id, iface) DO UPDATE SET
                     ip = excluded.ip, updated_at = excluded.updated_at""",
                (kind, machine_id, iface, ip,
                 datetime.now().isoformat(timespec="seconds")),
            )
        else:
            conn.execute("DELETE FROM iface_ip WHERE kind = ? AND id = ? AND iface = ?",
                         (kind, machine_id, iface))


def all_iface_ips() -> dict[tuple[str, str], list[str]]:
    """IPs anotadas o configuradas por máquina (para la portada y la API)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT kind, id, ip FROM iface_ip WHERE ip != '' ORDER BY updated_at DESC"
        ).fetchall()
    result: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        result.setdefault((row["kind"], row["id"]), []).append(row["ip"])
    return result


def all_last_ips() -> dict[tuple[str, str], list[str]]:
    """Última IP vista por Hangar para cada máquina."""
    with _connect() as conn:
        rows = conn.execute("SELECT kind, id, ips FROM last_ip").fetchall()
    result = {}
    for row in rows:
        try:
            ips = json.loads(row["ips"])
        except ValueError:
            continue
        if isinstance(ips, list) and ips:
            result[(row["kind"], row["id"])] = [str(ip) for ip in ips]
    return result


def save_last_ips(kind: str, machine_id: str, ips: list[str]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        if not ips:
            conn.execute("DELETE FROM last_ip WHERE kind = ? AND id = ?", (kind, machine_id))
            return
        conn.execute(
            """INSERT INTO last_ip (kind, id, ips, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (kind, id) DO UPDATE SET
                 ips = excluded.ips, updated_at = excluded.updated_at""",
            (kind, machine_id, json.dumps(list(ips)), now),
        )


def delete_iface_ips(kind: str, machine_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM iface_ip WHERE kind = ? AND id = ?", (kind, machine_id))
        conn.execute("DELETE FROM last_ip WHERE kind = ? AND id = ?", (kind, machine_id))


def save_meta(kind: str, machine_id: str, description: str, notes: str, tags: str,
              module: str = "") -> None:
    now = datetime.now().isoformat(timespec="seconds")
    module = module if module in MODULES else ""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO meta (kind, id, description, notes, tags, module, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (kind, id) DO UPDATE SET
                 description = excluded.description, notes = excluded.notes,
                 tags = excluded.tags, module = excluded.module,
                 updated_at = excluded.updated_at""",
            (kind, machine_id, description.strip(), notes.strip(), tags.strip(), module, now),
        )


def set_module(kind: str, machine_id: str, module: str) -> None:
    """Cambia solo el módulo (desde la portada), sin tocar el resto."""
    module = module if module in MODULES else ""
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """INSERT INTO meta (kind, id, module, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (kind, id) DO UPDATE SET module = excluded.module""",
            (kind, machine_id, module, now),
        )
