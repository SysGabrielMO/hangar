"""Driver Docker con el SDK oficial."""
from __future__ import annotations

import functools
import io
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile

import docker
from docker.errors import DockerException, NotFound

from .base import (DeletePlan, Driver, DriverError, Field, Interface, Machine, User, check_name,
                   human_bytes, parse_ip_request, parse_number, parse_passwd)

IMAGE_RE = re.compile(r"^[a-z0-9]+([._/:@-][A-Za-z0-9_.-]+)*$")
DEFAULT_SOCKET = "unix:///var/run/docker.sock"


@functools.cache
def _context_host() -> str | None:
    """Socket del contexto activo del comando docker, si no es el normal.

    Con Docker Desktop o Docker rootless el comando `docker` usa un contexto
    (p. ej. ~/.docker/desktop/docker.sock), pero la librería de Python solo
    mira DOCKER_HOST y /var/run/docker.sock: sin esto, docker funciona en la
    terminal y Hangar no conecta."""
    if os.environ.get("DOCKER_HOST") or not shutil.which("docker"):
        return None
    try:
        out = subprocess.run(["docker", "context", "inspect", "--format",
                              "{{.Endpoints.docker.Host}}"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    host = out.stdout.strip()
    return host if out.returncode == 0 and host and host != DEFAULT_SOCKET else None


def _explain(error: Exception, host: str) -> str:
    """Convierte el error de la librería en el motivo y la solución."""
    text = str(error)
    socket_path = host.removeprefix("unix://")
    fix_service = "Arranca el servicio: sudo systemctl enable --now docker"
    if not shutil.which("docker") and not os.path.exists(socket_path):
        reason = ("Docker no está instalado en este equipo (Hangar funciona igual con KVM "
                  "y LXC). Para usarlo: sudo apt install docker.io")
    elif "Permission denied" in text:
        reason = ("tu usuario no tiene permiso para usar Docker. Añádelo al grupo con "
                  "«sudo usermod -aG docker $USER», cierra sesión y vuelve a entrar (o "
                  "reinicia) y arranca Hangar otra vez.")
    elif "No such file" in text or "FileNotFoundError" in text:
        reason = f"no existe el socket {socket_path}: el servicio no está arrancado. {fix_service}"
    elif "Connection refused" in text:
        reason = f"el servicio no responde en {socket_path}. {fix_service}"
    else:
        return f"No se puede conectar a Docker ({host}): {text}"
    return f"No se puede conectar a Docker: {reason}"


def docker_client() -> docker.DockerClient:
    host = _context_host()
    try:
        return docker.DockerClient(base_url=host) if host else docker.from_env()
    except DockerException as e:
        raise DriverError(_explain(e, host or os.environ.get("DOCKER_HOST", DEFAULT_SOCKET))) from e


BUILTIN_NETWORKS = {"bridge", "host", "none"}   # las de Docker, sin IP fija
ANON_VOLUME_RE = re.compile(r"^[0-9a-f]{64}$")   # Docker nombra así a los anónimos
PORT_RE = re.compile(r"^(?:(\d{1,3}(?:\.\d{1,3}){3}):)?(\d{1,5}):(\d{1,5})(?:/(tcp|udp))?$")

RESTART_POLICIES = {"no": "No reiniciar", "always": "Siempre",
                    "unless-stopped": "Siempre, salvo si lo paro yo",
                    "on-failure": "Solo si falla"}

STATES = {"running": "running", "paused": "paused", "restarting": "running",
          "created": "stopped", "exited": "stopped", "dead": "stopped"}


class DockerDriver(Driver):
    kind = "docker"
    label = "Docker"
    no_snapshots_reason = ("Docker no tiene snapshots: un contenedor se recrea desde su imagen, "
                           "y los datos que quieras conservar deben ir en volúmenes.")

    def _client(self) -> docker.DockerClient:
        return docker_client()

    def _container(self, client, machine_id: str):
        try:
            return client.containers.get(machine_id)
        except NotFound as e:
            raise DriverError(f"No existe el contenedor {machine_id}") from e

    def list(self) -> list[Machine]:
        client = self._client()
        try:
            return sorted((self._summary(c) for c in client.containers.list(all=True)),
                          key=lambda m: m.name)
        finally:
            client.close()

    @staticmethod
    def _cpus(hc) -> float:
        """Límite de CPU, venga de --cpus (NanoCpus) o de --cpu-quota/--cpu-period."""
        if hc.get("NanoCpus"):
            return hc["NanoCpus"] / 1e9
        quota, period = hc.get("CpuQuota") or 0, hc.get("CpuPeriod") or 100000
        return quota / period if quota > 0 else 0

    def _summary(self, c) -> Machine:
        hc = c.attrs["HostConfig"]
        cpus = self._cpus(hc)
        mem = hc.get("Memory") or 0
        nets = c.attrs["NetworkSettings"]["Networks"] or {}
        return Machine(
            kind=self.kind, id=c.name, name=c.name,
            state=STATES.get(c.status, "unknown"),
            cpus=f"{cpus:g}" if cpus else "sin límite",
            memory=human_bytes(mem) if mem else "sin límite",
            memory_mib=mem // 2**20, vcpus=cpus,
            ips=[n["IPAddress"] for n in nets.values() if n.get("IPAddress")],
        )

    def get(self, machine_id: str) -> Machine:
        client = self._client()
        try:
            c = self._container(client, machine_id)
            m = self._summary(c)
            a = c.attrs
            cfg, hc = a["Config"], a["HostConfig"]
            m.raw_config, m.raw_label = json.dumps(a, indent=2), "docker inspect"

            labels = cfg.get("Labels") or {}
            m.sections["General"] = [
                ("ID", c.short_id),
                ("Imagen", cfg.get("Image", "-")),
                ("Comando", " ".join(cfg.get("Cmd") or []) or "-"),
                ("Creado", a["Created"][:19].replace("T", " ")),
                ("Política de reinicio", (hc.get("RestartPolicy") or {}).get("Name") or "no"),
                ("Proyecto compose", labels.get("com.docker.compose.project", "-")),
            ]
            m.sections["Recursos"] = [("CPUs", m.cpus), ("Memoria máxima", m.memory)]
            m.sections["Almacenamiento"] = [
                (f"{mt['Type']}", f"{mt.get('Source') or mt.get('Name')} → {mt['Destination']}"
                                  f"{'' if mt.get('RW', True) else ' (solo lectura)'}")
                for mt in a.get("Mounts", [])
            ]
            m.sections["Red"] = [
                (name, f"{n.get('IPAddress') or 'sin IP'}, mac {n.get('MacAddress') or '-'}")
                for name, n in (a["NetworkSettings"]["Networks"] or {}).items()
            ] + [
                # PortBindings (config) y no NetworkSettings.Ports, que está
                # vacío cuando el contenedor está parado
                ("puerto", f"{(b.get('HostIp') or '0.0.0.0')}:{b.get('HostPort')} → {port}")
                for port, binds in (hc.get("PortBindings") or {}).items() for b in binds or []
            ]
            m.sections["Variables de entorno"] = [
                tuple(e.split("=", 1)) if "=" in e else (e, "") for e in cfg.get("Env") or []
            ]
            return m
        finally:
            client.close()

    def editable(self, machine_id: str) -> list[Field]:
        client = self._client()
        try:
            hc = self._container(client, machine_id).attrs["HostConfig"]
            cpus = self._cpus(hc)
            policy = (hc.get("RestartPolicy") or {}).get("Name") or "no"
            return [
                Field("cpus", "CPUs", f"{cpus:g}", "number", min=0,
                      help="Puede ser decimal (1.5). 0 = sin límite."),
                Field("memory_mib", "Memoria máxima (MiB)", str((hc.get("Memory") or 0) // 2**20),
                      "number", min=0,
                      help="0 = sin límite (Docker no deja quitar un límite ya puesto)."),
                Field("restart", "Política de reinicio", policy, "select",
                      options=list(RESTART_POLICIES.items())),
            ]
        finally:
            client.close()

    def update(self, machine_id: str, values: dict[str, str]) -> list[str]:
        cpus = parse_number(values, "cpus", "CPUs", integer=False)
        memory_mib = parse_number(values, "memory_mib", "Memoria")
        policy = values.get("restart", "no")
        if policy not in RESTART_POLICIES:
            raise DriverError(f"Política de reinicio no válida: {policy}")
        if memory_mib and memory_mib < 6:
            raise DriverError("Memoria: Docker exige al menos 6 MiB")

        client = self._client()
        try:
            c = self._container(client, machine_id)
            hc = c.attrs["HostConfig"]
            current_mem = (hc.get("Memory") or 0) // 2**20
            if memory_mib == 0 and current_mem:
                raise DriverError("Docker no permite quitar un límite de memoria ya puesto: "
                                  "pon un valor más alto o recrea el contenedor.")
            changes = {}
            if abs(cpus - self._cpus(hc)) > 1e-9:
                # NanoCpus no se puede cambiar con update; quota/period es equivalente
                changes |= ({"cpu_period": 100000, "cpu_quota": int(cpus * 100000)}
                            if cpus else {"cpu_quota": -1})
            if memory_mib != current_mem:
                # swap sin límite extra, para que no choque con el nuevo valor
                changes |= {"mem_limit": memory_mib * 2**20, "memswap_limit": -1}
            if policy != ((hc.get("RestartPolicy") or {}).get("Name") or "no"):
                changes["restart_policy"] = {"Name": policy}
            if not changes:
                return []
            c.update(**changes)
            return ["Cambios aplicados (en caliente si el contenedor está encendido)."]
        except DockerException as e:
            raise DriverError(str(e)) from e
        finally:
            client.close()

    # --- Crear -----------------------------------------------------------

    def create_fields(self) -> list[Field]:
        client = self._client()
        try:
            images = sorted({t for img in client.images.list() for t in img.tags})
            networks = sorted(n.name for n in client.networks.list())
        finally:
            client.close()
        return [
            Field("name", "Nombre", "", required=True, placeholder="web-nuevo"),
            Field("image", "Imagen", "", required=True, suggestions=images,
                  placeholder="nginx:latest",
                  help="Si no está descargada, se descarga de Docker Hub."),
            Field("command", "Comando", "", placeholder="(el de la imagen)",
                  help="Opcional, p. ej. sleep infinity"),
            Field("ports", "Puertos", "", placeholder="8080:80, 127.0.0.1:2222:22",
                  help="host:contenedor separados por comas. Añade /udp si hace falta."),
            Field("volumes", "Volúmenes", "", "textarea",
                  placeholder="/srv/web:/var/www/html\ndatos:/var/lib/mysql\n/etc/hosts:/etc/hosts:ro",
                  help="Uno por línea: ruta del host o nombre de volumen : ruta dentro [:ro]"),
            Field("env", "Variables de entorno", "", "textarea",
                  placeholder="MYSQL_ROOT_PASSWORD=cambiame\nTZ=Europe/Madrid", help="CLAVE=valor, una por línea"),
            Field("network", "Red", "bridge", "select", options=[(n, n) for n in networks]),
            Field("restart", "Política de reinicio", "no", "select",
                  options=list(RESTART_POLICIES.items())),
            Field("cpus", "CPUs", "0", "number", min=0, help="0 = sin límite"),
            Field("memory_mib", "Memoria máxima (MiB)", "0", "number", min=0,
                  help="0 = sin límite"),
            Field("start", "Arrancar al crear", "1", "checkbox"),
        ]

    def prepare_create(self, values: dict[str, str]) -> dict:
        client = self._client()
        try:
            existing = [c.name for c in client.containers.list(all=True)]
            networks = [n.name for n in client.networks.list()]
        finally:
            client.close()

        image = values.get("image", "").strip()
        if not IMAGE_RE.match(image):
            raise DriverError("Imagen: escribe un nombre válido, p. ej. nginx:latest")
        ports = {}
        for item in filter(None, (p.strip() for p in values.get("ports", "").split(","))):
            m = PORT_RE.match(item)
            if not m:
                raise DriverError(f"Puerto no válido: «{item}» (usa 8080:80)")
            ip, host, inner, proto = m.groups()
            ports[f"{inner}/{proto or 'tcp'}"] = (ip, int(host)) if ip else int(host)
        volumes = []
        for line in filter(None, (v.strip() for v in values.get("volumes", "").splitlines())):
            parts = line.split(":")
            if len(parts) not in (2, 3) or not parts[1].startswith("/") or \
                    (len(parts) == 3 and parts[2] not in ("ro", "rw")):
                raise DriverError(f"Volumen no válido: «{line}» (usa /ruta/host:/ruta/dentro)")
            volumes.append(line)
        env = []
        for line in filter(None, (e.strip() for e in values.get("env", "").splitlines())):
            if "=" not in line or not line.split("=", 1)[0]:
                raise DriverError(f"Variable no válida: «{line}» (usa CLAVE=valor)")
            env.append(line)
        if values.get("network") not in networks:
            raise DriverError("Elige una red.")
        if values.get("restart") not in RESTART_POLICIES:
            raise DriverError("Elige una política de reinicio.")
        memory = parse_number(values, "memory_mib", "Memoria")
        if memory and memory < 6:
            raise DriverError("Memoria: Docker exige al menos 6 MiB")
        try:
            command = shlex.split(values.get("command", ""))
        except ValueError as e:
            raise DriverError(f"Comando: {e}") from e

        return {
            "name": check_name(values, existing), "image": image, "command": command or None,
            "ports": ports, "volumes": volumes, "environment": env,
            "network": values["network"], "restart_policy": {"Name": values["restart"]},
            "nano_cpus": int(parse_number(values, "cpus", "CPUs", integer=False) * 1e9) or None,
            "mem_limit": memory * 2**20 or None, "start": "start" in values,
        }

    def create(self, spec: dict, log) -> str:
        spec = dict(spec)
        start = spec.pop("start")
        client = self._client()
        try:
            try:
                client.images.get(spec["image"])
            except NotFound:
                self._pull(client, spec["image"], log)
            log("Creando el contenedor…")
            c = client.containers.create(**spec)
            if start:
                log("Arrancando…")
                c.start()
            log("Listo.")
            return c.name
        except DockerException as e:
            raise DriverError(getattr(e, "explanation", None) or str(e)) from e
        finally:
            client.close()

    @staticmethod
    def _pull(client, image: str, log) -> None:
        log(f"Descargando {image}…")
        seen = set()
        # El SDK separa repo y tag (o digest) y usa "latest" si no hay ninguno
        for event in client.api.pull(image, stream=True, decode=True):
            if "error" in event:
                raise DriverError(event["error"])
            # Solo los cambios de estado de cada capa, no cada tick de progreso
            line = f"{event.get('id', '')} {event.get('status', '')}".strip()
            if line not in seen and "Downloading" not in line and "Extracting" not in line:
                seen.add(line)
                log(line)

    def users(self, machine_id: str) -> list[User]:
        client = self._client()
        try:
            c = self._container(client, machine_id)
            # get_archive funciona aunque el contenedor esté parado
            try:
                stream, _ = c.get_archive("/etc/passwd")
            except NotFound as e:
                raise DriverError("La imagen no tiene /etc/passwd") from e
            with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
                member = tar.getmembers()[0]
                text = tar.extractfile(member).read().decode(errors="replace")
            return parse_passwd(text)
        finally:
            client.close()

    # --- Consola ---------------------------------------------------------

    console_hint = "Shell dentro del contenedor (docker exec): bash si lo tiene, si no sh."

    def console_command(self, machine_id: str) -> list[str]:
        client = self._client()
        try:
            c = self._container(client, machine_id)
            if c.status != "running":
                raise DriverError("Arranca el contenedor para abrir su consola.")
        finally:
            client.close()
        return ["docker", "exec", "-it", "-e", "TERM=xterm-256color", machine_id, "sh", "-c",
                "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"]

    # --- Red ---------------------------------------------------------------

    def interfaces(self, machine_id: str) -> list[Interface]:
        client = self._client()
        try:
            c = self._container(client, machine_id)
            result = []
            for name, n in (c.attrs["NetworkSettings"]["Networks"] or {}).items():
                fixed = (n.get("IPAMConfig") or {}).get("IPv4Address")
                builtin = name in BUILTIN_NETWORKS
                result.append(Interface(
                    key=name, network=name, mac=n.get("MacAddress") or "",
                    ips=[f"{n['IPAddress']}/{n.get('IPPrefixLen')}"] if n.get("IPAddress") else [],
                    ip_config=f"fija {fixed}" if fixed else "automática (IPAM de Docker)",
                    can_set_ip=not builtin,
                    set_ip_hint=("Docker solo permite IP fija en redes creadas por ti "
                                 "(con subred definida)." if builtin else
                                 "Se desconecta y vuelve a conectar a la red con la nueva IP "
                                 "(pierde la conexión un instante)."),
                ))
            return result
        finally:
            client.close()

    def interface_fields(self, machine_id: str) -> list[Field]:
        client = self._client()
        try:
            connected = set(self._container(client, machine_id)
                            .attrs["NetworkSettings"]["Networks"] or {})
            names = sorted(n.name for n in client.networks.list()
                           if n.name not in connected and n.name not in ("host", "none"))
        finally:
            client.close()
        return [
            Field("network", "Red de Docker", names[0] if names else "", "select",
                  options=[(n, n) for n in names]),
            Field("ip", "IP fija", "", placeholder="automática",
                  help="Opcional; solo en redes creadas por ti con subred."),
        ]

    def add_interface(self, machine_id: str, values: dict[str, str]) -> list[str]:
        network, ip = values.get("network", ""), values.get("ip", "").strip()
        if ip:
            parse_ip_request({"mode": "static", "address": f"{ip}/32"})
        client = self._client()
        try:
            client.networks.get(network).connect(machine_id, ipv4_address=ip or None)
            return [f"Conectado a «{network}»" + (f" con IP {ip}." if ip else ".")]
        except DockerException as e:
            raise DriverError(getattr(e, "explanation", None) or str(e)) from e
        finally:
            client.close()

    def remove_interface(self, machine_id: str, key: str) -> list[str]:
        client = self._client()
        try:
            client.networks.get(key).disconnect(machine_id)
            return [f"Desconectado de «{key}»."]
        except DockerException as e:
            raise DriverError(getattr(e, "explanation", None) or str(e)) from e
        finally:
            client.close()

    def set_ip(self, machine_id: str, key: str, values: dict[str, str], log) -> None:
        mode, address, gateway, dns = parse_ip_request(values)
        if key in BUILTIN_NETWORKS:
            raise DriverError("Docker solo permite IP fija en redes creadas por ti.")
        ip = address.split("/")[0] if mode == "static" else None
        client = self._client()
        try:
            net = client.networks.get(key)
            if ip:
                subnets = [cfg.get("Subnet") for cfg in net.attrs["IPAM"].get("Config") or []]
                if subnets and not any(ipaddress.ip_address(ip) in ipaddress.ip_network(sn)
                                       for sn in subnets if sn):
                    raise DriverError(f"{ip} no está en la subred de «{key}» ({', '.join(subnets)}).")
            log(f"Desconectando de «{key}»…")
            net.disconnect(machine_id)
            try:
                net.connect(machine_id, ipv4_address=ip)
            except DockerException:
                net.connect(machine_id)   # que no se quede sin red si la IP no vale
                log("No se pudo poner esa IP; se ha vuelto a conectar con IP automática.")
                raise
            log(f"Conectado de nuevo con IP {ip}." if ip else "Conectado con IP automática.")
            if gateway or dns:
                log("La puerta de enlace y el DNS los pone Docker según la red; se ignoran.")
        except DockerException as e:
            raise DriverError(getattr(e, "explanation", None) or str(e)) from e
        finally:
            client.close()

    # --- Borrar ----------------------------------------------------------

    def delete_plan(self, machine_id: str) -> DeletePlan:
        client = self._client()
        try:
            c = self._container(client, machine_id)
            plan = DeletePlan(delete=[f"El contenedor «{c.name}»"],
                              keep=[f"La imagen {c.attrs['Config'].get('Image')}"])
            for mt in c.attrs.get("Mounts", []):
                if mt["Type"] == "bind":
                    plan.keep.append(f"{mt['Source']} (ruta del host)")
                elif ANON_VOLUME_RE.match(mt.get("Name", "")):
                    plan.delete.append(f"Volumen anónimo {mt['Name'][:12]}… → {mt['Destination']} "
                                       "(si marcas «Borrar volúmenes anónimos»)")
                else:
                    plan.keep.append(f"Volumen «{mt.get('Name')}» (con nombre)")
            plan.options.append(Field("delete_volumes", "Borrar volúmenes anónimos", "1",
                                      "checkbox"))
            return plan
        finally:
            client.close()

    def delete(self, machine_id: str, values: dict[str, str]) -> list[str]:
        client = self._client()
        try:
            c = self._container(client, machine_id)
            if c.status in ("running", "paused", "restarting"):
                raise DriverError("Apaga el contenedor antes de borrarlo.")
            c.remove(v="delete_volumes" in values)
            return [f"Contenedor «{c.name}» borrado."]
        except DockerException as e:
            raise DriverError(getattr(e, "explanation", None) or str(e)) from e
        finally:
            client.close()

    def start(self, machine_id: str) -> None:
        self._action(machine_id, lambda c: c.unpause() if c.status == "paused" else c.start())

    def stop(self, machine_id: str) -> None:
        self._action(machine_id, lambda c: c.stop())

    def kill(self, machine_id: str) -> None:
        self._action(machine_id, lambda c: c.kill())

    def restart(self, machine_id: str) -> None:
        self._action(machine_id, lambda c: c.restart())

    def _action(self, machine_id, fn) -> None:
        client = self._client()
        try:
            fn(self._container(client, machine_id))
        except DockerException as e:
            raise DriverError(str(e)) from e
        finally:
            client.close()
