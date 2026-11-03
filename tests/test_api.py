import pytest

from hangar import db, jobs, main
from hangar.drivers import DRIVERS
from hangar.drivers.base import Interface, Machine


@pytest.fixture
def fake_machines(monkeypatch):
    machines = [
        Machine(kind="kvm", id="vm-1", name="srv-web", state="running",
                cpus="2", memory="2.0 GiB", memory_mib=2048, vcpus=2,
                ips=["10.0.0.5"]),
        Machine(kind="docker", id="cache", name="cache", state="stopped",
                cpus="1", memory="512.0 MiB", memory_mib=512, vcpus=1),
    ]
    meta = {("kvm", "vm-1"): db.Meta(description="Servidor web",
                                     module="IMPWEB", tags="lab")}
    monkeypatch.setattr(main, "list_all", lambda: (machines, {}))
    monkeypatch.setattr(db, "all_meta", lambda: meta)
    monkeypatch.setattr(db, "get_meta",
                        lambda kind, machine_id: meta.get((kind, machine_id), db.Meta()))
    jobs.JOBS.clear()
    return machines


def test_index(client, fake_machines):
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "srv-web" in html
    assert "IMPWEB" in html
    assert "Preparar clase" in html
    assert "RAM configurada" in html
    assert "2.5 GiB" in html            # 2048 + 512 MiB configurados
    assert "3 vCPU" in html             # 2 + 1


def test_status(client, fake_machines):
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["running"] == 1
    assert data["counts"] == {"kvm": 1, "lxc": 0, "docker": 1}
    assert data["machines"][0]["ips"] == ["10.0.0.5"]


def test_api_machines(client, fake_machines):
    response = client.get("/api/machines")
    assert response.status_code == 200
    data = response.json()
    vm = next(m for m in data["machines"] if m["name"] == "srv-web")
    assert vm["memory_mib"] == 2048
    assert vm["meta"]["module"] == "IMPWEB"


def test_metrics_endpoint(client, fake_machines, monkeypatch):
    monkeypatch.setattr(main.metrics, "sample", lambda kind, machine_id: {
        "state": "running", "cpu_pct": 12.5, "mem_pct": 50.0,
        "mem_used": 1024, "mem_limit": 2048, "history": [],
    })
    response = client.get("/api/metrics/kvm/vm-1")
    assert response.status_code == 200
    assert response.json()["cpu_pct"] == 12.5


def test_metrics_unknown_kind(client):
    assert client.get("/api/metrics/nope/x").status_code == 404


def test_host_header_rejected(client):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_csrf_rejected(client):
    response = client.post("/m/kvm/vm-1/stop",
                           headers={"origin": "http://evil.example"})
    assert response.status_code == 403


def test_defined_ip_saved_and_validated(client, monkeypatch):
    saved = []

    class FakeDriver:
        kind = "fake"

        def interfaces(self, machine_id):
            return [Interface(key="mac-1", network="red-privada")]

    monkeypatch.setitem(DRIVERS, "fake", FakeDriver())
    monkeypatch.setattr(db, "save_iface_ip",
                        lambda kind, machine_id, iface, ip: saved.append(ip))

    response = client.post("/m/fake/vm-1/network/mac-1/defined-ip",
                           data={"ip": "10.10.100.50/24"}, follow_redirects=False)
    assert response.status_code == 303
    assert saved == ["10.10.100.50/24"]

    response = client.post("/m/fake/vm-1/network/mac-1/defined-ip",
                           data={"ip": "no-es-ip"}, follow_redirects=False)
    assert response.status_code == 303
    assert saved == ["10.10.100.50/24"]
