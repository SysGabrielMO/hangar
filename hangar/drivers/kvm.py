"""Driver KVM a través de libvirt (qemu:///system)."""
from __future__ import annotations

import base64
import functools
import ipaddress
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import libvirt

from .base import (SNAPSHOT_NAME_RE, DeletePlan, Disk, Driver, DriverError, Field, Interface,
                   Machine, Snapshot, Usage, User, check_name, host_bridges, human_bytes,
                   parse_ip_request, parse_number, parse_passwd, run_streaming)

GIB = 1024 ** 3
DISK_FORMATS = ("qcow2", "raw")
NETWORK_MODELS = ("virtio", "e1000e", "rtl8139")
# Config persistente (no la de ejecución) y sin ocultar contraseñas VNC/SPICE,
# porque este XML se vuelve a guardar con defineXML.
XML_FLAGS = libvirt.VIR_DOMAIN_XML_INACTIVE | libvirt.VIR_DOMAIN_XML_SECURE


# Se ejecuta como root dentro de la VM (con qemu-guest-agent) para cambiar la IP
# de forma persistente, con lo que use cada distro.
# Argumentos: interfaz, modo (static|dhcp), ip/prefijo, gateway, dns (separados por espacios)
GUEST_SET_IP = r"""
set -e
IF="$1"; MODE="$2"; ADDR="$3"; GW="$4"; DNS="$5"
nm_managed() {
    command -v nmcli >/dev/null 2>&1 || return 1
    nmcli -t -f RUNNING general 2>/dev/null | grep -q running || return 1
    nmcli -t -f DEVICE,STATE device 2>/dev/null | grep "^$IF:" | grep -vq unmanaged
}
if nm_managed; then
    CON=$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v d="$IF" '$2==d {print $1; exit}')
    [ -n "$CON" ] || CON=$(nmcli -t -f NAME,DEVICE connection show | awk -F: -v d="$IF" '$2==d {print $1; exit}')
    if [ -z "$CON" ]; then
        CON="hangar-$IF"
        nmcli connection add type ethernet ifname "$IF" con-name "$CON" >/dev/null
    fi
    if [ "$MODE" = dhcp ]; then
        nmcli connection modify "$CON" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns ""
    else
        nmcli connection modify "$CON" ipv4.method manual ipv4.addresses "$ADDR" ipv4.gateway "$GW" ipv4.dns "$DNS"
    fi
    nmcli connection up "$CON" >/dev/null
    echo "Aplicado con NetworkManager (conexión «$CON»)."
elif [ -d /etc/netplan ] && ls /etc/netplan/*.yaml >/dev/null 2>&1; then
    F="/etc/netplan/99-hangar-$IF.yaml"
    {
        echo "network:"
        echo "  version: 2"
        echo "  ethernets:"
        echo "    $IF:"
        if [ "$MODE" = dhcp ]; then
            echo "      dhcp4: true"
        else
            echo "      dhcp4: false"
            echo "      addresses: [$ADDR]"
            [ -z "$GW" ] || printf '      routes:\n        - to: default\n          via: %s\n' "$GW"
            [ -z "$DNS" ] || echo "      nameservers: {addresses: [$(echo $DNS | sed 's/ /, /g')]}"
        fi
    } > "$F"
    chmod 600 "$F"
    netplan apply
    echo "Aplicado con netplan ($F)."
elif [ -f /etc/network/interfaces ]; then
    F=/etc/network/interfaces
    ifdown "$IF" >/dev/null 2>&1 || true
    # Quita el bloque anterior de Hangar y comenta la configuración previa de la interfaz
    sed -i "/^# hangar-$IF inicio/,/^# hangar-$IF fin/d" "$F"
    for f in "$F" /etc/network/interfaces.d/*; do
        [ -f "$f" ] || continue
        awk -v d="$IF" '
            /^[^ \t#]/ { skip = 0 }
            $1 == "iface" && $2 == d { skip = 1 }
            ($1 == "auto" || $1 == "allow-hotplug") && NF == 2 && $2 == d { print "#hangar# " $0; next }
            { print (skip && NF ? "#hangar# " $0 : $0) }' "$f" > "$f.hangar.tmp"
        cat "$f.hangar.tmp" > "$f"
        rm -f "$f.hangar.tmp"
    done
    {
        echo "# hangar-$IF inicio"
        echo "auto $IF"
        if [ "$MODE" = dhcp ]; then
            echo "iface $IF inet dhcp"
        else
            echo "iface $IF inet static"
            echo "    address $ADDR"
            [ -z "$GW" ] || echo "    gateway $GW"
            [ -z "$DNS" ] || echo "    dns-nameservers $DNS"
        fi
        echo "# hangar-$IF fin"
    } >> "$F"
    ip addr flush dev "$IF"
    ifup "$IF"
    if [ -n "$DNS" ] && ! command -v resolvconf >/dev/null 2>&1; then
        cp /etc/resolv.conf /etc/resolv.conf.hangar.bak 2>/dev/null || true
        for s in $DNS; do echo "nameserver $s"; done > /etc/resolv.conf
    fi
    echo "Aplicado en /etc/network/interfaces."
elif command -v networkctl >/dev/null 2>&1 && systemctl is-active -q systemd-networkd; then
    F="/etc/systemd/network/10-hangar-$IF.network"
    {
        printf '[Match]\nName=%s\n\n[Network]\n' "$IF"
        if [ "$MODE" = dhcp ]; then
            echo "DHCP=ipv4"
        else
            echo "Address=$ADDR"
            [ -z "$GW" ] || echo "Gateway=$GW"
            for s in $DNS; do echo "DNS=$s"; done
        fi
    } > "$F"
    networkctl reload
    networkctl reconfigure "$IF"
    echo "Aplicado con systemd-networkd ($F)."
else
    ip addr flush dev "$IF"
    if [ "$MODE" = static ]; then
        ip addr add "$ADDR" dev "$IF"
        ip link set "$IF" up
        [ -z "$GW" ] || ip route replace default via "$GW"
    fi
    echo "AVISO: no se reconoce el gestor de red; cambio aplicado con ip, NO es persistente."
fi
echo
ip -4 addr show dev "$IF" | sed -n 's/^ *inet /IP actual: /p'
"""


# Se ejecuta como root dentro de la VM para que haya un login en el puerto
# serie (lo que muestra la consola web) y, con GRUB, también los mensajes de
# arranque.
GUEST_ENABLE_SERIAL = r"""
set -e
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl enable --now serial-getty@ttyS0.service
    echo "Login activado en ttyS0 (serial-getty@ttyS0, también tras reiniciar)."
elif [ -f /etc/inittab ]; then
    if grep -q '^[^#]*ttyS0' /etc/inittab; then
        echo "/etc/inittab ya tenía un login en ttyS0."
    elif grep -q '^[^#:]*::' /etc/inittab; then
        # Formato de BusyBox (Alpine)
        echo 'ttyS0::respawn:/sbin/getty -L 115200 ttyS0 vt100' >> /etc/inittab
        echo "Login añadido a /etc/inittab."
    else
        echo 'T0:23:respawn:/sbin/getty -L ttyS0 115200 vt100' >> /etc/inittab
        echo "Login añadido a /etc/inittab."
    fi
    kill -HUP 1
else
    echo "No se reconoce el sistema de arranque (ni systemd ni /etc/inittab)."
    exit 1
fi

if [ -f /etc/default/grub ] && ! grep -q 'console=ttyS0' /etc/default/grub; then
    if grep -q '^GRUB_CMDLINE_LINUX=' /etc/default/grub; then
        cp /etc/default/grub /etc/default/grub.hangar.bak
        sed -i -e 's/^GRUB_CMDLINE_LINUX="\(.*\)"/GRUB_CMDLINE_LINUX="\1 console=tty0 console=ttyS0,115200n8"/' \
               -e 's/^GRUB_CMDLINE_LINUX=" /GRUB_CMDLINE_LINUX="/' /etc/default/grub
        if command -v update-grub >/dev/null 2>&1; then
            update-grub >/dev/null 2>&1
        elif command -v grub2-mkconfig >/dev/null 2>&1; then
            grub2-mkconfig -o /boot/grub2/grub.cfg >/dev/null 2>&1
        fi
        echo "GRUB: los mensajes de arranque saldrán también por el puerto serie desde el"
        echo "próximo reinicio (copia del original en /etc/default/grub.hangar.bak)."
    fi
fi
"""


