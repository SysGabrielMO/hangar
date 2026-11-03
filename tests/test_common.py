import pytest

from hangar.common import host_allowed, safe_next, same_origin
from hangar.drivers.base import DriverError, SNAPSHOT_NAME_RE, parse_ip_request


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:8090", "localhost", "localhost:8090", "[::1]:8090",
])
def test_host_allowed(host):
    assert host_allowed(host)


@pytest.mark.parametrize("host", ["example.com", "127.0.0.1.evil:8090", "0.0.0.0:8090"])
def test_host_rejected(host):
    assert not host_allowed(host)


def test_same_origin():
    host = "127.0.0.1:8090"
    assert same_origin(None, host)          # curl/scripts
    assert same_origin("http://127.0.0.1:8090", host)
    assert not same_origin("http://evil.example", host)


@pytest.mark.parametrize("url", [
    "/", "/?module=BBDD", "/m/kvm/abc?tab=metrics",
])
def test_safe_next_accepts_local(url):
    assert safe_next(url) == url


@pytest.mark.parametrize("url", [
    "", "//evil.example", "https://evil.example", "javascript:alert(1)",
])
def test_safe_next_rejects_external(url):
    assert safe_next(url) == "/"


def test_parse_ip_static():
    mode, address, gateway, dns = parse_ip_request({
        "mode": "static", "address": "192.168.10.20/24",
        "gateway": "192.168.10.1", "dns": "1.1.1.1 8.8.8.8",
    })
    assert (mode, address, gateway, dns) == (
        "static", "192.168.10.20/24", "192.168.10.1", "1.1.1.1 8.8.8.8")


def test_parse_ip_dhcp():
    assert parse_ip_request({"mode": "dhcp", "address": "basura"}) == ("dhcp", "", "", "")


@pytest.mark.parametrize("values, message", [
    ({"mode": "static", "address": "192.168.1.20"}, "máscara"),
    ({"mode": "static", "address": "192.168.1.20/24", "gateway": "10.0.0.1"}, "no está en la red"),
    ({"mode": "static", "address": "192.168.1.20/24", "dns": "no-es-ip"}, "IPv4"),
    ({"mode": "otro", "address": "192.168.1.20/24"}, "Modo"),
])
def test_parse_ip_errors(values, message):
    with pytest.raises(DriverError, match=message):
        parse_ip_request(values)


@pytest.mark.parametrize("name", ["snap-1", "antes_apache", "2026.11.03", "a"])
def test_snapshot_name_valid(name):
    assert SNAPSHOT_NAME_RE.match(name)


@pytest.mark.parametrize("name", ["", "-empieza", "con espacio", "a" * 64, "snap/1"])
def test_snapshot_name_invalid(name):
    assert not SNAPSHOT_NAME_RE.match(name)
