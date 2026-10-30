"""Página de redes: redes libvirt, bridges del host y redes Docker, con qué
máquinas cuelgan de cada una. Permite crear y borrar redes libvirt y Docker."""
from __future__ import annotations

import ipaddress
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import quote

import docker
import libvirt
from docker.errors import DockerException
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .common import list_all, templates
from .drivers import DRIVERS, DriverError
from .drivers.base import host_bridges
from .drivers.docker_driver import docker_client

router = APIRouter()
URI = "qemu:///system"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,30}$")


@dataclass
class Net:
    name: str
    kind: str                     # "libvirt" | "bridge" | "docker"
    mode: str = ""
    bridge: str = ""
    subnet: str = ""
    gateway: str = ""
    dhcp: str = ""                # rango, o "" si no hay DHCP
    active: bool = True
    autostart: bool = False
    builtin: bool = False         # redes que no se deben borrar
    members: list[tuple[str, str, str, list[str]]] = field(default_factory=list)
    # (tipo, id, nombre, ips)


def _back(msg: str = "", error: str = ""):
    return RedirectResponse(f"/networks?{'msg' if msg else 'error'}={quote(msg or error)}",
                            status_code=303)


def _libvirt_networks(conn) -> list[Net]:
    nets = []
    for n in sorted(conn.listAllNetworks(), key=lambda n: n.name()):
        root = ET.fromstring(n.XMLDesc())
        fwd, br = root.find("forward"), root.find("bridge")
        net = Net(name=n.name(), kind="libvirt", active=bool(n.isActive()),
                  autostart=bool(n.autostart()), bridge=br.get("name", "") if br is not None else "",
                  mode=(fwd.get("mode", "nat") if fwd is not None else "aislada"))
        for ip in root.findall("ip"):
            if ip.get("family", "ipv4") == "ipv4" and ip.get("address"):
                mask = ip.get("prefix") or ip.get("netmask") or "24"
                net.subnet = str(ipaddress.IPv4Network(f"{ip.get('address')}/{mask}", strict=False))
                net.gateway = ip.get("address")
                rng = ip.find("dhcp/range")
                if rng is not None:
                    net.dhcp = f"{rng.get('start')} – {rng.get('end')}"
                break
        nets.append(net)
    return nets


def _docker_networks(client) -> list[Net]:
    nets = []
    for n in sorted(client.networks.list(), key=lambda n: n.name):
        cfg = (n.attrs.get("IPAM", {}).get("Config") or [{}])[0]
        net = Net(name=n.name, kind="docker", mode=n.attrs.get("Driver", ""),
                  subnet=cfg.get("Subnet", ""), gateway=cfg.get("Gateway", ""),
                  bridge=(n.attrs.get("Options") or {}).get("com.docker.network.bridge.name", ""),
                  builtin=n.name in ("bridge", "host", "none"))
        for cid, c in (n.attrs.get("Containers") or {}).items():
            net.members.append(("docker", c.get("Name", cid[:12]), c.get("Name", cid[:12]),
                                [c["IPv4Address"]] if c.get("IPv4Address") else []))
        nets.append(net)
    return nets


def collect():
    """Todas las redes y quién está en cada una."""
    errors, libvirt_nets, docker_nets = [], [], []
    try:
        conn = libvirt.open(URI)
        try:
            libvirt_nets = _libvirt_networks(conn)
        finally:
            conn.close()
    except libvirt.libvirtError as e:
        errors.append(f"libvirt: {e}")
    try:
        client = docker_client()
        try:
            docker_nets = _docker_networks(client)
        finally:
            client.close()
    except (DockerException, DriverError) as e:
        errors.append(str(e) if isinstance(e, DriverError) else f"Docker: {e}")

    by_name = {n.name: n for n in libvirt_nets}
    by_bridge = {n.bridge: n for n in libvirt_nets if n.bridge}
    bridges = {b: Net(name=b, kind="bridge", bridge=b) for b in host_bridges()
               if b not in by_bridge and not b.startswith(("br-", "docker"))}

    machines, list_errors = list_all()
    errors += [f"{DRIVERS[k].label}: {e}" for k, e in list_errors.items()]
    for m in machines:
        if m.kind == "docker":
            continue      # ya vienen en _docker_networks
        try:
            ifaces = DRIVERS[m.kind].interfaces(m.id)
        except DriverError:
            continue
        for i in ifaces:
            # KVM indica la red libvirt; LXC indica el bridge
            target = by_name.get(i.network) or by_bridge.get(i.network) or bridges.get(i.network)
            if target is not None:
                target.members.append((m.kind, m.id, m.name, i.ips))
    return libvirt_nets, list(bridges.values()), docker_nets, errors


@router.get("/networks")
def networks_page(request: Request, msg: str = "", error: str = ""):
    libvirt_nets, bridges, docker_nets, errors = collect()
    return templates.TemplateResponse(request, "networks.html", {
        "libvirt_nets": libvirt_nets, "bridges": bridges, "docker_nets": docker_nets,
        "errors": errors, "msg": msg, "error": error,
    })