def _parse_keep_ns(xml: str) -> ET.Element:
    """Parsea registrando los prefijos (libosinfo:, qemu:…) para que
    ET.tostring los conserve en vez de escribir ns0:, ns1:…"""
    for _, (prefix, uri) in ET.iterparse(io.StringIO(xml), events=["start-ns"]):
        ET.register_namespace(prefix, uri)
    return ET.fromstring(xml)

try:
    import libvirt_qemu  # para hablar con qemu-guest-agent
except ImportError:  # pragma: no cover
    libvirt_qemu = None

# libvirt escribe sus errores por stderr además de lanzar la excepción
libvirt.registerErrorHandler(lambda *_: None, None)

STATES = {
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "running",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "running",   # apagándose
    libvirt.VIR_DOMAIN_SHUTOFF: "stopped",
    libvirt.VIR_DOMAIN_CRASHED: "stopped",
}


def _parse_disks(text: str, pools: set[str], default_pool: str) -> list[dict]:
    """'20:default:qcow2' por línea → discos validados."""
    disks = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(":")]
        if len(parts) > 3:
            raise DriverError(f"Disco no válido: «{line}» (usa tamaño[:pool[:formato]])")
        try:
            size = int(float(parts[0]))
        except ValueError:
            raise DriverError(f"Disco «{line}»: el tamaño no es un número") from None
        if size < 1:
            raise DriverError(f"Disco «{line}»: el tamaño mínimo es 1 GiB")
        pool = parts[1] if len(parts) > 1 and parts[1] else default_pool
        fmt = parts[2] if len(parts) > 2 and parts[2] else "qcow2"
        if pool not in pools:
            raise DriverError(f"Disco «{line}»: no existe el pool «{pool}»")
        if fmt not in DISK_FORMATS:
            raise DriverError(f"Disco «{line}»: formato no válido (qcow2 o raw)")
        disks.append({"size_gib": size, "pool": pool, "format": fmt})
    if not disks:
        raise DriverError("Añade al menos un disco.")
    return disks


def _parse_networks(text: str, networks: set[str], bridges: set[str]) -> list[dict]:
    """Lista JSON de fuentes (o líneas «red[:modelo]» en formato antiguo)."""
    raw = (text or "").strip()
    if not raw:
        return []
    items = []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError:
            raise DriverError("Redes: la lista enviada no es válida.") from None
        if not isinstance(data, list):
            raise DriverError("Redes: la lista enviada no es válida.")
        for item in data:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", ""))
            model = str(item.get("model", "virtio"))
            kind, _, name = source.partition(":")
            if kind not in ("network", "bridge") or not name:
                raise DriverError("Redes: elige una red o un bridge válido.")
            if kind == "network" and name not in networks:
                raise DriverError(f"Red «{name}»: no existe esa red de libvirt.")
            if kind == "bridge" and name not in bridges:
                raise DriverError(f"Bridge «{name}»: no existe en el host.")
            if model not in NETWORK_MODELS:
                raise DriverError(f"Red «{name}»: modelo no válido "
                                  f"({', '.join(NETWORK_MODELS)})")
            items.append({"type": kind, "name": name, "model": model})
        return items

    # Compatibilidad con el formato de texto anterior: red[:modelo].
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, model = line.partition(":")
        model = model or "virtio"
        if name not in networks:
            raise DriverError(f"Red «{name}»: no existe esa red de libvirt")
        if model not in NETWORK_MODELS:
            raise DriverError(f"Red «{name}»: modelo no válido "
                              f"({', '.join(NETWORK_MODELS)})")
        items.append({"type": "network", "name": name, "model": model})
    return items

