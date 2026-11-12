"""Rutas y utilidades de la interfaz: pestaña Ajustes, tareas, host y redes."""
import ipaddress
import time

import pytest

from hangar import common, db, jobs, main, networks
from hangar.drivers import DRIVERS
from hangar.drivers.base import DeletePlan, DriverError, Field, Machine
from hangar.drivers.kvm import KvmDriver


class FakeDriver:
    kind = "fake"
    label = "Fake"
    no_snapshots_reason = ""
    snapshot_name_auto = False
    edit_fails = False

    def get(self, machine_id):
        if machine_id != "vm-1":
            raise DriverError("No existe")
        return Machine(kind="fake", id="vm-1", name="srv-prueba", state="stopped",
                       memory="1.0 GiB", memory_mib=1024, vcpus=1,
                       sections={"General": [("ID", "vm-1")]}, raw_config="<xml/>")

    def editable(self, machine_id):
        if self.edit_fails:
            raise DriverError("No se pudo leer la configuración")
        return [Field("vcpus", "vCPU", "1", "number")]

    def delete_plan(self, machine_id):
        return DeletePlan(delete=["La máquina «srv-prueba»"])


@pytest.fixture
def fake(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setitem(DRIVERS, "fake", driver)
    monkeypatch.setattr(db, "get_meta", lambda kind, machine_id: db.Meta())
    return driver


def test_settings_tab_gathers_edit_and_delete(client, fake):
    html = client.get("/m/fake/vm-1?tab=settings").text
    assert "Aplicar cambios" in html
    assert "Borrar para siempre" in html
    assert "&lt;xml/&gt;" in html               # la configuración en bruto, plegada


@pytest.mark.parametrize("old_tab", ["edit", "config", "delete"])
def test_old_tab_links_open_settings(client, fake, old_tab):
    html = client.get(f"/m/fake/vm-1?tab={old_tab}").text
    assert "Borrar para siempre" in html


def test_settings_sections_fail_independently(client, fake):
    fake.edit_fails = True
    html = client.get("/m/fake/vm-1?tab=settings").text
    assert "No se pudo leer la configuración" in html
    assert "Borrar para siempre" in html          # el resto de la pestaña sigue ahí


def test_unknown_tab_falls_back_to_summary(client, fake):
    response = client.get("/m/fake/vm-1?tab=disks")   # solo KVM tiene Discos
    assert response.status_code == 200
    assert "General" in response.text


def test_back_sends_settings_sections_to_settings_tab():
    url = common.back("kvm", "vm-1", "delete", error="Nombre incorrecto").headers["location"]
    assert url.startswith("/m/kvm/vm-1?tab=settings&sec=delete&error=")
    assert url.endswith("#delete")
    assert common.back("kvm", "vm-1", "network").headers["location"] == "/m/kvm/vm-1?tab=network"


def test_index_survives_module_removed_from_list(client, monkeypatch):
    machines = [Machine(kind="kvm", id="vm-1", name="antigua", state="stopped")]
    monkeypatch.setattr(main, "list_all", lambda: (machines, {}))
    monkeypatch.setattr(db, "all_meta",
                        lambda: {("kvm", "vm-1"): db.Meta(module="MODULO-RETIRADO")})
    response = client.get("/")
    assert response.status_code == 200
    assert "antigua" in response.text
    assert "Sin módulo" in response.text


def test_static_urls_carry_a_version(client, fake):
    assert "/static/style.css?v=" in client.get("/m/fake/vm-1").text
    assert common.static_url("no-existe.css") == "/static/no-existe.css?v=0"


def test_api_host(client):
    data = client.get("/api/host").json()
    assert {"mem_total", "mem_pct", "load", "cpus", "disk_pct"} <= data.keys()


def test_api_job_reports_progress_and_result(client):
    jobs.JOBS.clear()
    job = jobs.submit("kvm", "Prueba", lambda log: log("hola") or "vm-9", tab="snapshots")
    for _ in range(100):
        if job.status != "running":
            break
        time.sleep(0.01)
    data = client.get(f"/api/jobs/{job.id}").json()
    assert data["status"] == "done"
    assert data["lines"] == ["hola"]
    assert data["url"] == "/m/kvm/vm-9?tab=snapshots"
    assert client.get("/api/jobs/no-existe").status_code == 404


def test_recent_jobs_forget_old_finished_ones():
    jobs.JOBS.clear()
    old = jobs.Job(id="a", kind="kvm", title="vieja", status="done",
                   finished_at=time.time() - jobs.RECENT_SECONDS - 5)
    new = jobs.Job(id="b", kind="kvm", title="reciente", status="done", finished_at=time.time())
    running = jobs.Job(id="c", kind="kvm", title="en curso")
    jobs.JOBS.update({j.id: j for j in (old, new, running)})
    assert [j.title for j in jobs.recent()] == ["en curso", "reciente"]
    jobs.JOBS.clear()


@pytest.mark.parametrize("address,expected", [
    ("10.0.0.1/24", ("10.0.0.128", "10.0.0.254")),     # mitad alta
    ("10.0.0.254/24", ("10.0.0.128", "10.0.0.253")),   # el host no entra en el rango
    ("10.0.0.200/24", ("10.0.0.128", "10.0.0.199")),
    ("10.0.0.6/29", ("10.0.0.4", "10.0.0.5")),
    ("10.0.0.1/30", ("10.0.0.2", "10.0.0.2")),
    ("10.0.0.2/30", ("10.0.0.1", "10.0.0.1")),         # antes: IndexError
    ("10.0.0.1/8", ("10.128.0.0", "10.255.255.254")),  # antes: listaba 16 millones de IPs
])
def test_dhcp_range(address, expected):
    iface = ipaddress.IPv4Interface(address)
    start, end = networks.dhcp_range(iface)
    assert (str(start), str(end)) == expected
    assert not int(start) <= int(iface.ip) <= int(end)
    assert start in iface.network and end in iface.network


def test_kvm_without_networks_asks_virt_install_for_none():
    assert KvmDriver._network_args([]) == ["--network", "none"]
    assert KvmDriver._network_args([{"type": "bridge", "name": "br0", "model": "virtio"}]) == \
        ["--network", "bridge=br0,model=virtio"]


def test_docker_usage_excludes_file_cache(monkeypatch):
    from hangar.drivers.docker_driver import DockerDriver

    class Container:
        status = "running"
        attrs = {"HostConfig": {"Memory": 0}}

        def stats(self, **kwargs):
            assert kwargs.get("one_shot") is True      # sin esperar 1 s a otra muestra
            return {"cpu_stats": {"cpu_usage": {"total_usage": 2_000_000_000}},
                    # cgroup v2: la caché liberable se llama inactive_file, no cache
                    "memory_stats": {"usage": 3000, "stats": {"inactive_file": 1000, "file": 2500}}}

    class Client:
        def close(self):
            pass

    driver = DockerDriver()
    monkeypatch.setattr(driver, "_client", lambda: Client())
    monkeypatch.setattr(driver, "_container", lambda client, machine_id: Container())
    usage = driver.usage("x")
    assert usage.mem_used == 2000
    assert usage.cpu_time == 2.0


# --- Rendimiento y robustez ---------------------------------------------------

class CountingDriver:
    kind = "fake"
    label = "Fake"

    def __init__(self):
        self.calls = 0

    def list(self):
        self.calls += 1
        return [Machine(kind="fake", id="vm-1", name="vm-1", state="running")]


@pytest.fixture
def counting(monkeypatch):
    driver = CountingDriver()
    monkeypatch.setattr(common, "DRIVERS", {"fake": driver})
    for name in ("all_last_ips", "all_iface_ips"):
        monkeypatch.setattr(db, name, lambda: {})
    monkeypatch.setattr(db, "save_last_ips", lambda *args: None)
    return driver


def test_list_all_is_cached_and_invalidated(counting):
    common.list_all()
    common.list_all()
    assert counting.calls == 1                     # la segunda sale de la caché
    common.invalidate_list()
    assert common.list_all()[0][0].name == "vm-1"
    assert counting.calls == 2                     # tras un cambio se vuelve a preguntar


def test_list_all_serves_stale_while_refreshing(counting, monkeypatch):
    common.list_all()
    monkeypatch.setattr(common, "LIST_TTL", 0)     # la lista guardada ya «ha caducado»
    machines, _ = common.list_all()                # responde al momento con la anterior…
    assert machines[0].name == "vm-1"
    for _ in range(200):                           # …y refresca por detrás
        if counting.calls >= 2:
            break
        time.sleep(0.01)
    assert counting.calls >= 2


def test_list_survives_a_driver_that_blows_up(monkeypatch):
    class Broken:
        kind, label = "roto", "Roto"

        def list(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(common, "DRIVERS", {"roto": Broken()})
    for name in ("all_last_ips", "all_iface_ips"):
        monkeypatch.setattr(db, name, lambda: {})
    machines, errors = common.list_all()
    assert machines == [] and "boom" in errors["roto"]


def test_post_invalidates_the_list(client, counting, monkeypatch):
    monkeypatch.setattr(db, "set_module", lambda *args: None)
    common.list_all()
    client.post("/m/fake/vm-1/module", data={"module": ""}, follow_redirects=False)
    common.list_all()
    assert counting.calls == 2


def test_slow_power_action_returns_at_once_and_reports_failure(monkeypatch):
    class Slow:
        kind, label = "fake", "Fake"

        def stop(self, machine_id):
            time.sleep(0.3)
            raise DriverError("no quiso apagarse")

        def get(self, machine_id):
            return Machine(kind="fake", id=machine_id, name="lenta", state="running")

    monkeypatch.setattr(main, "POWER_WAIT", 0.05)
    started = time.monotonic()
    main.run_power(Slow(), "stop", "vm-1")          # no espera a que termine
    assert time.monotonic() - started < 0.25
    for _ in range(100):
        notices = common.pop_notices()
        if notices:
            break
        time.sleep(0.02)
    assert notices and "no quiso apagarse" in notices[0] and "lenta" in notices[0]


def test_fast_power_failure_is_reported_immediately():
    class Failing:
        kind, label = "fake", "Fake"

        def start(self, machine_id):
            raise DriverError("ya está encendida")

    with pytest.raises(DriverError, match="ya está encendida"):
        main.run_power(Failing(), "start", "vm-1")
    assert common.pop_notices() == []


def test_api_state(client, monkeypatch, fake):
    machines = [Machine(kind="fake", id="vm-1", name="x", state="running", ips=["10.0.0.9"])]
    monkeypatch.setattr(main, "find_machine",
                        lambda kind, mid: next((m for m in machines if m.id == mid), None))
    assert client.get("/api/state/fake/vm-1").json()["state"] == "running"
    assert client.get("/api/state/fake/borrada").json()["state"] == "gone"


def test_versioned_static_files_are_cacheable(client):
    assert "immutable" in client.get("/static/app.js?v=1").headers["cache-control"]
    assert "immutable" not in client.get("/static/app.js").headers.get("cache-control", "")


def test_unexpected_error_shows_a_page_not_a_blank_500(monkeypatch):
    from fastapi.testclient import TestClient

    def boom():
        raise RuntimeError("libvirt se ha caído")

    monkeypatch.setattr(main, "list_all", boom)
    quiet = TestClient(main.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    response = quiet.get("/")
    assert response.status_code == 500
    assert "libvirt se ha caído" in response.text and "Hangar" in response.text
    assert quiet.get("/api/status").json()["detail"].startswith("Algo ha fallado")


@pytest.mark.parametrize("url", ["/\\\\evil.example", "/\\tevil", "//evil.example", "http://evil"])
def test_safe_next_rejects_tricky_urls(url):
    assert common.safe_next(url) == "/"


def test_token_check_tolerates_non_ascii(monkeypatch):
    monkeypatch.setattr(common, "ACCESS_TOKEN", "secreto")
    assert not common.token_ok("ñandú")           # antes: TypeError → error 500
    assert common.token_ok("secreto")


def test_db_recreates_tables_if_file_is_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hangar.db")
    db.save_iface_ip("kvm", "vm-1", "mac", "10.0.0.5")
    (tmp_path / "hangar.db").unlink()
    assert db.get_iface_ips("kvm", "vm-1") == {}   # antes: «no such table»
