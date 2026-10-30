"""Modelo común y contrato que cumple cada driver (KVM, LXC, Docker).

La interfaz web solo habla con esta interfaz, así que una VM, un
contenedor LXC y uno Docker se tratan igual.
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class DriverError(Exception):
    """Error al hablar con el hipervisor / runtime (se muestra en la web)."""


@dataclass
class User:
    name: str
    uid: int
    home: str
    shell: str

    @property
    def can_login(self) -> bool:
        return not self.shell.endswith(("nologin", "false", "sync", "shutdown", "halt"))


@dataclass
class Field:
    """Un campo modificable de una máquina; la web genera el formulario con esto."""
    key: str
    label: str
    value: str
    type: str = "text"        # "text" | "number" | "select" | "checkbox" | "textarea"
    help: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)  # (valor, texto)
    suggestions: list[str] = field(default_factory=list)          # autocompletar en "text"
    required: bool = False
    placeholder: str = ""
    min: float | None = None
    step: float | None = None


@dataclass
class Snapshot:
    name: str
    created: str              # "AAAA-MM-DD HH:MM"
    description: str = ""
    state: str = ""           # estado de la máquina al hacerlo ("encendida", "apagada")
    parent: str = ""
    current: bool = False     # la máquina parte de este snapshot


SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


@dataclass
class DeletePlan:
    delete: list[str] = field(default_factory=list)   # lo que desaparece
    keep: list[str] = field(default_factory=list)     # lo que se conserva
    options: list[Field] = field(default_factory=list)


@dataclass
class Machine:
    kind: str                 # "kvm" | "lxc" | "docker"
    id: str                   # clave estable: UUID (kvm), nombre (lxc, docker)
    name: str
    state: str                # "running" | "stopped" | "paused" | "unknown"
    cpus: str = "-"
    memory: str = "-"
    ips: list[str] = field(default_factory=list)
    # Secciones de la ficha: {"Recursos": [("vCPU", "2"), ...], ...}
    sections: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    raw_config: str = ""      # XML (kvm), config (lxc) o inspect JSON (docker)
    raw_label: str = "Configuración"
    display: str = ""         # pantalla gráfica (spice/vnc) para abrir con virt-viewer


@dataclass
class Interface:
    """Una tarjeta de red de la máquina."""
    key: str                  # identificador para las acciones: MAC (kvm), índice (lxc), red (docker)
    network: str              # red o bridge al que está conectada
    mac: str = ""
    model: str = ""
    ips: list[str] = field(default_factory=list)       # IPs que tiene ahora
    ip_config: str = ""       # cómo obtiene la IP: "DHCP de libvirt", "estática 10.0.0.5/24"…
    dhcp: bool = False        # la red le da IP por DHCP (se cambia con reserva)
    can_set_ip: bool = True
    set_ip_hint: str = ""     # cómo se aplicará el cambio de IP


class Driver:
    kind: str = ""
    label: str = ""
    # Explicación si no hay snapshots (se muestra en su pestaña); vacío = sí hay
    no_snapshots_reason: str = ""
    snapshot_name_auto: bool = False   # el runtime pone el nombre (LXC: snap0, snap1…)
    console_hint: str = ""             # se muestra encima de la terminal web

    def list(self) -> list[Machine]:
        raise NotImplementedError

    def get(self, machine_id: str) -> Machine:
        raise NotImplementedError

    def users(self, machine_id: str) -> list[User]:
        """Usuarios definidos dentro de la máquina (/etc/passwd)."""
        raise NotImplementedError

    def editable(self, machine_id: str) -> list[Field]:
        """Campos que se pueden cambiar, con su valor actual."""
        raise NotImplementedError

    def update(self, machine_id: str, values: dict[str, str]) -> list[str]:
        """Aplica los cambios y devuelve avisos para el usuario.

        `values` trae solo los campos del formulario (los checkbox sin marcar
        no llegan). Lanza DriverError si algún valor no es válido.
        """
        raise NotImplementedError

    def create_fields(self) -> list[Field]:
        """Formulario para crear una máquina nueva."""
        raise NotImplementedError

    def prepare_create(self, values: dict[str, str]) -> dict:
        """Valida el formulario (rápido, antes de lanzar la tarea) y devuelve
        la especificación que recibirá create(). Lanza DriverError."""
        raise NotImplementedError

    def create(self, spec: dict, log) -> str:
        """Crea la máquina (en segundo plano), escribe el progreso con
        log(texto) y devuelve el id de la máquina nueva."""
        raise NotImplementedError

    def delete_plan(self, machine_id: str) -> DeletePlan:
        """Qué se borraría y qué se conservaría (para enseñarlo antes)."""
        raise NotImplementedError

    def delete(self, machine_id: str, values: dict[str, str]) -> list[str]:
        """Borra la máquina (debe estar apagada) según las opciones del plan."""
        raise NotImplementedError

    def console_command(self, machine_id: str) -> list[str]:
        """Comando que abre una terminal en la máquina (debe estar encendida)."""
        raise NotImplementedError

    def open_display(self, machine_id: str) -> None:
        raise DriverError("Esta máquina no tiene pantalla gráfica.")

    # Si es True, la página de consola ofrece «Activar consola serie»
    can_enable_serial: bool = False

    def enable_serial_console(self, machine_id: str, log) -> None:
        raise DriverError("No disponible para este tipo de máquina.")

    def interfaces(self, machine_id: str) -> list[Interface]:
        raise NotImplementedError

    def interface_fields(self, machine_id: str) -> list[Field]:
        """Formulario para añadir una interfaz."""
        raise NotImplementedError

    def add_interface(self, machine_id: str, values: dict[str, str]) -> list[str]:
        raise NotImplementedError

    def remove_interface(self, machine_id: str, key: str) -> list[str]:
        raise NotImplementedError

    def set_ip(self, machine_id: str, key: str, values: dict[str, str], log) -> None:
        """values: mode ("static"|"dhcp"), address ("10.0.0.5/24"), gateway, dns."""
        raise NotImplementedError

    def snapshots(self, machine_id: str) -> list[Snapshot]:
        """Snapshots de la máquina, del más antiguo al más reciente."""
        raise NotImplementedError

    def create_snapshot(self, machine_id: str, name: str, description: str, log) -> str:
        """Crea un snapshot (en segundo plano) y devuelve su nombre."""
        raise NotImplementedError

    def revert_snapshot(self, machine_id: str, name: str, log) -> None:
        raise NotImplementedError

    def delete_snapshot(self, machine_id: str, name: str, log) -> None:
        raise NotImplementedError

    def start(self, machine_id: str) -> None:
        """Arranca, o reanuda si está pausada."""
        raise NotImplementedError

    def stop(self, machine_id: str) -> None:
        """Apagado ordenado."""
        raise NotImplementedError

    def kill(self, machine_id: str) -> None:
        """Apagado forzado (como quitar el cable)."""
        raise NotImplementedError

    def restart(self, machine_id: str) -> None:
        raise NotImplementedError


def parse_passwd(text: str) -> list[User]:
    users = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 7 or line.startswith("#"):
            continue
        try:
            uid = int(parts[2])
        except ValueError:
            continue
        users.append(User(name=parts[0], uid=uid, home=parts[5], shell=parts[6]))
    return users


def parse_number(values: dict[str, str], key: str, label: str, *, minimum: float = 0,
                 integer: bool = True) -> float:
    raw = values.get(key, "").strip().replace(",", ".")
    try:
        n = int(raw) if integer else float(raw)
    except ValueError:
        raise DriverError(f"{label}: «{raw}» no es un número válido") from None
    if n < minimum:
        raise DriverError(f"{label}: el mínimo es {minimum:g}")
    return n


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def check_name(values: dict[str, str], existing: list[str]) -> str:
    name = values.get("name", "").strip()
    if not NAME_RE.match(name):
        raise DriverError("Nombre: usa letras, números, guiones, puntos o guiones bajos "
                          "(máx. 63, sin espacios).")
    if name in existing:
        raise DriverError(f"Ya existe una máquina llamada «{name}».")
    return name


def run_streaming(cmd: list[str], log, timeout: int = 3600) -> None:
    """Ejecuta un comando enviando su salida al log de la tarea."""
    log("$ " + " ".join(cmd))
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    except FileNotFoundError as e:
        raise DriverError(f"No se encuentra {cmd[0]}") from e
    last = ""
    try:
        for line in p.stdout:
            log(line)
            last = line.strip() or last
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        p.kill()
        raise DriverError("La operación tardó demasiado y se canceló") from e
    if p.returncode != 0:
        # La última línea suele ser el motivo («Apaga el contenedor…»)
        raise DriverError(last or f"{cmd[0]} terminó con error (código {p.returncode}).")


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit in ("B", "KiB") else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def parse_ip_request(values: dict[str, str]) -> tuple[str, str, str, str]:
    """Valida el formulario de cambiar IP → (modo, ip/prefijo, gateway, dns)."""
    mode = values.get("mode", "static")
    if mode not in ("static", "dhcp"):
        raise DriverError("Modo de IP no válido.")
    if mode == "dhcp":
        return mode, "", "", ""
    try:
        iface = ipaddress.ip_interface(values.get("address", "").strip())
    except ValueError:
        raise DriverError("IP: escríbela con su máscara, p. ej. 192.168.10.20/24") from None
    if iface.version != 4 or "/" not in values.get("address", ""):
        raise DriverError("IP: escríbela en IPv4 con su máscara, p. ej. 192.168.10.20/24")
    gateway = values.get("gateway", "").strip()
    dns = values.get("dns", "").strip()
    for label, value in (("Puerta de enlace", gateway), *(("DNS", d) for d in dns.split())):
        if value:
            try:
                ipaddress.IPv4Address(value)
            except ValueError:
                raise DriverError(f"{label}: «{value}» no es una IPv4 válida") from None
    if gateway and ipaddress.IPv4Address(gateway) not in iface.network:
        raise DriverError(f"La puerta de enlace {gateway} no está en la red {iface.network}")
    return mode, str(iface), gateway, " ".join(dns.split())


def host_bridges() -> list[str]:
    """Bridges de red del host (br0, virbr0, lxcbr0, docker0…)."""
    net = Path("/sys/class/net")
    return sorted(p.name for p in net.iterdir() if (p / "bridge").exists())
