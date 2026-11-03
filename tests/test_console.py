from hangar import console
from hangar.drivers.base import Machine


def test_consoles_page_lists_running_machines(client, monkeypatch):
    machines = [
        Machine(kind="kvm", id="vm-1", name="srv-web", state="running",
                cpus="2", memory="2.0 GiB"),
        Machine(kind="docker", id="cache", name="cache", state="stopped",
                cpus="1", memory="512.0 MiB"),
    ]
    monkeypatch.setattr(console, "list_all", lambda: (machines, {}))
    response = client.get("/consoles")
    assert response.status_code == 200
    assert "srv-web" in response.text
    assert "vm-1" in response.text
    assert "cache" not in response.text


def test_consoles_page_without_running_machines(client, monkeypatch):
    monkeypatch.setattr(console, "list_all", lambda: ([], {}))
    response = client.get("/consoles")
    assert response.status_code == 200
    assert "No hay máquinas encendidas" in response.text
