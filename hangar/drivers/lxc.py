"""Driver LXC clásico (lxc-*).

Los contenedores viven en /var/lib/lxc y son de root, así que todo pasa por
un pequeño script ayudante que se ejecuta con sudo (ver scripts/).
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import threading
import time
from pathlib import Path

from .base import (SNAPSHOT_NAME_RE, DeletePlan, Driver, DriverError, Field, Interface, Machine,
                   Snapshot, Usage, User, check_name, host_bridges, human_bytes, parse_ip_request,
                   parse_number, parse_passwd, run_streaming)

HELPER = "/usr/local/sbin/hangar-lxc-helper"
DOWNLOAD_TEMPLATE = "/usr/share/lxc/templates/lxc-download"
HOST_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
FALLBACK_IMAGES = ["debian/trixie", "debian/bookworm", "ubuntu/noble", "alpine/3.22"]
IMAGES_TTL = 24 * 3600
IMAGES_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") \
    / "hangar" / "lxc-images.json"

CPUSET_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")
UNITS = {"": 1 / 1024 ** 2, "K": 1 / 1024, "M": 1, "G": 1024}


def _parse_config(text: str) -> dict[str, list[str]]:
    cfg: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            cfg.setdefault(key.strip(), []).append(value.strip())
    return cfg


def _memory_mib(value: str) -> int:
    """'2G' → 2048, '512M' → 512, '1073741824' → 1024, 'max' o vacío → 0."""
    m = re.fullmatch(r"(\d+)([KMG]?)", value.strip().upper())
    return round(int(m[1]) * UNITS[m[2]]) if m else 0


STATES = {"RUNNING": "running", "STOPPED": "stopped", "FROZEN": "paused"}


def _parse_seconds(value: str) -> float:
    """'1.23s' o '1.23 seconds' → 1.23."""
    m = re.search(r"([\d.]+)", value or "")
    return float(m[1]) if m else 0.0


def _parse_size(value: str) -> int:
    """'12.34 MiB' → bytes."""
    m = re.match(r"\s*([\d.]+)\s*([KMGTP]?i?B)", value or "")
    if not m:
        return 0
    factor = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3,
              "TiB": 1024 ** 4, "PiB": 1024 ** 5}
    return int(float(m[1]) * factor.get(m[2], 1))


class LxcDriver(Driver):
    kind = "lxc"
    label = "LXC"
    command = ["sudo", "-n", HELPER]
    snapshot_name_auto = True

    # Cada consulta al ayudante es un sudo (~50 ms) y una misma página pide lo
    # mismo varias veces (ficha, recursos, plan de borrado…): las lecturas se
    # guardan un instante y cualquier acción que cambie algo las tira.
    READ_ACTIONS = ("dump", "snap-list")
    READ_TTL = 1.5

    def __init__(self):
        self._reads: dict[tuple, tuple[float, str]] = {}
        self._reads_lock = threading.Lock()

    def _run(self, *args: str) -> str:
        is_read = bool(args) and args[0] in self.READ_ACTIONS
        if is_read:
            with self._reads_lock:
                hit = self._reads.get(args)
                if hit and time.monotonic() - hit[0] < self.READ_TTL:
                    return hit[1]
        else:
            with self._reads_lock:
                self._reads.clear()
        try:
            p = subprocess.run([*self.command, *args],
                               capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as e:
            raise DriverError("LXC tardó demasiado en responder") from e
        except OSError as e:           # p. ej. no hay sudo en el sistema
            raise DriverError(f"No se pudo ejecutar el ayudante LXC: {e}") from e
        except ValueError as e:        # un argumento con un byte nulo
            raise DriverError("Valor no válido.") from e
        finally:
            if not is_read:            # lo que se leyó mientras tanto ya no vale
                with self._reads_lock:
                    self._reads.clear()
        if p.returncode != 0:
            err = p.stderr.strip()
            if "password" in err or "contraseña" in err or "No such file" in err:
                err = ("Falta instalar el ayudante LXC: "
                       "sudo ./scripts/install-lxc-helper.sh")
            raise DriverError(err or f"Error de LXC (código {p.returncode})")
        if is_read:
            with self._reads_lock:
                self._reads[args] = (time.monotonic(), p.stdout)
                if len(self._reads) > 64:
                    self._reads.pop(next(iter(self._reads)))
        return p.stdout

    def _stream(self, args: list[str], log, **kwargs) -> None:
        """Acción larga del ayudante con su salida en el registro de la tarea."""
        try:
            run_streaming([*self.command, *args], log, **kwargs)
        finally:
            with self._reads_lock:     # cambia cosas: las lecturas guardadas ya no valen
                self._reads.clear()

    def list(self) -> list[Machine]:
        return sorted(self._parse_dump(self._run("dump")), key=lambda m: m.name)

    def get(self, machine_id: str) -> Machine:
        machines = self._parse_dump(self._run("dump", machine_id))
        if not machines:
            raise DriverError(f"No existe el contenedor {machine_id}")
        return machines[0]

    def usage(self, machine_id: str) -> Usage:
        m = self.get(machine_id)
        if m.state != "running":
            return Usage(state=m.state)
        resources = dict(m.sections.get("Recursos", []))
        return Usage(state=m.state,
                     cpu_time=_parse_seconds(resources.get("Uso de CPU", "")),
                     mem_used=_parse_size(resources.get("Uso de memoria", "")),
                     mem_limit=m.memory_mib * 2**20)

    def _parse_dump(self, text: str) -> list[Machine]:
        machines = []
        for block in text.split("==> ")[1:]:
            header, _, config = block.partition("--- config\n")
            name, *info_lines = header.strip().splitlines()
            machines.append(self._build(name.strip(), info_lines, config))
        return machines

    def _build(self, name: str, info_lines: list[str], config: str) -> Machine:
        info: dict[str, list[str]] = {}
        for line in info_lines:
            key, _, value = line.partition(":")
            info.setdefault(key.strip(), []).append(value.strip())

        cfg = _parse_config(config)

        def c(key, default="-"):
            return cfg.get(key, [default])[-1]

        state = STATES.get(info.get("State", ["?"])[0], "unknown")
        mem_mib = _memory_mib(c("lxc.cgroup2.memory.max", ""))
        m = Machine(kind=self.kind, id=name, name=name, state=state,
                    cpus=self._cpus(cfg), memory=self._memory_label(c("lxc.cgroup2.memory.max", "")),
                    memory_mib=mem_mib, vcpus=float(self._cpu_count(cfg)),
                    ips=info.get("IP", []), raw_config=config, raw_label="Fichero config")

        m.sections["General"] = [
            ("Nombre de host", c("lxc.uts.name")),
            ("Arquitectura", c("lxc.arch")),
            ("Arranque automático", "sí" if c("lxc.start.auto", "0") == "1" else "no"),
            ("Perfil AppArmor", c("lxc.apparmor.profile")),
            ("Sin privilegios", "sí" if "lxc.idmap" in cfg else "no"),
            ("PID", info.get("PID", ["-"])[0]),
        ]
        m.sections["Recursos"] = [
            ("CPUs", m.cpus),
            ("Memoria máxima", m.memory),
            ("Uso de CPU", info.get("CPU use", ["-"])[0]),
            ("Uso de memoria", info.get("Memory use", ["-"])[0]),
        ]
        m.sections["Almacenamiento"] = [("rootfs", c("lxc.rootfs.path"))] + [
            ("montaje", v) for v in cfg.get("lxc.mount.entry", [])
        ]
        m.sections["Red"] = self._networks(cfg) + [("IP", ip) for ip in m.ips]
        return m

    @staticmethod
    def _memory_label(value: str) -> str:
        mib = _memory_mib(value)
        return human_bytes(mib * 2**20) if mib else "sin límite"

    @staticmethod
    def _cpu_count(cfg) -> int:
        cpuset = cfg.get("lxc.cgroup2.cpuset.cpus", [None])[-1]
        if not cpuset:
            return 0
        count = 0
        for part in cpuset.split(","):
            lo, _, hi = part.partition("-")
            count += int(hi) - int(lo) + 1 if hi else 1
        return count

    @staticmethod
    def _cpus(cfg) -> str:
        cpuset = cfg.get("lxc.cgroup2.cpuset.cpus", [None])[-1]
        if not cpuset:
            return "sin límite"
        return f"{LxcDriver._cpu_count(cfg)} ({cpuset})"

    @staticmethod
    def _networks(cfg) -> list[tuple[str, str]]:
        nets = {}
        for key, values in cfg.items():
            if key.startswith("lxc.net."):
                _, _, idx, prop = key.split(".", 3)
                nets.setdefault(idx, {})[prop] = values[-1]
        return [
            (f"net.{idx}", f"{n.get('type', '?')} → {n.get('link', '-')}"
                           f"{', mac ' + n['hwaddr'] if 'hwaddr' in n else ''}"
                           f"{', ' + n['ipv4.address'] if 'ipv4.address' in n else ''}")
            for idx, n in sorted(nets.items())
        ]

    def _current(self, machine_id: str) -> dict[str, str]:
        cfg = _parse_config(self.get(machine_id).raw_config)
        return {
            "cpuset": cfg.get("lxc.cgroup2.cpuset.cpus", [""])[-1],
            "memory_mib": str(_memory_mib(cfg.get("lxc.cgroup2.memory.max", [""])[-1])),
            "autostart": "1" if cfg.get("lxc.start.auto", ["0"])[-1] == "1" else "",
            "bridge": cfg.get("lxc.net.0.link", [""])[-1],
        }

    def _resource_fields(self, cur: dict[str, str]) -> list[Field]:
        bridges = host_bridges()
        if cur["bridge"] and cur["bridge"] not in bridges:
            bridges.insert(0, cur["bridge"])
        return [
            Field("cpuset", "CPUs asignadas (cpuset)", cur["cpuset"],
                  help="Núcleos del host, p. ej. 0-1 o 0,2. Vacío = sin límite."),
            Field("memory_mib", "Memoria máxima (MiB)", cur["memory_mib"], "number",
                  min=0, help="0 = sin límite"),
            Field("bridge", "Bridge de red (net.0)", cur["bridge"], "select",
                  options=[(b, b) for b in bridges],
                  help="Si está encendido, se aplica al reiniciar el contenedor."),
            Field("autostart", "Arrancar con el host", cur["autostart"], "checkbox"),
        ]

    def editable(self, machine_id: str) -> list[Field]:
        return self._resource_fields(self._current(machine_id))

    def _validate(self, values: dict[str, str]) -> tuple[str, int, str]:
        cpuset = values.get("cpuset", "").replace(" ", "")
        if cpuset and not CPUSET_RE.match(cpuset):
            raise DriverError(f"CPUs: «{cpuset}» no es un cpuset válido (ej. 0-1 o 0,2)")
        memory = parse_number(values, "memory_mib", "Memoria")
        bridge = values.get("bridge", "")
        if bridge and not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", bridge):
            raise DriverError(f"Bridge no válido: {bridge}")
        return cpuset, memory, bridge

    def update(self, machine_id: str, values: dict[str, str]) -> list[str]:
        cur = self._current(machine_id)
        cpuset, memory, bridge = self._validate(values)
        autostart = "autostart" in values

        changes = []
        if bridge and bridge != cur["bridge"]:
            changes.append(("lxc.net.0.link", bridge))
        if cpuset != cur["cpuset"]:
            changes.append(("lxc.cgroup2.cpuset.cpus", cpuset))
        if str(memory) != cur["memory_mib"]:
            changes.append(("lxc.cgroup2.memory.max", f"{memory}M" if memory else ""))
        if autostart != bool(cur["autostart"]):
            changes.append(("lxc.start.auto", "1" if autostart else "0"))

        notes = []
        for key, value in changes:
            out = self._run("set", machine_id, key, value).strip()
            notes.append(out or f"{key} guardado.")
        return notes

    # --- Crear -----------------------------------------------------------

    _images: list[str] = []
    _images_at = 0.0

    def _image_list(self) -> list[str]:
        """Imágenes 'distro/versión' del servidor de lxc-download.

        Bajar el índice tarda unos segundos, así que se guarda en memoria y en
        disco (un día): el formulario de alta se abre al momento."""
        if self._images and time.time() - self._images_at < IMAGES_TTL:
            return self._images
        try:
            saved = json.loads(IMAGES_CACHE.read_text(encoding="utf-8"))
            if saved.get("arch") == HOST_ARCH and saved.get("images") \
                    and time.time() - float(saved.get("at", 0)) < IMAGES_TTL:
                LxcDriver._images, LxcDriver._images_at = list(saved["images"]), float(saved["at"])
                return self._images
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            out = subprocess.run([DOWNLOAD_TEMPLATE, "--list", "--arch", HOST_ARCH],
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        images = [f"{p[0]}/{p[1]}" for p in (line.split() for line in out.splitlines())
                  if len(p) == 5 and p[2] == HOST_ARCH and p[3] == "default"]
        if images:
            LxcDriver._images, LxcDriver._images_at = images, time.time()
            try:
                IMAGES_CACHE.parent.mkdir(parents=True, exist_ok=True)
                IMAGES_CACHE.write_text(json.dumps(
                    {"arch": HOST_ARCH, "at": self._images_at, "images": images}), encoding="utf-8")
            except OSError:
                pass                    # sin caché en disco: solo en memoria
        return images or self._images or FALLBACK_IMAGES

    def create_fields(self) -> list[Field]:
        images = self._image_list()
        default = "debian/trixie" if "debian/trixie" in images else images[0]
        defaults = {"cpuset": "", "memory_mib": "0", "autostart": "",
                    "bridge": "lxcbr0" if "lxcbr0" in host_bridges() else ""}
        return [
            Field("name", "Nombre", "", required=True, placeholder="ct-nuevo"),
            Field("image", "Imagen", default, "select", options=[(i, i) for i in images],
                  help=f"Imágenes de images.linuxcontainers.org para {HOST_ARCH}. "
                       "La primera vez se descargan (puede tardar)."),
            *self._resource_fields(defaults),
            Field("start", "Arrancar al terminar", "1", "checkbox"),
        ]

    def prepare_create(self, values: dict[str, str]) -> dict:
        name = check_name(values, [m.name for m in self.list()])
        image = values.get("image", "")
        if image not in self._image_list():
            raise DriverError("Elige una imagen de la lista.")
        self._validate(values)
        return {"name": name, "image": image, "values": values}

    def create(self, spec: dict, log) -> str:
        name = spec["name"]
        dist, release = spec["image"].split("/", 1)
        self._stream(["create", name, dist, release, HOST_ARCH], log,
                      timeout=1800)
        for note in self.update(name, spec["values"]):
            log(note)
        if "start" in spec["values"]:
            log("Arrancando…")
            self.start(name)
        return name

    def users(self, machine_id: str) -> list[User]:
        return parse_passwd(self._run("passwd", machine_id))

    # --- Consola ---------------------------------------------------------

    console_hint = "Shell de root dentro del contenedor (lxc-attach)."

    def console_command(self, machine_id: str) -> list[str]:
        if self.get(machine_id).state != "running":
            raise DriverError("Arranca el contenedor para abrir su consola.")
        return [*self.command, "attach", machine_id]

    # --- Red ---------------------------------------------------------------

    def interfaces(self, machine_id: str) -> list[Interface]:
        return self._interfaces_of(self.get(machine_id))

    def interfaces_all(self) -> dict[str, list[Interface]]:
        # Un solo volcado del ayudante para todos los contenedores
        return {m.id: self._interfaces_of(m) for m in self._parse_dump(self._run("dump"))}

    @staticmethod
    def _interfaces_of(m: Machine) -> list[Interface]:
        nets: dict[str, dict[str, str]] = {}
        for key, values in _parse_config(m.raw_config).items():
            if key.startswith("lxc.net."):
                _, _, idx, prop = key.split(".", 3)
                nets.setdefault(idx, {})[prop] = values[-1]
        result = []
        for idx, n in sorted(nets.items(), key=lambda item: int(item[0])):
            static = n.get("ipv4.address")
            result.append(Interface(
                key=idx, network=n.get("link", "-"), mac=n.get("hwaddr", ""),
                model=f"net.{idx} ({n.get('type', '?')})",
                # lxc-info da las IPs del contenedor sin decir de qué interfaz son
                ips=m.ips if len(nets) == 1 else [],
                defined_ip=static or "",
                ip_config=(f"estática {static}" + (f", gateway {n['ipv4.gateway']}"
                                                   if n.get("ipv4.gateway") else "")
                           if static else "la que asigne el contenedor (normalmente DHCP)"),
                set_ip_hint="Se guarda en el config del contenedor y se aplica al reiniciarlo. "
                            "Si dentro también corre un cliente DHCP, puede tener las dos IPs.",
            ))
        return result

    def interface_fields(self, machine_id: str) -> list[Field]:
        bridges = host_bridges()
        return [Field("bridge", "Bridge del host", "lxcbr0" if "lxcbr0" in bridges else "",
                      "select", options=[(b, b) for b in bridges])]

    def add_interface(self, machine_id: str, values: dict[str, str]) -> list[str]:
        idx = self._run("net-add", machine_id, values.get("bridge", "")).strip()
        return [f"Interfaz net.{idx} añadida en {values.get('bridge')}: "
                "se activa al reiniciar el contenedor."]

    def remove_interface(self, machine_id: str, key: str) -> list[str]:
        self._run("net-del", machine_id, key)
        return [f"Interfaz net.{key} quitada: se aplica al reiniciar el contenedor."]

    def set_ip(self, machine_id: str, key: str, values: dict[str, str], log) -> None:
        mode, address, gateway, _ = parse_ip_request(values)
        if key not in {i.key for i in self.interfaces(machine_id)}:
            raise DriverError(f"No existe la interfaz net.{key}")
        for prop, value in (("ipv4.address", address), ("ipv4.gateway", gateway)):
            self._run("set", machine_id, f"lxc.net.{key}.{prop}", value)
        log(f"net.{key}: " + (f"IP estática {address}" + (f", gateway {gateway}" if gateway
                                                        else "") if mode == "static"
                             else "sin IP fija (la asignará el contenedor)") + ".")
        if values.get("dns", "").strip():
            log("El DNS no se puede fijar desde el config de LXC: cámbialo dentro del "
                "contenedor (/etc/resolv.conf).")
        if self.get(machine_id).state == "running":
            log("Reinicia el contenedor para aplicar el cambio.")

    # --- Snapshots -------------------------------------------------------

    def snapshots(self, machine_id: str) -> list[Snapshot]:
        snaps = []
        for line in self._run("snap-list", machine_id).splitlines():
            name, _, rest = line.partition("|")
            ts, _, comment = rest.partition("|")
            # lxc guarda la fecha como "2026:10:30 11:05:42"
            created = ts.replace(":", "-", 2)[:16] if ts else "?"
            snaps.append(Snapshot(name=name, created=created, description=comment.strip(),
                                  state="apagado"))
        return sorted(snaps, key=lambda s: int(s.name.removeprefix("snap")))

    def create_snapshot(self, machine_id: str, name: str, description: str, log) -> str:
        before = {s.name for s in self.snapshots(machine_id)}
        log("Copiando el rootfs (puede tardar según lo que ocupe)…")
        self._stream(["snap-create", machine_id, description], log)
        new = [s.name for s in self.snapshots(machine_id) if s.name not in before]
        log(f"Snapshot «{new[0] if new else '?'}» creado.")
        return new[0] if new else ""

    def revert_snapshot(self, machine_id: str, name: str, log) -> None:
        log(f"Restaurando «{name}»: el contenedor actual se sustituye por la copia…")
        self._stream(["snap-restore", machine_id, name], log)
        log("Hecho.")

    def delete_snapshot(self, machine_id: str, name: str, log) -> None:
        self._stream(["snap-delete", machine_id, name], log)
        log(f"Snapshot «{name}» borrado.")

    # --- Borrar ----------------------------------------------------------

    def delete_plan(self, machine_id: str) -> DeletePlan:
        cfg = _parse_config(self.get(machine_id).raw_config)
        rootfs = cfg.get("lxc.rootfs.path", ["?"])[-1]
        plan = DeletePlan(delete=[f"El contenedor «{machine_id}», su config y su rootfs "
                                  f"({rootfs.split(':', 1)[-1]}) con todo lo que haya dentro"])
        snaps = self.snapshots(machine_id)
        if snaps:
            what = "Su snapshot" if len(snaps) == 1 else f"Sus {len(snaps)} snapshots"
            plan.delete.append(f"{what} ({', '.join(s.name for s in snaps)})")
        plan.keep = [f"{entry.split()[0]} (montaje del host)"
                     for entry in cfg.get("lxc.mount.entry", []) if entry.startswith("/")]
        return plan

    def delete(self, machine_id: str, values: dict[str, str]) -> list[str]:
        self._run("destroy", machine_id)
        return [f"Contenedor «{machine_id}» borrado."]

    def start(self, machine_id: str) -> None:
        self._run("start", machine_id)

    def stop(self, machine_id: str) -> None:
        self._run("stop", machine_id)

    def kill(self, machine_id: str) -> None:
        self._run("kill", machine_id)

    def restart(self, machine_id: str) -> None:
        # Apagar + arrancar, no "lxc-stop -r": así vuelve a leer el config y se
        # aplican los cambios guardados (IP, interfaces, límites)
        self._run("stop", machine_id)
        self._run("start", machine_id)