class KvmDriver(Driver):
    kind = "kvm"
    label = "KVM"

    def __init__(self, uri: str = "qemu:///system"):
        self.uri = uri

    def _conn(self) -> libvirt.virConnect:
        try:
            return libvirt.open(self.uri)
        except libvirt.libvirtError as e:
            raise DriverError(f"No se puede conectar a {self.uri}: {e}") from e

    def _domain(self, conn, machine_id: str) -> libvirt.virDomain:
        try:
            return conn.lookupByUUIDString(machine_id)
        except libvirt.libvirtError as e:
            raise DriverError(f"No existe la VM {machine_id}") from e

    def list(self) -> list[Machine]:
        conn = self._conn()
        try:
            reserved = self._reservation_map(conn)
            machines = []
            for dom in conn.listAllDomains():
                machine = self._summary(dom)
                if not machine.ips:
                    machine.ips = self._reserved_ips(dom, reserved)
                machines.append(machine)
            return sorted(machines, key=lambda m: m.name)
        finally:
            conn.close()

    @staticmethod
    def _reservation_map(conn) -> dict[str, str]:
        """MAC → IP de las reservas DHCP de todas las redes."""
        reserved = {}
        for network in conn.listAllNetworks():
            try:
                root = ET.fromstring(network.XMLDesc())
            except libvirt.libvirtError:
                continue
            for host in root.findall("ip/dhcp/host"):
                mac, ip = (host.get("mac") or "").lower(), host.get("ip")
                if mac and ip:
                    reserved[mac] = ip
        return reserved

    @staticmethod
    def _reserved_ips(dom, reserved: dict[str, str]) -> list[str]:
        try:
            root = ET.fromstring(dom.XMLDesc(XML_FLAGS))
        except libvirt.libvirtError:
            return []
        ips = []
        for iface in root.findall("devices/interface"):
            mac = iface.find("mac")
            if mac is not None and mac.get("address", "").lower() in reserved:
                ips.append(reserved[mac.get("address").lower()])
        return ips

    def _summary(self, dom) -> Machine:
        state, max_mem_kib, _, vcpus, _ = dom.info()
        m = Machine(kind=self.kind, id=dom.UUIDString(), name=dom.name(),
                    state=STATES.get(state, "unknown"),
                    cpus=str(vcpus), memory=human_bytes(max_mem_kib * 1024),
                    memory_mib=max_mem_kib // 1024, vcpus=float(vcpus))
        if m.state == "running":
            m.ips = self._ips(dom)
        return m

    def _ips(self, dom) -> list[str]:
        # Primero el agente (ve todas las interfaces); si no, las concesiones DHCP
        sources = [libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT,
                   libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE,
                   libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP]
        for src in sources:
            try:
                ifaces = dom.interfaceAddresses(src)
            except libvirt.libvirtError:
                continue
            ips = [a["addr"] for name, i in ifaces.items() if name != "lo"
                   for a in (i.get("addrs") or [])
                   if a["type"] == libvirt.VIR_IP_ADDR_TYPE_IPV4]
            if ips:
                return ips
        return []

    def get(self, machine_id: str) -> Machine:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            m = self._summary(dom)
            xml = dom.XMLDesc()
            m.raw_config, m.raw_label = xml, "XML de libvirt"
            root = ET.fromstring(xml)

            os_type = root.find("os/type")
            cpu = root.find("cpu")
            m.sections["General"] = [
                ("UUID", m.id),
                ("Arranque automático", "sí" if dom.autostart() else "no"),
                ("Persistente", "sí" if dom.isPersistent() else "no"),
                ("Arquitectura", os_type.get("arch", "-") if os_type is not None else "-"),
                ("Máquina", os_type.get("machine", "-") if os_type is not None else "-"),
                ("Modo CPU", cpu.get("mode", "-") if cpu is not None else "-"),
            ]
            m.sections["Recursos"] = [
                ("vCPU", m.cpus),
                ("Memoria máxima", m.memory),
                ("IPs", ", ".join(m.ips) or "-"),
            ]
            m.sections["Discos"] = [
                (f"{d.find('target').get('dev')} ({d.get('device')})",
                 self._disk_source(d))
                for d in root.findall("devices/disk") if d.find("target") is not None
            ]
            m.sections["Red"] = [
                (i.find("mac").get("address") if i.find("mac") is not None else "-",
                 self._iface_desc(i))
                for i in root.findall("devices/interface")
            ]
            m.sections["Pantalla"] = [
                (g.get("type"), f"puerto {g.get('port', 'auto')}")
                for g in root.findall("devices/graphics")
            ]
            graphics = root.find("devices/graphics")
            m.display = graphics.get("type") if graphics is not None else ""
            return m
        finally:
            conn.close()

    def usage(self, machine_id: str) -> Usage:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            info = dom.info()
            state = STATES.get(info[0], "unknown")
            if state != "running":
                return Usage(state=state)
            stats = {}
            try:
                stats = dom.memoryStats()
            except libvirt.libvirtError:
                pass
            # libvirt devuelve memoriaStats y dom.info() en KiB, no en bytes.
            mem_kib = stats.get("rss") or stats.get("actual") or info[2]
            return Usage(state=state, cpu_time=info[4] / 1e9,
                         mem_used=int(mem_kib) * 1024, mem_limit=info[1] * 1024)
        finally:
            conn.close()

    @staticmethod
    def _disk_source(disk) -> str:
        src = disk.find("source")
        if src is None:
            return "(vacío)"
        path = src.get("file") or src.get("dev") or src.get("volume") or src.get("name") or "?"
        drv = disk.find("driver")
        fmt = f" [{drv.get('type')}]" if drv is not None and drv.get("type") else ""
        return path + fmt

    @staticmethod
    def _iface_desc(iface) -> str:
        src = iface.find("source")
        where = "-"
        if src is not None:
            where = src.get("network") or src.get("bridge") or src.get("dev") or "-"
        model = iface.find("model")
        model_s = f", modelo {model.get('type')}" if model is not None else ""
        return f"{iface.get('type')} → {where}{model_s}"

    def users(self, machine_id: str) -> list[User]:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            if STATES.get(dom.info()[0]) != "running":
                raise DriverError("La VM está apagada: arráncala para leer sus usuarios "
                                  "(necesita qemu-guest-agent dentro).")
            return parse_passwd(self._agent_read_file(dom, "/etc/passwd"))
        finally:
            conn.close()

    @staticmethod
    def _agent(dom, execute: str, **arguments):
        """Ejecuta un comando de qemu-guest-agent y devuelve su "return"."""
        if libvirt_qemu is None:
            raise DriverError("Falta el módulo libvirt_qemu.")
        payload = {"execute": execute}
        if arguments:
            payload["arguments"] = arguments
        try:
            out = libvirt_qemu.qemuAgentCommand(dom, json.dumps(payload), 5, 0)
        except libvirt.libvirtError as e:
            raise DriverError("No responde qemu-guest-agent dentro de la VM "
                              f"(¿está instalado y arrancado?): {e}") from e
        return json.loads(out)["return"]

    def _agent_read_file(self, dom, path: str) -> str:
        handle = self._agent(dom, "guest-file-open", path=path, mode="r")
        try:
            data = self._agent(dom, "guest-file-read", handle=handle, count=1024 * 1024)
            return base64.b64decode(data["buf-b64"]).decode(errors="replace")
        finally:
            self._agent(dom, "guest-file-close", handle=handle)

    def _guest_exec(self, dom, path: str, args: list[str], timeout: int = 90) -> tuple[int, str]:
        """Ejecuta un programa dentro de la VM y devuelve (código, salida)."""
        pid = self._agent(dom, "guest-exec", path=path, arg=args,
                          **{"capture-output": True})["pid"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._agent(dom, "guest-exec-status", pid=pid)
            if status.get("exited"):
                out = "".join(base64.b64decode(status.get(k, "")).decode(errors="replace")
                              for k in ("out-data", "err-data"))
                return status.get("exitcode", -1), out
            time.sleep(0.5)
        raise DriverError("El comando dentro de la VM tardó demasiado.")

    # --- Modificar -------------------------------------------------------

    @staticmethod
    def _resizable_disks(dom) -> list[tuple[str, str, int]]:
        """(target, ruta, capacidad en bytes) de los discos de fichero/bloque."""
        root = ET.fromstring(dom.XMLDesc(XML_FLAGS))
        disks = []
        for d in root.findall("devices/disk[@device='disk']"):
            src, target = d.find("source"), d.find("target")
            path = src is not None and (src.get("file") or src.get("dev"))
            if not path or target is None:
                continue
            try:
                capacity = dom.blockInfo(target.get("dev"))[0]
            except libvirt.libvirtError:
                continue  # p. ej. el fichero no existe
            disks.append((target.get("dev"), path, capacity))
        return disks

    def disks(self, machine_id: str) -> list[Disk]:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            root = ET.fromstring(dom.XMLDesc(XML_FLAGS))
            active = dom.isActive()
            result = []
            for d in root.findall("devices/disk[@device='disk']"):
                src, target = d.find("source"), d.find("target")
                if target is None:
                    continue
                path = src.get("file") or src.get("dev") if src is not None else ""
                if not path and src is not None and src.get("volume"):
                    try:
                        path = conn.storagePoolLookupByName(src.get("pool")) \
                            .storageVolLookupByName(src.get("volume")).path()
                    except libvirt.libvirtError:
                        pass
                drv = d.find("driver")
                size = 0
                if active:
                    try:
                        size = dom.blockInfo(target.get("dev"))[0]
                    except libvirt.libvirtError:
                        pass
                if not size and path:
                    try:
                        size = conn.storageVolLookupByPath(path).info()[1]
                    except libvirt.libvirtError:
                        try:
                            size = os.path.getsize(path)
                        except OSError:
                            pass
                result.append(Disk(target=target.get("dev", "?"), path=path or "(vacío)",
                                   size=size, format=drv.get("type", "") if drv is not None else "",
                                   bus=target.get("bus", "")))
            return result
        finally:
            conn.close()

    @staticmethod
    def _next_disk_target(root: ET.Element, bus: str) -> str:
        used = {el.get("dev") for el in root.findall("devices/disk/target")}
        prefix = "vd" if bus == "virtio" else "sd"
        for code in range(ord("a"), ord("z") + 1):
            target = prefix + chr(code)
            if target not in used:
                return target
        raise DriverError("No quedan letras libres para otro disco (máximo 26).")

    def disk_fields(self, machine_id: str) -> list[Field]:
        conn = self._conn()
        try:
            pools = self._pool_names(conn)
        finally:
            conn.close()
        default = "default" if "default" in pools else (pools[0] if pools else "")
        return [
            Field("size_gib", "Tamaño (GiB)", "20", "number", min=1),
            Field("pool", "Pool de almacenamiento", default, "select",
                  options=[(p, p) for p in pools],
                  help="El volumen se crea en este pool; debe estar activo."),
            Field("format", "Formato", "qcow2", "select", options=[
                ("qcow2", "qcow2 (recomendado)"), ("raw", "raw")]),
            Field("bus", "Bus", "virtio", "select", options=[
                ("virtio", "virtio (recomendado)"), ("scsi", "SCSI"), ("sata", "SATA")]),
        ]

    def add_disk(self, machine_id: str, values: dict[str, str]) -> list[str]:
        size = int(parse_number(values, "size_gib", "Tamaño", minimum=1))
        pool_name = values.get("pool", "")
        fmt = values.get("format", "qcow2")
        bus = values.get("bus", "virtio")
        if fmt not in DISK_FORMATS:
            raise DriverError("Formato de disco no válido.")
        if bus not in ("virtio", "scsi", "sata"):
            raise DriverError("Bus de disco no válido.")
        conn = self._conn()
        vol = None
        try:
            dom = self._domain(conn, machine_id)
            try:
                pool = conn.storagePoolLookupByName(pool_name)
            except libvirt.libvirtError as e:
                raise DriverError(f"No existe el pool «{pool_name}»") from e
            if not pool.isActive():
                raise DriverError(f"El pool «{pool_name}» no está activo.")
            pool.refresh(0)
            root = ET.fromstring(dom.XMLDesc(XML_FLAGS))
            target_dev = self._next_disk_target(root, bus)

            existing = {v.name() for v in pool.listAllVolumes()}
            index = len(root.findall("devices/disk[@device='disk']")) + 1
            name = f"{dom.name()}-disco{index}.{fmt}"
            while name in existing:
                index += 1
                name = f"{dom.name()}-disco{index}.{fmt}"

            volume_xml = ET.Element("volume")
            ET.SubElement(volume_xml, "name").text = name
            ET.SubElement(volume_xml, "capacity", unit="GiB").text = str(size)
            ET.SubElement(volume_xml, "allocation", unit="GiB").text = "0"
            target_el = ET.SubElement(volume_xml, "target")
            ET.SubElement(target_el, "format", type=fmt)
            vol = pool.createXML(ET.tostring(volume_xml, encoding="unicode"), 0)
            path = vol.path()
            try:
                block = vol.info()[0] == 1
            except libvirt.libvirtError:
                block = False

            disk = ET.Element("disk", type="block" if block else "file", device="disk")
            ET.SubElement(disk, "driver", name="qemu", type=fmt)
            ET.SubElement(disk, "source", **({"dev": path} if block else {"file": path}))
            ET.SubElement(disk, "target", dev=target_dev, bus=bus)
            xml = ET.tostring(disk, encoding="unicode")
            note = "."
            if dom.isActive():
                try:
                    dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG
                                          | libvirt.VIR_DOMAIN_AFFECT_LIVE)
                    note = ", conectado en caliente."
                except libvirt.libvirtError:
                    dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG)
                    note = (": la VM no admite añadirlo en caliente; aparecerá al "
                            "apagarla y volver a arrancarla.")
            else:
                dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG)
            return [f"Disco {target_dev} añadido: {path} ({size} GiB){note}"]
        except libvirt.libvirtError as e:
            if vol is not None:
                try:
                    vol.delete(0)
                except libvirt.libvirtError:
                    pass
            raise DriverError(str(e)) from e
        finally:
            conn.close()

    def editable(self, machine_id: str) -> list[Field]:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            root = ET.fromstring(dom.XMLDesc(XML_FLAGS))
            host_cpus = conn.getInfo()[2]
            memory_kib = int(root.findtext("memory"))
            fields = [
                Field("vcpus", "vCPU", root.findtext("vcpu"), "number", min=1, step=1,
                      help=f"El host tiene {host_cpus} CPUs."),
                Field("memory_mib", "Memoria (MiB)", str(memory_kib // 1024), "number",
                      min=64, help="1 GiB = 1024 MiB"),
                Field("autostart", "Arrancar con el host", "1" if dom.autostart() else "",
                      "checkbox"),
            ]
            for target, path, capacity in self._resizable_disks(dom):
                fields.append(Field(
                    f"disk_{target}", f"Disco {target} (GiB)", f"{capacity / GIB:g}", "number",
                    min=round(capacity / GIB, 2),
                    help=f"{path} · solo se puede ampliar"))
            return fields
        finally:
            conn.close()

    def update(self, machine_id: str, values: dict[str, str]) -> list[str]:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            running = dom.isActive()
            root = _parse_keep_ns(dom.XMLDesc(XML_FLAGS))
            notes = []

            # 1. Validar todo antes de tocar nada
            vcpus = parse_number(values, "vcpus", "vCPU", minimum=1)
            memory_kib = parse_number(values, "memory_mib", "Memoria", minimum=64) * 1024
            if root.find("cpu/numa") is not None and memory_kib != int(root.findtext("memory")):
                raise DriverError("Esta VM tiene NUMA configurado: cambia la memoria con "
                                  "virsh edit para ajustar también las celdas.")
            grow = []
            for target, path, capacity in self._resizable_disks(dom):
                key = f"disk_{target}"
                if key not in values:
                    continue
                new = int(parse_number(values, key, f"Disco {target}", integer=False) * GIB)
                if new < capacity - 1024 ** 2:
                    raise DriverError(f"Disco {target}: no se puede reducir un disco.")
                if new > capacity + 1024 ** 2:
                    grow.append((target, path, new))

            # 2. CPU y memoria: se reescribe la config persistente
            vcpu_el = root.find("vcpu")
            if int(vcpu_el.text) != vcpus or int(root.findtext("memory")) != memory_kib:
                vcpu_el.text = str(vcpus)
                vcpu_el.attrib.pop("current", None)
                for tag in ("memory", "currentMemory"):
                    el = root.find(tag)
                    if el is None:
                        el = ET.SubElement(root, tag)
                    el.text, el.attrib = str(memory_kib), {"unit": "KiB"}
                conn.defineXML(ET.tostring(root, encoding="unicode"))
                notes.append("CPU/memoria guardadas" +
                             (": se aplicarán en el próximo arranque." if running else "."))

            # 3. Arranque automático
            autostart = "autostart" in values
            if bool(dom.autostart()) != autostart:
                dom.setAutostart(int(autostart))
                notes.append("Arranque automático " + ("activado." if autostart else
                                                       "desactivado."))

            # 4. Ampliar discos
            for target, path, new in grow:
                if running:
                    dom.blockResize(target, new, libvirt.VIR_DOMAIN_BLOCK_RESIZE_BYTES)
                else:
                    conn.storageVolLookupByPath(path).resize(new)
                notes.append(f"Disco {target} ampliado a {new / GIB:g} GiB: amplía también "
                             "la partición dentro de la VM (growpart + resize2fs).")
            return notes
        except libvirt.libvirtError as e:
            raise DriverError(str(e)) from e
        finally:
            conn.close()

    # --- Crear -----------------------------------------------------------

    @staticmethod
    def _pool_names(conn) -> list[str]:
        try:
            pools = conn.listAllStoragePools(libvirt.VIR_CONNECT_LIST_STORAGE_POOLS_ACTIVE)
        except libvirt.libvirtError:
            return []
        return sorted(pool.name() for pool in pools)

    def _isos(self, conn) -> list[tuple[str, str]]:
        """(ruta, nombre) de las ISOs de todos los pools activos."""
        isos = []
        for pool in conn.listAllStoragePools(libvirt.VIR_CONNECT_LIST_STORAGE_POOLS_ACTIVE):
            try:
                pool.refresh(0)
            except libvirt.libvirtError:
                pass
            for vol in pool.listAllVolumes():
                if vol.name().lower().endswith(".iso"):
                    isos.append((vol.path(), f"{vol.name()} ({pool.name()})"))
        return sorted(isos, key=lambda i: i[1].lower())

    def _disk_images(self, conn) -> list[tuple[str, str]]:
        """(ruta, nombre) de las imágenes qcow2 de todos los pools activos."""
        images = []
        for pool in conn.listAllStoragePools(libvirt.VIR_CONNECT_LIST_STORAGE_POOLS_ACTIVE):
            try:
                pool.refresh(0)
            except libvirt.libvirtError:
                pass
            for vol in pool.listAllVolumes():
                if not vol.name().lower().endswith(".qcow2"):
                    continue
                try:
                    size = vol.info()[1]
                except libvirt.libvirtError:
                    size = 0
                images.append((vol.path(), f"{vol.name()} ({pool.name()}, {human_bytes(size)})"))
        return sorted(images, key=lambda item: item[1].lower())

    @staticmethod
    @functools.cache
    def _osinfo_ids() -> list[str]:
        try:
            out = subprocess.run(["osinfo-query", "os", "-f", "short-id"],
                                 capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        return sorted(line.strip() for line in out.splitlines()[2:] if line.strip())

    def network_choices(self) -> tuple[list[dict], str]:
        """Opciones para el desplegable de redes del alta y la red por defecto."""
        conn = self._conn()
        try:
            networks = sorted(n.name() for n in conn.listAllNetworks())
        finally:
            conn.close()
        bridges = host_bridges()
        options = [{"value": f"network:{name}", "label": f"Red {name}"} for name in networks]
        options += [{"value": f"bridge:{name}", "label": f"Bridge {name}"}
                    for name in bridges if name not in networks]
        default = (f"network:default" if "default" in networks else
                   (options[0]["value"] if options else ""))
        return options, default

    def create_fields(self) -> list[Field]:
        conn = self._conn()
        try:
            isos = self._isos(conn)
            origins = [(f"iso:{path}", f"ISO · {label}") for path, label in isos]
            origins += [(f"image:{path}", f"Imagen · {label}")
                        for path, label in self._disk_images(conn)]
            origins += [(f"clone:{d.name()}", f"Clonar · {d.name()}")
                        for d in sorted(conn.listAllDomains(), key=lambda d: d.name())]
            pools = self._pool_names(conn)
        finally:
            conn.close()
        default_pool = "default" if "default" in pools else (pools[0] if pools else "default")
        return [
            Field("name", "Nombre", "", required=True, placeholder="srv-nuevo"),
            Field("origin", "Origen", "", "select", options=origins,
                  help="Instalar desde una ISO, arrancar una imagen qcow2 (cloud) o clonar "
                       "una VM existente (debe estar apagada). Al clonar se conservan sus "
                       "discos y redes."),
            Field("cloud_init", "Cloud-init (YAML)", "", "textarea", rows=12,
                  placeholder="#cloud-config\nusers:\n  - name: usuario\n    ssh_authorized_keys:\n      - ssh-ed25519 AAAA...\n",
                  help="Pega aquí el cloud-config (se le añade #cloud-config si falta). "
                       "Hangar genera la ISO seed y la conecta como CD-ROM; solo se usa "
                       "al crear desde una imagen qcow2."),
            Field("osinfo", "Sistema operativo (osinfo)", "", suggestions=self._osinfo_ids(),
                  placeholder="autodetectar",
                  help="Vacío = detectarlo de la ISO. Para Windows pon p. ej. win11."),
            Field("vcpus", "vCPU", "2", "number", min=1, step=1),
            Field("memory_mib", "Memoria (MiB)", "2048", "number", min=64),
            Field("disks", "Discos (uno por línea)", f"20:{default_pool}:qcow2", "textarea",
                  placeholder="20:default:qcow2\n50:default:qcow2",
                  help="tamaño_GiB[:pool[:formato]]. La primera línea es el disco del sistema. "
                       f"Pools activos: {', '.join(pools) or 'ninguno'}. Formatos: qcow2, raw."),
        ]

    def prepare_create(self, values: dict[str, str]) -> dict:
        conn = self._conn()
        try:
            existing = [d.name() for d in conn.listAllDomains()]
            networks = {n.name() for n in conn.listAllNetworks()}
            bridges = set(host_bridges())
            pools = set(self._pool_names(conn))
            default_pool = "default" if "default" in pools else (sorted(pools)[0] if pools else "default")
            isos = [path for path, _ in self._isos(conn)]
            images = [path for path, _ in self._disk_images(conn)]
            spec = {
                "name": check_name(values, existing),
                "vcpus": parse_number(values, "vcpus", "vCPU", minimum=1),
                "memory_mib": parse_number(values, "memory_mib", "Memoria", minimum=64),
                "osinfo": values.get("osinfo", "").strip(),
            }
            if spec["osinfo"] and spec["osinfo"] not in self._osinfo_ids():
                raise DriverError(f"Sistema «{spec['osinfo']}» desconocido para osinfo.")

            origin = values.get("origin", "")
            kind, _, target = origin.partition(":")
            if kind == "iso" and target in isos:
                spec["iso"] = target
                spec["disks"] = _parse_disks(values.get("disks", ""), pools, default_pool)
                spec["networks"] = _parse_networks(values.get("networks", ""), networks, bridges)
            elif kind == "image" and target in images:
                spec["image"] = target
                spec["disks"] = _parse_disks(values.get("disks", ""), pools, default_pool)
                spec["networks"] = _parse_networks(values.get("networks", ""), networks, bridges)
                spec["cloud_init"] = values.get("cloud_init", "").strip()
            elif kind == "clone" and target in existing:
                if conn.lookupByName(target).isActive():
                    raise DriverError(f"Apaga «{target}» antes de clonarla.")
                # El clon conserva discos y redes de la original.
                spec["clone"] = target
                spec["disks"] = []
                spec["networks"] = []
            else:
                raise DriverError("Elige una ISO, una imagen qcow2 o una VM para clonar.")
            return spec
        finally:
            conn.close()

    def _make_cloud_init(self, pool, name: str, config: str, log) -> str:
        """Genera una ISO NoCloud con user-data/meta-data y la sube al pool."""
        config = (config or "").strip()
        if not config:
            return ""
        if not config.startswith(("#cloud-config", "#!")):
            config = "#cloud-config\n" + config
        tool = next((t for t in ("genisoimage", "mkisofs") if shutil.which(t)), None)
        if tool is None:
            raise DriverError("Falta genisoimage/mkisofs: sudo apt install genisoimage")

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "user-data").write_text(config + "\n", encoding="utf-8")
            (folder / "meta-data").write_text(
                f"instance-id: hangar-{name}-{datetime.now():%Y%m%d%H%M%S}\n"
                f"local-hostname: {name}\n", encoding="utf-8")
            iso = folder / "seed.iso"
            proc = subprocess.run([
                tool, "-quiet", "-output", str(iso), "-volid", "cidata",
                "-joliet", "-rock", "-input-charset", "utf-8",
                str(folder / "user-data"), str(folder / "meta-data"),
            ], capture_output=True, text=True)
            if proc.returncode != 0 or not iso.exists():
                raise DriverError(f"No se pudo generar la ISO cloud-init: "
                                  f"{proc.stderr.strip() or 'error de genisoimage'}")

            existing = {vol.name() for vol in pool.listAllVolumes()}
            vol_name = f"{name}-seed.iso"
            index = 2
            while vol_name in existing:
                vol_name = f"{name}-seed-{index}.iso"
                index += 1
            volume_xml = ET.Element("volume")
            ET.SubElement(volume_xml, "name").text = vol_name
            ET.SubElement(volume_xml, "capacity", unit="bytes").text = str(iso.stat().st_size)
            ET.SubElement(volume_xml, "allocation", unit="bytes").text = str(iso.stat().st_size)
            ET.SubElement(ET.SubElement(volume_xml, "target"), "format", type="raw")
            volume = None
            try:
                volume = pool.createXML(ET.tostring(volume_xml, encoding="unicode"), 0)
                upload = subprocess.run([
                    "virsh", "-c", self.uri, "vol-upload", vol_name, str(iso), pool.name(),
                ], capture_output=True, text=True)
                if upload.returncode != 0:
                    raise DriverError(f"No se pudo subir la ISO cloud-init al pool: "
                                      f"{upload.stderr.strip() or 'error de virsh'}")
                log(f"Cloud-init: ISO generada en {volume.path()}")
                return volume.path()
            except Exception:
                if volume is not None:
                    try:
                        volume.delete(0)
                    except libvirt.libvirtError:
                        pass
                raise

    def _create_from_image(self, spec: dict, log) -> str:
        """Crea una VM desde una imagen qcow2 (cloud) con cloud-init opcional."""
        base = Path(spec["image"])
        if not base.is_file():
            raise DriverError(f"No existe la imagen base: {base}")

        conn = self._conn()
        target_vol = None
        seed = ""
        try:
            try:
                base_vol = conn.storageVolLookupByPath(str(base))
            except libvirt.libvirtError as e:
                raise DriverError(f"La imagen base no está en un pool de libvirt: {base}") from e
            base_bytes = base_vol.info()[1]
            pool = base_vol.storagePoolLookupByVolume()

            size = int(spec["disks"][0]["size_gib"])
            minimum = (base_bytes + GIB - 1) // GIB
            if base_bytes and size < minimum:
                raise DriverError(f"El primer disco debe ser de al menos {minimum} GiB "
                                  f"(la imagen base ocupa {human_bytes(base_bytes)}).")

            existing = {vol.name() for vol in pool.listAllVolumes()}
            name = f"{spec['name']}.qcow2"
            index = 2
            while name in existing:
                name = f"{spec['name']}-{index}.qcow2"
                index += 1

            volume_xml = ET.Element("volume")
            ET.SubElement(volume_xml, "name").text = name
            ET.SubElement(volume_xml, "capacity", unit="GiB").text = str(size)
            ET.SubElement(volume_xml, "allocation", unit="GiB").text = "0"
            ET.SubElement(ET.SubElement(volume_xml, "target"), "format", type="qcow2")
            backing = ET.SubElement(volume_xml, "backingStore")
            ET.SubElement(backing, "path").text = str(base)
            ET.SubElement(backing, "format", type="qcow2")
            target_vol = pool.createXML(ET.tostring(volume_xml, encoding="unicode"), 0)
            if target_vol.info()[1] != size * GIB:
                target_vol.resize(size * GIB)
            target = target_vol.path()
            log(f"Disco diferencial creado en {target} sobre {base}")
            if spec.get("cloud_init"):
                seed = self._make_cloud_init(pool, spec["name"], spec["cloud_init"], log)
        except libvirt.libvirtError as e:
            if target_vol is not None:
                try:
                    target_vol.delete(0)
                except libvirt.libvirtError:
                    pass
            raise DriverError(str(e)) from e
        finally:
            conn.close()

        osinfo = f"name={spec['osinfo']}" if spec["osinfo"] else "detect=on,require=off"
        command = [
            "virt-install", "--connect", self.uri, "--name", spec["name"],
            "--vcpus", str(spec["vcpus"]), "--memory", str(spec["memory_mib"]),
            "--disk", f"path={target},format=qcow2",
        ]
        for disk in spec["disks"][1:]:
            command += ["--disk", f"pool={disk['pool']},size={disk['size_gib']},"
                                  f"format={disk['format']}"]
        if seed:
            command += ["--disk", f"path={seed},device=cdrom"]
        command += ["--import", "--osinfo", osinfo]
        for network in spec["networks"]:
            command += ["--network", f"{network['type']}={network['name']},model={network['model']}"]
        command += ["--channel", "unix,target_type=virtio,name=org.qemu.guest_agent.0"]
        command += ["--graphics", "spice", "--noautoconsole"]
        run_streaming(command, log)
        log("VM creada desde la imagen; si cloud-init está bien configurado, "
            "aplicará usuario, claves y red al arrancar.")
        conn = self._conn()
        try:
            return conn.lookupByName(spec["name"]).UUIDString()
        finally:
            conn.close()

    def create(self, spec: dict, log) -> str:
        name = spec["name"]
        if "image" in spec:
            return self._create_from_image(spec, log)
        if "clone" in spec:
            run_streaming(["virt-clone", "--connect", self.uri, "--original", spec["clone"],
                           "--name", name, "--auto-clone"], log)
            log("Se conservan los discos y las redes de la VM original; "
                "puedes añadir más desde las pestañas Discos y Red.")
            conn = self._conn()
            try:
                uuid = conn.lookupByName(name).UUIDString()
            finally:
                conn.close()
            for note in self.update(uuid, {"vcpus": str(spec["vcpus"]),
                                           "memory_mib": str(spec["memory_mib"])}):
                log(note)
            for note in self.enable_agent(uuid):
                log(note)
            return uuid

        osinfo = f"name={spec['osinfo']}" if spec["osinfo"] else "detect=on,require=off"
        command = [
            "virt-install", "--connect", self.uri, "--name", name,
            "--vcpus", str(spec["vcpus"]), "--memory", str(spec["memory_mib"]),
        ]
        for disk in spec["disks"]:
            # Sin forzar bus: virt-install elige según osinfo
            # (virtio para Linux, SATA/e1000e para Windows…)
            command += ["--disk", f"pool={disk['pool']},size={disk['size_gib']},"
                                  f"format={disk['format']}"]
        command += ["--cdrom", spec["iso"], "--osinfo", osinfo]
        for network in spec["networks"]:
            command += ["--network", f"{network['type']}={network['name']},model={network['model']}"]
        command += ["--channel", "unix,target_type=virtio,name=org.qemu.guest_agent.0"]
        command += ["--graphics", "spice", "--noautoconsole"]
        run_streaming(command, log)
        log("VM creada y arrancada desde la ISO: conéctate con virt-manager o "
            "virt-viewer para hacer la instalación.")
        conn = self._conn()
        try:
            return conn.lookupByName(name).UUIDString()
        finally:
            conn.close()

    # --- Consola y pantalla ----------------------------------------------

    console_hint = ("Consola serie de la VM. Si no ves nada, pulsa Enter. Si sigue en negro, "
                    "el sistema no tiene un login en el puerto serie: pulsa «Activar consola serie» "
                    "(o usa «Pantalla»).")

    def console_command(self, machine_id: str) -> list[str]:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            if not dom.isActive():
                raise DriverError("Arranca la VM para abrir su consola.")
            return ["virsh", "-c", self.uri, "console", dom.UUIDString(), "--force"]
        finally:
            conn.close()

    can_enable_serial = True

    def enable_serial_console(self, machine_id: str, log) -> None:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            if not dom.isActive():
                raise DriverError("Arranca la VM: se activa desde dentro con qemu-guest-agent.")
            root = ET.fromstring(dom.XMLDesc())
            if root.find("devices/serial") is None:
                raise DriverError("La VM no tiene puerto serie. Añádelo con virt-manager "
                                  "(Añadir hardware → Serie) y reiníciala.")
            if not self._has_agent(root):
                raise DriverError("La VM no tiene qemu-guest-agent: entra por «Pantalla» y "
                                  "ejecuta systemctl enable --now serial-getty@ttyS0")
            try:
                os_id = self._agent(dom, "guest-get-osinfo").get("id", "")
            except DriverError:
                os_id = ""
            if os_id == "mswindows":
                raise DriverError("En Windows la consola serie no da un login: usa «Pantalla».")
            log("Activando el login en el puerto serie dentro de la VM…")
            code, out = self._guest_exec(dom, "/bin/sh", ["-c", GUEST_ENABLE_SERIAL])
            log(out.strip())
            if code != 0:
                raise DriverError(f"No se pudo activar (código {code}).")
            log("Listo: vuelve a la consola y pulsa Enter.")
        finally:
            conn.close()

    def open_display(self, machine_id: str) -> None:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            if ET.fromstring(dom.XMLDesc()).find("devices/graphics") is None:
                raise DriverError("Esta VM no tiene pantalla gráfica: usa la consola.")
            uuid = dom.UUIDString()
        finally:
            conn.close()
        if not shutil.which("virt-viewer"):
            raise DriverError("Falta virt-viewer: sudo apt install virt-viewer")
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise DriverError("Hangar no tiene acceso a tu escritorio (falta DISPLAY): "
                              "arráncalo desde una terminal de tu sesión gráfica.")
        # --attach usa libvirt para llegar a la pantalla; --wait espera si está apagada
        subprocess.Popen(["virt-viewer", "--connect", self.uri, "--attach", "--wait",
                          "--reconnect", uuid], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

    # --- Red ---------------------------------------------------------------

    @staticmethod
    def _net_ipv4(net_root: ET.Element) -> tuple[ET.Element | None, ipaddress.IPv4Network | None]:
        """El <ip> IPv4 de una red libvirt y su subred."""
        for ip in net_root.findall("ip"):
            if ip.get("family", "ipv4") == "ipv4" and ip.get("address"):
                mask = ip.get("prefix") or ip.get("netmask") or "24"
                return ip, ipaddress.IPv4Network(f"{ip.get('address')}/{mask}", strict=False)
        return None, None

    def _live_addresses(self, dom) -> dict[str, list[str]]:
        """MAC → IPs con prefijo, del agente o de las concesiones DHCP."""
        for src in (libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT,
                    libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE,
                    libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP):
            try:
                ifaces = dom.interfaceAddresses(src)
            except libvirt.libvirtError:
                continue
            found = {(i.get("hwaddr") or "").lower(): [
                f"{a['addr']}/{a['prefix']}" if a.get("prefix") else a["addr"]
                for a in (i.get("addrs") or [])
                if a["type"] == libvirt.VIR_IP_ADDR_TYPE_IPV4]
                for name, i in ifaces.items() if name != "lo"}
            if any(found.values()):
                return found
        return {}

    @staticmethod
    def _has_agent(root: ET.Element) -> bool:
        return any(t.get("name") == "org.qemu.guest_agent.0"
                   for t in root.findall("devices/channel/target"))

    def has_agent(self, machine_id: str) -> bool:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            return self._has_agent(ET.fromstring(dom.XMLDesc(XML_FLAGS)))
        finally:
            conn.close()

    def enable_agent(self, machine_id: str) -> list[str]:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            root = ET.fromstring(dom.XMLDesc(XML_FLAGS))
            if self._has_agent(root):
                return ["La VM ya tiene el canal del agente QEMU."]
            channel = ET.Element("channel", type="unix")
            ET.SubElement(channel, "source", mode="bind")
            ET.SubElement(channel, "target", type="virtio",
                          name="org.qemu.guest_agent.0")
            xml = ET.tostring(channel, encoding="unicode")
            note = "."
            if dom.isActive():
                try:
                    dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG
                                          | libvirt.VIR_DOMAIN_AFFECT_LIVE)
                    note = (" en caliente. Reinicia qemu-guest-agent dentro de la VM "
                            "(o la VM) para que lo use.")
                except libvirt.libvirtError:
                    dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG)
                    note = (": se aplicará al apagar y volver a arrancar la VM.")
            else:
                dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG)
                note = ": se aplicará al arrancar la VM."
            return [f"Canal del agente QEMU añadido{note}"]
        except libvirt.libvirtError as e:
            raise DriverError(str(e)) from e
        finally:
            conn.close()

    def interfaces(self, machine_id: str) -> list[Interface]:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            root = ET.fromstring(dom.XMLDesc(XML_FLAGS))
            running, agent = dom.isActive(), self._has_agent(root)
            live = self._live_addresses(dom) if running else {}
            nets = {n.name(): ET.fromstring(n.XMLDesc()) for n in conn.listAllNetworks()}
            result = []
            for el in root.findall("devices/interface"):
                mac = el.find("mac").get("address").lower()
                src, model = el.find("source"), el.find("model")
                network = src.get("network") if src is not None else None
                where = network or (src.get("bridge") if src is not None else "") or "-"
                iface = Interface(key=mac, mac=mac, model=model.get("type") if model is not None
                                  else "", network=where, ips=live.get(mac, []))
                ip_el = nets[network].find("ip/dhcp") if network in nets else None
                if network in nets and ip_el is not None:
                    reserved = next((h.get("ip") for h in ip_el.findall("host")
                                     if (h.get("mac") or "").lower() == mac), None)
                    iface.dhcp = True
                    iface.defined_ip = reserved or ""
                    iface.ip_config = (f"DHCP de libvirt, reserva fija {reserved}" if reserved
                                       else "DHCP de libvirt")
                    iface.set_ip_hint = (f"Se crea una reserva DHCP en «{network}»"
                                         + (" y se renueva la IP dentro de la VM."
                                            if agent else ": la VM la recibe al renovar su IP "
                                            "(o al reiniciar)."))
                else:
                    iface.ip_config = ("sin DHCP en la red" if network else "bridge del host") + \
                                      ": la IP se configura dentro de la VM"
                    iface.can_set_ip = agent
                    iface.set_ip_hint = ("Se configura dentro de la VM con qemu-guest-agent "
                                         "(debe estar encendida)." if agent else
                                         "Sin qemu-guest-agent: Hangar no puede entrar a "
                                         "cambiarla. Instálalo en la VM o cámbiala a mano.")
                result.append(iface)
            return result
        finally:
            conn.close()

    def interface_fields(self, machine_id: str) -> list[Field]:
        conn = self._conn()
        try:
            nets = sorted(conn.listAllNetworks(), key=lambda n: n.name())
            options = [(f"network:{n.name()}", f"Red {n.name()}"
                        + ("" if n.isActive() else " (inactiva)")) for n in nets]
        finally:
            conn.close()
        options += [(f"bridge:{b}", f"Bridge del host {b}") for b in host_bridges()]
        return [
            Field("source", "Conectar a", "network:default", "select", options=options),
            Field("model", "Modelo de tarjeta", "virtio", "select", options=[
                ("virtio", "virtio (Linux, la más rápida)"),
                ("e1000e", "e1000e (Windows sin drivers virtio)"),
                ("rtl8139", "rtl8139 (sistemas antiguos)")]),
        ]

    def add_interface(self, machine_id: str, values: dict[str, str]) -> list[str]:
        kind, _, name = values.get("source", "").partition(":")
        model = values.get("model", "virtio")
        if kind not in ("network", "bridge") or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) \
                or model not in ("virtio", "e1000e", "rtl8139"):
            raise DriverError("Elige una red y un modelo válidos.")
        el = ET.Element("interface", type=kind)
        ET.SubElement(el, "source", {kind: name})
        ET.SubElement(el, "model", type=model)
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            before = {i.key for i in self.interfaces(machine_id)}
            xml = ET.tostring(el, encoding="unicode")
            when = "."
            if dom.isActive():
                try:
                    dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG
                                          | libvirt.VIR_DOMAIN_AFFECT_LIVE)
                    when = ", ya conectada en caliente."
                except libvirt.libvirtError:
                    # Sin hotplug (sin ACPI o sin puertos PCIe libres): solo config
                    dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG)
                    when = (": la VM no admite añadirla en caliente, aparecerá al "
                            "apagarla y volver a arrancarla.")
            else:
                dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG)
            new = [i.mac for i in self.interfaces(machine_id) if i.key not in before]
            return [f"Interfaz añadida en «{name}»" + (f" (MAC {new[0]})" if new else "") + when]
        except libvirt.libvirtError as e:
            raise DriverError(str(e)) from e
        finally:
            conn.close()

    def remove_interface(self, machine_id: str, key: str) -> list[str]:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            notes = []
            for flag, xml in ((libvirt.VIR_DOMAIN_AFFECT_CONFIG, dom.XMLDesc(XML_FLAGS)),
                              (libvirt.VIR_DOMAIN_AFFECT_LIVE,
                               dom.XMLDesc() if dom.isActive() else None)):
                if xml is None:
                    continue
                el = next((i for i in ET.fromstring(xml).findall("devices/interface")
                           if i.find("mac").get("address").lower() == key.lower()), None)
                if el is None:
                    continue
                try:
                    dom.detachDeviceFlags(ET.tostring(el, encoding="unicode"), flag)
                    notes.append("en caliente" if flag == libvirt.VIR_DOMAIN_AFFECT_LIVE
                                 else "de la configuración")
                except libvirt.libvirtError:
                    if flag != libvirt.VIR_DOMAIN_AFFECT_LIVE:
                        raise
                    notes.append("seguirá conectada hasta que apagues la VM")
            if not notes:
                raise DriverError(f"No existe la interfaz {key}")
            return [f"Interfaz {key} quitada ({', '.join(notes)})."]
        except libvirt.libvirtError as e:
            raise DriverError(str(e)) from e
        finally:
            conn.close()

    def set_ip(self, machine_id: str, key: str, values: dict[str, str], log) -> None:
        mode, address, gateway, dns = parse_ip_request(values)
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            iface = next((i for i in self.interfaces(machine_id) if i.key == key), None)
            if iface is None:
                raise DriverError(f"No existe la interfaz {key}")
            root = ET.fromstring(dom.XMLDesc(XML_FLAGS))
            agent = dom.isActive() and self._has_agent(root)

            if iface.dhcp:
                self._dhcp_reservation(conn, dom, iface, mode, address, log)
                if not agent:
                    log("La VM tomará la IP al renovar su concesión DHCP (o al reiniciarla).")
                    return
                log("Renovando la IP dentro de la VM…")
                mode, address, gateway, dns = "dhcp", "", "", ""
            elif not iface.can_set_ip:
                raise DriverError(iface.set_ip_hint)
            elif not dom.isActive():
                raise DriverError("Arranca la VM: la IP se cambia dentro de ella con el agente.")
            self._set_ip_in_guest(dom, iface.mac, mode, address, gateway, dns, log)
        except libvirt.libvirtError as e:
            raise DriverError(str(e)) from e
        finally:
            conn.close()

    def _dhcp_reservation(self, conn, dom, iface: Interface, mode: str, address: str,
                          log) -> None:
        net = conn.networkLookupByName(iface.network)
        net_root = ET.fromstring(net.XMLDesc())
        ip_el, subnet = self._net_ipv4(net_root)
        hosts = ip_el.findall("dhcp/host")
        mine = next((h for h in hosts if (h.get("mac") or "").lower() == iface.mac), None)
        flags = libvirt.VIR_NETWORK_UPDATE_AFFECT_CONFIG
        if net.isActive():
            flags |= libvirt.VIR_NETWORK_UPDATE_AFFECT_LIVE
        section = libvirt.VIR_NETWORK_SECTION_IP_DHCP_HOST

        if mode == "dhcp":
            if mine is not None:
                net.update(libvirt.VIR_NETWORK_UPDATE_COMMAND_DELETE, section, -1,
                           ET.tostring(mine, encoding="unicode"), flags)
                log(f"Reserva DHCP de {iface.mac} eliminada: tomará una IP libre del rango.")
            return

        ip = ipaddress.ip_interface(address).ip
        if ip not in subnet or ip in (subnet.network_address, subnet.broadcast_address,
                                      ipaddress.IPv4Address(ip_el.get("address"))):
            raise DriverError(f"{ip} no es una IP de equipo válida en {iface.network} ({subnet}).")
        clash = next((h for h in hosts if h.get("ip") == str(ip) and h is not mine), None)
        if clash is not None:
            raise DriverError(f"{ip} ya está reservada para {clash.get('mac')} "
                              f"({clash.get('name') or 'sin nombre'}).")
        host = ET.Element("host", mac=iface.mac, name=dom.name(), ip=str(ip))
        command = (libvirt.VIR_NETWORK_UPDATE_COMMAND_MODIFY if mine is not None
                   else libvirt.VIR_NETWORK_UPDATE_COMMAND_ADD_LAST)
        net.update(command, section, -1, ET.tostring(host, encoding="unicode"), flags)
        log(f"Reserva DHCP en «{iface.network}»: {iface.mac} → {ip}.")

    def _set_ip_in_guest(self, dom, mac: str, mode: str, address: str, gateway: str, dns: str,
                         log) -> None:
        guest = self._agent(dom, "guest-network-get-interfaces")
        name = next((i["name"] for i in guest
                     if (i.get("hardware-address") or "").lower() == mac), None)
        if name is None:
            raise DriverError(f"El agente no ve ninguna interfaz con la MAC {mac} dentro de la VM.")
        try:
            os_id = self._agent(dom, "guest-get-osinfo").get("id", "")
        except DriverError:
            os_id = ""
        log(f"Interfaz dentro de la VM: {name} · "
            + (f"estática {address}" if mode == "static" else "DHCP"))

        if os_id == "mswindows":
            netsh = r"C:\Windows\System32\netsh.exe"
            if mode == "dhcp":
                commands = [["interface", "ipv4", "set", "address", f"name={name}", "source=dhcp"],
                            ["interface", "ipv4", "set", "dnsservers", f"name={name}",
                             "source=dhcp"]]
            else:
                iface = ipaddress.ip_interface(address)
                commands = [["interface", "ipv4", "set", "address", f"name={name}", "static",
                             str(iface.ip), str(iface.netmask), *([gateway] if gateway else [])]]
                for n, server in enumerate(dns.split()):
                    commands.append(["interface", "ipv4", "add" if n else "set", "dnsservers",
                                     f"name={name}", *([] if n else ["static"]), server,
                                     *(["index=" + str(n + 1)] if n else ["primary"]),
                                     "validate=no"])
            for args in commands:
                code, out = self._guest_exec(dom, netsh, args)
                log(out.strip() or f"netsh {' '.join(args)}")
                if code != 0:
                    raise DriverError(f"netsh falló (código {code}).")
            return

        code, out = self._guest_exec(dom, "/bin/sh", ["-c", GUEST_SET_IP, "sh", name, mode,
                                                      address, gateway, dns])
        log(out.strip())
        if code != 0:
            raise DriverError(f"No se pudo cambiar la IP dentro de la VM (código {code}).")

    # --- Snapshots -------------------------------------------------------

    def _snapshot(self, dom, name: str) -> libvirt.virDomainSnapshot:
        try:
            return dom.snapshotLookupByName(name)
        except libvirt.libvirtError as e:
            raise DriverError(f"No existe el snapshot «{name}»") from e

    def snapshots(self, machine_id: str) -> list[Snapshot]:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            current = dom.snapshotCurrent().getName() if dom.hasCurrentSnapshot() else ""
            snaps = []
            for s in dom.listAllSnapshots():
                r = ET.fromstring(s.getXMLDesc())
                created = datetime.fromtimestamp(int(r.findtext("creationTime") or 0))
                memory = r.find("memory")
                with_memory = memory is not None and memory.get("snapshot") not in ("no", None)
                state = r.findtext("state") or ""
                snaps.append((created, Snapshot(
                    name=s.getName(), created=created.strftime("%Y-%m-%d %H:%M"),
                    description=r.findtext("description") or "",
                    state=("encendida, con memoria" if with_memory else
                           "apagada" if state == "shutoff" else state),
                    parent=r.findtext("parent/name") or "", current=s.getName() == current)))
            return [snap for _, snap in sorted(snaps, key=lambda pair: pair[0])]
        finally:
            conn.close()

    def create_snapshot(self, machine_id: str, name: str, description: str, log) -> str:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            if name in dom.snapshotListNames():
                raise DriverError(f"Ya hay un snapshot llamado «{name}».")
            snap = ET.Element("domainsnapshot")
            ET.SubElement(snap, "name").text = name
            if description:
                ET.SubElement(snap, "description").text = description
            log("Creando snapshot" + (" (con la memoria: la VM se pausa unos segundos)…"
                                      if dom.isActive() else "…"))
            # Por defecto libvirt hace un snapshot interno (dentro del qcow2),
            # igual que virt-manager o "virsh snapshot-create-as"
            dom.snapshotCreateXML(ET.tostring(snap, encoding="unicode"), 0)
            log(f"Snapshot «{name}» creado.")
            return name
        except libvirt.libvirtError as e:
            raise DriverError(str(e)) from e
        finally:
            conn.close()

    def revert_snapshot(self, machine_id: str, name: str, log) -> None:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            log(f"Volviendo a «{name}»…")
            dom.revertToSnapshot(self._snapshot(dom, name), 0)
            log("Hecho: la VM queda como estaba en ese momento "
                f"({'encendida' if dom.isActive() else 'apagada'}).")
        except libvirt.libvirtError as e:
            raise DriverError(str(e)) from e
        finally:
            conn.close()

    def delete_snapshot(self, machine_id: str, name: str, log) -> None:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            log(f"Borrando «{name}» (sus hijos pasan a colgar de su padre)…")
            self._snapshot(dom, name).delete(0)
            log("Borrado.")
        except libvirt.libvirtError as e:
            raise DriverError(str(e)) from e
        finally:
            conn.close()

    # --- Borrar ----------------------------------------------------------

    def _disk_owners(self, conn, dom) -> tuple[list[str], dict[str, list[str]]]:
        """Discos propios de la VM y, para cada uno, qué otras VMs lo usan."""
        def disk_paths(d):
            root = ET.fromstring(d.XMLDesc(XML_FLAGS))
            return [s.get("file") or s.get("dev")
                    for s in root.findall("devices/disk[@device='disk']/source")
                    if s.get("file") or s.get("dev")]

        mine = disk_paths(dom)
        shared = {p: [] for p in mine}
        for other in conn.listAllDomains():
            if other.UUIDString() != dom.UUIDString():
                for p in disk_paths(other):
                    if p in shared:
                        shared[p].append(other.name())
        return mine, shared

    def delete_plan(self, machine_id: str) -> DeletePlan:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            plan = DeletePlan(delete=[f"La definición de la VM «{dom.name()}» (config y NVRAM)"])
            if n := dom.snapshotNum():
                what = "Su snapshot" if n == 1 else f"Sus {n} snapshots"
                plan.delete.append(f"{what} (van dentro de los discos: "
                                   "si conservas los discos, quedan inaccesibles desde libvirt)")
            mine, shared = self._disk_owners(conn, dom)
            for path in mine:
                if shared[path]:
                    plan.keep.append(f"{path}: lo usa también {', '.join(shared[path])}")
                else:
                    plan.delete.append(f"{path} (si marcas «Borrar discos»)")
            root = ET.fromstring(dom.XMLDesc(XML_FLAGS))
            plan.keep += [f"{s.get('file')} (ISO/CD-ROM)" for s in
                          root.findall("devices/disk[@device='cdrom']/source") if s.get("file")]
            plan.options.append(Field("delete_disks", "Borrar también los discos", "1",
                                      "checkbox"))
            return plan
        finally:
            conn.close()

    def delete(self, machine_id: str, values: dict[str, str]) -> list[str]:
        conn = self._conn()
        try:
            dom = self._domain(conn, machine_id)
            if dom.isActive():
                raise DriverError("Apaga la VM antes de borrarla.")
            mine, shared = self._disk_owners(conn, dom)
            name = dom.name()
            flags = (libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
                     | libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
                     | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
                     | libvirt.VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA)
            dom.undefineFlags(flags)
            notes = [f"VM «{name}» borrada."]

            if "delete_disks" in values:
                for path in mine:
                    if shared[path]:
                        notes.append(f"Conservado {path} (lo usa {', '.join(shared[path])}).")
                        continue
                    try:
                        conn.storageVolLookupByPath(path).delete(0)
                        notes.append(f"Disco {path} borrado.")
                    except libvirt.libvirtError:
                        notes.append(f"No se pudo borrar {path}: no está en ningún pool de "
                                     "libvirt, bórralo a mano si ya no lo necesitas.")
            return notes
        except libvirt.libvirtError as e:
            raise DriverError(str(e)) from e
        finally:
            conn.close()

    def start(self, machine_id: str) -> None:
        self._action(machine_id, lambda d: d.resume() if STATES.get(d.info()[0]) == "paused"
                     else d.create())

    def stop(self, machine_id: str) -> None:
        # Apagado ordenado por ACPI (como pulsar el botón de apagado)
        self._action(machine_id, lambda d: d.shutdown())

    def kill(self, machine_id: str) -> None:
        self._action(machine_id, lambda d: d.destroy())

    def restart(self, machine_id: str) -> None:
        self._action(machine_id, lambda d: d.reboot())

    def _action(self, machine_id, fn) -> None:
        conn = self._conn()
        try:
            fn(self._domain(conn, machine_id))
        except libvirt.libvirtError as e:
            raise DriverError(str(e)) from e
        finally:
            conn.close()
