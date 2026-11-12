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
from fastapi.responses import JSONResponse, RedirectResponse

from . import db
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
    # La lista de redes de Docker no dice quién hay en cada una: se saca de los
    # contenedores, que además incluye los que están parados.
    members: dict[str, list] = {}
    for c in client.containers.list(all=True, ignore_removed=True):
        for net_name, cfg in ((c.attrs.get("NetworkSettings") or {}).get("Networks") or {}).items():
            ip = (cfg or {}).get("IPAddress") or ((cfg or {}).get("IPAMConfig") or {}).get("IPv4Address")
            members.setdefault(net_name, []).append(("docker", c.name, c.name, [ip] if ip else []))

    nets = []
    for n in sorted(client.networks.list(), key=lambda n: n.name):
        configs = n.attrs.get("IPAM", {}).get("Config") or [{}]
        # La configuración IPv4 (una red puede tener también una IPv6)
        cfg = next((c for c in configs if ":" not in c.get("Subnet", "")), configs[0])
        nets.append(Net(name=n.name, kind="docker", mode=n.attrs.get("Driver", ""),
                        subnet=cfg.get("Subnet", ""), gateway=cfg.get("Gateway", ""),
                        bridge=(n.attrs.get("Options") or {}).get("com.docker.network.bridge.name", ""),
                        builtin=n.name in ("bridge", "host", "none"),
                        members=sorted(members.get(n.name, []), key=lambda m: m[2].lower())))
    return nets


def _networks() -> tuple[list[Net], list[Net], list[str]]:
    """Redes de libvirt y de Docker (sin mirar las máquinas) y los errores."""
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
    return libvirt_nets, docker_nets, errors


def dhcp_range(iface: ipaddress.IPv4Interface) -> tuple[ipaddress.IPv4Address,
                                                        ipaddress.IPv4Address] | None:
    """Rango DHCP para una red cuyo host tiene la IP `iface`: la mitad alta de
    la subred, dejando la baja para IPs fijas y sin incluir nunca la del host.
    None si la subred es tan pequeña que no queda sitio."""
    subnet = iface.network
    first, last = int(subnet.network_address) + 1, int(subnet.broadcast_address) - 1
    start = first + (last - first + 1) // 2
    host = int(iface.ip)
    if start <= host <= last:
        # El host cae en la mitad alta: se usa el trozo más grande que no lo pise
        if host - start >= last - host:
            last = host - 1
        else:
            start = host + 1
    if start > last:
        # Sin sitio arriba (subredes de 2 hosts): lo que quede libre por debajo
        if host <= first:
            return None
        start, last = first, host - 1
    return ipaddress.IPv4Address(start), ipaddress.IPv4Address(last)


def collect():
    """Todas las redes y quién está en cada una."""
    libvirt_nets, docker_nets, errors = _networks()

    by_name = {n.name: n for n in libvirt_nets}
    by_bridge = {n.bridge: n for n in libvirt_nets if n.bridge}
    bridges = {b: Net(name=b, kind="bridge", bridge=b) for b in host_bridges()
               if b not in by_bridge and not b.startswith(("br-", "docker"))}

    machines, list_errors = list_all()
    errors += [f"{DRIVERS[k].label}: {e}" for k, e in list_errors.items()]
    # Interfaces de todas las máquinas con una consulta por tecnología
    # (los contenedores Docker ya vienen en _docker_networks)
    interfaces = {}
    for kind in {m.kind for m in machines} - {"docker"}:
        try:
            interfaces[kind] = DRIVERS[kind].interfaces_all()
        except DriverError:
            interfaces[kind] = {}
    saved_all = db.all_iface_ips_by_key()
    for m in machines:
        if m.kind == "docker":
            continue
        saved_ips = saved_all.get((m.kind, m.id), {})
        for i in interfaces[m.kind].get(m.id, []):
            i.defined_ip = saved_ips.get(i.key) or i.defined_ip
            # KVM indica la red libvirt; LXC indica el bridge
            target = by_name.get(i.network) or by_bridge.get(i.network) or bridges.get(i.network)
            if target is not None:
                ips = i.ips or ([i.defined_ip] if i.defined_ip else [])
                target.members.append((m.kind, m.id, m.name, ips))
    return libvirt_nets, list(bridges.values()), docker_nets, errors


@router.get("/networks")
def networks_page(request: Request, msg: str = "", error: str = ""):
    libvirt_nets, bridges, docker_nets, errors = collect()
    return templates.TemplateResponse(request, "networks.html", {
        "libvirt_nets": libvirt_nets, "bridges": bridges, "docker_nets": docker_nets,
        "errors": errors, "msg": msg, "error": error,
    })


def _check_overlap(subnet: ipaddress.IPv4Network) -> None:
    libvirt_nets, docker_nets, _ = _networks()
    for other in libvirt_nets + docker_nets:
        try:
            other_subnet = ipaddress.ip_network(other.subnet) if other.subnet else None
        except ValueError:
            continue
        if other_subnet is not None and other_subnet.version == 4 \
                and subnet.overlaps(other_subnet):
            raise DriverError(f"{subnet} se solapa con la red «{other.name}» ({other.subnet}).")


@router.post("/networks/libvirt/create")
def create_libvirt(request: Request, name: str = Form(""), mode: str = Form("nat"),
                   address: str = Form(""), dhcp: str = Form(""), autostart: str = Form("")):
    wants_json = "application/json" in request.headers.get("accept", "")
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
        _check_overlap(subnet)

        root = ET.Element("network")
        ET.SubElement(root, "name").text = name
        if mode == "nat":
            ET.SubElement(root, "forward", mode="nat")
        ET.SubElement(root, "bridge", stp="on", delay="0")
        ip = ET.SubElement(root, "ip", address=str(iface.ip), prefix=str(subnet.prefixlen))
        dhcp_note = "sin DHCP"
        if dhcp:
            rng = dhcp_range(iface)
            if rng is None:
                raise DriverError(f"{subnet} es demasiado pequeña para repartir IPs por DHCP: "
                                  "usa una máscara más amplia o desmarca DHCP.")
            start, end = rng
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
        if wants_json:
            return JSONResponse({"detail": str(e)}, status_code=400)
        return _back(error=str(e))
    except libvirt.libvirtError as e:
        if wants_json:
            return JSONResponse({"detail": str(e)}, status_code=400)
        return _back(error=str(e))
    if wants_json:
        return JSONResponse({"name": name, "mode": mode, "subnet": str(subnet),
                             "dhcp": dhcp_note})
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
    except (ValueError, TypeError):    # nombre con caracteres imposibles
        return _back(error="Nombre de red no válido.")
    return _back(error="Acción no válida.")


@router.post("/networks/docker/create")
def create_docker(name: str = Form(""), subnet: str = Form("")):
    try:
        if not NAME_RE.match(name):
            raise DriverError("Nombre: letras, números, guiones o puntos (máx. 31).")
        ipam = None
        if subnet.strip():
            try:
                net = ipaddress.IPv4Network(subnet.strip(), strict=False)
            except ValueError:
                raise DriverError("Subred: escríbela como 172.30.0.0/24") from None
            if net.prefixlen > 30:
                raise DriverError(f"{net} es demasiado pequeña para una red de contenedores.")
            _check_overlap(net)
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
    return _back(f"Red Docker «{name}» creada" + (f" ({net})." if ipam else "."))


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