def _check_overlap(subnet: ipaddress.IPv4Network, name: str) -> None:
    libvirt_nets, _, docker_nets, _ = collect()
    for other in libvirt_nets + docker_nets:
        if other.subnet and subnet.overlaps(ipaddress.ip_network(other.subnet)):
            raise DriverError(f"{subnet} se solapa con la red «{other.name}» ({other.subnet}).")


@router.post("/networks/libvirt/create")
def create_libvirt(name: str = Form(""), mode: str = Form("nat"), address: str = Form(""),
                   dhcp: str = Form(""), autostart: str = Form("")):
    try:
        if not NAME_RE.match(name):
            raise DriverError("Nombre: letras, números, guiones o puntos (máx. 31).")
        if mode not in ("nat", "isolated"):
            raise DriverError("Modo no válido.")
        try:
            iface = ipaddress.IPv4Interface(address.strip())
        except ValueError:
            raise DriverError("Dirección: la IP del host en la red con su máscara, "
                              "p. ej. 10.30.0.1/24") from None
        subnet = iface.network
        if subnet.prefixlen > 30 or iface.ip in (subnet.network_address, subnet.broadcast_address):
            raise DriverError(f"{iface} no sirve como IP del host en {subnet}.")
        _check_overlap(subnet, name)

        root = ET.Element("network")
        ET.SubElement(root, "name").text = name
        if mode == "nat":
            ET.SubElement(root, "forward", mode="nat")
        ET.SubElement(root, "bridge", stp="on", delay="0")
        ip = ET.SubElement(root, "ip", address=str(iface.ip), prefix=str(subnet.prefixlen))
        dhcp_note = "sin DHCP"
        if dhcp:
            hosts = list(subnet.hosts())
            # Rango en la mitad alta, dejando la baja para IPs estáticas
            start, end = hosts[len(hosts) // 2], hosts[-1]
            if iface.ip in (start, end):
                start = hosts[len(hosts) // 2 + 1]
            ET.SubElement(ET.SubElement(ip, "dhcp"), "range", start=str(start), end=str(end))
            dhcp_note = f"DHCP {start} – {end}"

        conn = libvirt.open(URI)
        try:
            net = conn.networkDefineXML(ET.tostring(root, encoding="unicode"))
            net.create()
            if autostart:
                net.setAutostart(1)
        finally:
            conn.close()
    except DriverError as e:
        return _back(error=str(e))
    except libvirt.libvirtError as e:
        return _back(error=str(e))
    kind = "NAT" if mode == "nat" else "aislada"
    return _back(f"Red «{name}» creada: {kind}, {subnet}, {dhcp_note}.")


@router.post("/networks/libvirt/{name}/{action}")
def libvirt_action(name: str, action: str):
    try:
        conn = libvirt.open(URI)
        try:
            net = conn.networkLookupByName(name)
            if action == "start":
                net.create()
                return _back(f"Red «{name}» activada.")
            if action == "stop":
                net.destroy()
                return _back(f"Red «{name}» desactivada.")
            if action == "delete":
                users = [d.name() for d in conn.listAllDomains()
                         if f"network='{name}'" in d.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)]
                if users:
                    raise DriverError(f"No se puede borrar «{name}»: la usan {', '.join(users)}.")
                if net.isActive():
                    net.destroy()
                net.undefine()
                return _back(f"Red «{name}» borrada.")
        finally:
            conn.close()
    except DriverError as e:
        return _back(error=str(e))
    except libvirt.libvirtError as e:
        return _back(error=str(e))
    return _back(error="Acción no válida.")


@router.post("/networks/docker/create")
def create_docker(name: str = Form(""), subnet: str = Form("")):
    try:
        if not NAME_RE.match(name):
            raise DriverError("Nombre: letras, números, guiones o puntos (máx. 31).")
        ipam = None
        if subnet.strip():
            try:
                net = ipaddress.IPv4Network(subnet.strip())
            except ValueError:
                raise DriverError("Subred: escríbela como 172.30.0.0/24") from None
            _check_overlap(net, name)
            ipam = docker.types.IPAMConfig(pool_configs=[docker.types.IPAMPool(
                subnet=str(net), gateway=str(next(net.hosts())))])
        client = docker_client()
        try:
            client.networks.create(name, driver="bridge", ipam=ipam)
        finally:
            client.close()
    except DriverError as e:
        return _back(error=str(e))
    except DockerException as e:
        return _back(error=getattr(e, "explanation", None) or str(e))
    return _back(f"Red Docker «{name}» creada" + (f" ({subnet.strip()})." if ipam else "."))


@router.post("/networks/docker/{name}/delete")
def delete_docker(name: str):
    try:
        if name in ("bridge", "host", "none"):
            raise DriverError("Las redes propias de Docker no se borran.")
        client = docker_client()
        try:
            net = client.networks.get(name)
            if net.attrs.get("Containers"):
                raise DriverError(f"No se puede borrar «{name}»: tiene contenedores conectados.")
            net.remove()
        finally:
            client.close()
    except DriverError as e:
        return _back(error=str(e))
    except DockerException as e:
        return _back(error=getattr(e, "explanation", None) or str(e))
    return _back(f"Red Docker «{name}» borrada.")
