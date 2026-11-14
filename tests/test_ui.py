"""Rutas y utilidades de la interfaz: pestaña Ajustes, tareas, host y redes."""
import ipaddress
import time

import pytest

from hangar import common, db, jobs, main, networks
from hangar.drivers import DRIVERS
from hangar.drivers.base import DeletePlan, DriverError, Field, Machine, Usage
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


# --- Consumo en vivo en la portada y resumen de la ficha -------------------

class UsageDriver:
    """Driver mínimo que solo sabe decir cuánto consumen sus máquinas."""

    def __init__(self):
        self.cpu_time = 10.0
        self.calls = 0

    def usage_all(self):
        self.calls += 1
        return {"a": Usage(state="running", cpu_time=self.cpu_time,
                           mem_used=512 * 2**20, mem_limit=1024 * 2**20)}


@pytest.fixture
def usage_driver(monkeypatch):
    from hangar import metrics
    driver = UsageDriver()
    monkeypatch.setattr(metrics, "DRIVERS", {"fake": driver})
    monkeypatch.setattr(metrics, "_all_cache", (0.0, {}))
    metrics.forget("fake", "a")
    yield driver
    metrics.forget("fake", "a")


def test_sample_all_needs_two_readings_for_cpu(usage_driver, monkeypatch):
    from hangar import metrics
    first = metrics.sample_all({"fake"})
    assert first["fake/a"] == {"cpu": None, "mem": 512 * 2**20, "limit": 1024 * 2**20}

    monkeypatch.setattr(metrics, "_all_cache", (0.0, {}))      # caduca la lectura guardada
    usage_driver.cpu_time += 0.001
    second = metrics.sample_all({"fake"})
    assert second["fake/a"]["cpu"] is not None
    assert 0 <= second["fake/a"]["cpu"] <= 100


def test_sample_all_is_cached_between_polls(usage_driver):
    from hangar import metrics
    metrics.sample_all({"fake"})
    metrics.sample_all({"fake"})
    assert usage_driver.calls == 1


def test_sample_all_survives_a_broken_driver(monkeypatch):
    from hangar import metrics

    class Broken:
        def usage_all(self):
            raise RuntimeError("se cayó")

    monkeypatch.setattr(metrics, "DRIVERS", {"roto": Broken()})
    monkeypatch.setattr(metrics, "_all_cache", (0.0, {}))
    assert metrics.sample_all({"roto", "no-existe"}) == {}


def test_base_usage_all_skips_stopped_and_failing_machines():
    from hangar.drivers.base import Driver, Machine

    class Three(Driver):
        def list(self):
            return [Machine("x", "on", "on", "running"), Machine("x", "off", "off", "stopped"),
                    Machine("x", "bad", "bad", "running")]

        def usage(self, machine_id):
            if machine_id == "bad":
                raise DriverError("ya no está")
            return Usage(state="running", cpu_time=1.0)

    assert list(Three().usage_all()) == ["on"]


def test_lxc_usage_reads_the_cgroup(tmp_path, monkeypatch):
    from hangar.drivers import lxc
    box = tmp_path / "lxc.payload.web"
    box.mkdir()
    (box / "memory.current").write_text("1000000\n")
    (box / "memory.max").write_text("max\n")
    (box / "memory.stat").write_text("anon 5\ninactive_file 400000\n")
    (box / "cpu.stat").write_text("usage_usec 2500000\nuser_usec 1\n")
    monkeypatch.setattr(lxc, "CGROUP_ROOT", tmp_path)

    usage = lxc._cgroup_usage("web")
    assert (usage.cpu_time, usage.mem_used, usage.mem_limit) == (2.5, 600000, 0)
    assert lxc._cgroup_usage("no-existe") is None
    assert list(lxc.LxcDriver().usage_all()) == ["web"]

    (box / "memory.max").write_text("2000000\n")
    assert lxc.LxcDriver().usage("web").mem_limit == 2000000


def test_api_status_includes_usage(client, monkeypatch):
    machines = [Machine("kvm", "a", "a", "running")]
    monkeypatch.setattr(main, "list_all", lambda: (machines, {}))
    seen = []
    monkeypatch.setattr(main.metrics, "sample_all",
                        lambda kinds: seen.append(set(kinds)) or {"kvm/a": {"cpu": 1.5, "mem": 1, "limit": 0}})
    data = client.get("/api/status").json()
    assert data["usage"] == {"kvm/a": {"cpu": 1.5, "mem": 1, "limit": 0}}
    assert seen == [{"kvm"}]


def test_summary_shows_overview_and_folds_technical_details(client, fake):
    page = client.get("/m/fake/vm-1").text
    assert 'class="overview"' in page
    assert "Detalles técnicos" in page
    assert 'class="card tech"' in page


def test_summary_survives_failing_overview_sources(client, fake, monkeypatch):
    def boom(machine_id):
        raise RuntimeError("libvirt se ha ido")

    monkeypatch.setattr(fake, "interfaces", boom, raising=False)
    monkeypatch.setattr(fake, "snapshots", boom, raising=False)
    response = client.get("/m/fake/vm-1")
    assert response.status_code == 200
    assert "No está conectada a ninguna red" in response.text


def test_success_message_is_marked_as_toast_but_errors_stay(client, fake):
    page = client.get("/m/fake/vm-1?msg=Guardado&error=Mal").text
    assert '<div class="alert ok" data-toast>' in page
    assert '<div class="alert error">' in page


@pytest.mark.parametrize("kind", ["kvm", "lxc", "docker"])
def test_create_form_groups_every_field_once(client, kind, monkeypatch):
    try:
        expected = [f.key for f in main.DRIVERS[kind].create_fields()]
    except DriverError:
        pytest.skip("sin acceso a esa tecnología en este equipo")
    page = client.get(f"/new/{kind}").text
    assert "form-block" in page
    for key in expected:
        assert page.count(f'data-field="{key}"') == 1, key


# --- Acciones en lote, IP de las apagadas y memoria de las VM --------------

class PowerDriver:
    def __init__(self):
        self.calls = []

    def start(self, machine_id):
        self.calls.append(("start", machine_id))

    def stop(self, machine_id):
        self.calls.append(("stop", machine_id))
        if machine_id == "rota":
            raise DriverError("no responde")


@pytest.fixture
def bulk_env(monkeypatch):
    driver = PowerDriver()
    monkeypatch.setitem(DRIVERS, "fake", driver)
    machines = [Machine("fake", "on", "on", "running"), Machine("fake", "off", "off", "stopped"),
                Machine("fake", "rota", "rota", "running")]
    monkeypatch.setattr(main, "list_all", lambda: (machines, {}))
    return driver


def test_bulk_start_only_touches_stopped_machines(client, bulk_env):
    data = client.post("/bulk", json={"action": "start",
                                      "keys": ["fake/on", "fake/off", "fake/off", "fake/nope"]}).json()
    assert bulk_env.calls == [("start", "off")]
    assert (data["done"], data["skipped"], data["errors"]) == (1, 1, [])


def test_bulk_stop_reports_the_ones_that_fail(client, bulk_env):
    data = client.post("/bulk", json={"action": "stop", "keys": ["fake/on", "fake/rota"]}).json()
    assert sorted(bulk_env.calls) == [("stop", "on"), ("stop", "rota")]
    assert data["errors"] == ["rota: no responde"]


def test_bulk_module_moves_every_machine(client, bulk_env, monkeypatch):
    moved = []
    monkeypatch.setattr(db, "set_module", lambda kind, machine_id, module: moved.append((machine_id, module)))
    response = client.post("/bulk", json={"action": "module", "keys": ["fake/on", "fake/off"],
                                          "module": "ASO"})
    assert response.json()["done"] == 2
    assert moved == [("on", "ASO"), ("off", "ASO")]


@pytest.mark.parametrize("body", [
    {"action": "delete", "keys": ["fake/on"]},          # borrar en lote no existe, a propósito
    {"action": "start", "keys": []},
    {"action": "start", "keys": [1, 2]},
    {"action": "start", "keys": "fake/on"},
    {"action": "module", "keys": ["fake/on"], "module": "NO-EXISTE"},
    {"keys": ["fake/on"]},
    [],
])
def test_bulk_rejects_bad_requests(client, bulk_env, body):
    assert client.post("/bulk", json=body).status_code == 400
    assert bulk_env.calls == []


def test_bulk_with_only_unknown_machines_is_404(client, bulk_env):
    assert client.post("/bulk", json={"action": "stop", "keys": ["fake/nope"]}).status_code == 404


def test_index_marks_ips_of_stopped_machines_as_last_known(client, monkeypatch):
    machines = [Machine("kvm", "a", "encendida", "running", ips=["10.0.0.1"]),
                Machine("kvm", "b", "apagada", "stopped", ips=["10.0.0.2"])]
    monkeypatch.setattr(main, "list_all", lambda: (machines, {}))
    monkeypatch.setattr(db, "all_meta", lambda: {})
    page = client.get("/").text
    assert page.count('class="mono js-ips stale"') == 1
    assert page.count('class="js-select"') == 2
    assert 'id="bulk-bar"' in page


def test_kvm_memory_uses_guest_view_and_never_exceeds_the_vm():
    import libvirt

    class Dom:
        def __init__(self, stats):
            self.stats = stats

        def info(self):
            return [libvirt.VIR_DOMAIN_RUNNING, 786432, 786432, 1, 5_000_000_000]

        def memoryStats(self):
            return self.stats

    # Con globo: disponible - utilizable (lo que el invitado usa de verdad)
    with_balloon = KvmDriver._usage_of(Dom({"available": 733116, "usable": 550420, "rss": 826656}))
    assert with_balloon.mem_used == (733116 - 550420) * 1024
    # Sin globo: el proceso de QEMU ocupa más que la RAM de la VM; no se pasa del 100 %
    without = KvmDriver._usage_of(Dom({"rss": 826656}))
    assert without.mem_used == without.mem_limit == 786432 * 1024
    assert without.cpu_time == 5.0


def test_lxc_fixed_ip_goes_first():
    from hangar.drivers.lxc import LxcDriver
    dump = ("==> web\nName:           web\nState:          RUNNING\nIP:             10.0.3.234\n"
            "IP:             10.0.3.77\n--- config\nlxc.net.0.type = veth\n"
            "lxc.net.0.ipv4.address = 10.0.3.77/24\n")
    assert LxcDriver()._parse_dump(dump)[0].ips == ["10.0.3.77", "10.0.3.234"]


class FakeDom:
    def __init__(self, name, cdroms):
        self._name, self._cdroms = name, cdroms

    def name(self):
        return self._name

    def XMLDesc(self, flags=0):
        disks = "".join(f"<disk device='cdrom'><source file='{path}'/></disk>" for path in self._cdroms)
        return f"<domain><devices>{disks}</devices></domain>"


def test_kvm_delete_takes_its_own_cloud_init_iso_but_not_other_isos():
    dom = FakeDom("web", ["/img/web-seed.iso", "/img/web-seed-2.iso", "/img/debian-13.iso",
                          "/img/otra-web-seed.iso"])
    seeds, isos = KvmDriver._cdroms(dom)
    assert seeds == ["/img/web-seed.iso", "/img/web-seed-2.iso"]
    assert isos == ["/img/debian-13.iso", "/img/otra-web-seed.iso"]


def test_kvm_delete_frees_dhcp_reservations():
    updates = []

    class Net:
        def name(self):
            return "default"

        def isActive(self):
            return True

        def XMLDesc(self):
            return ("<network><ip address='10.0.0.1'><dhcp>"
                    "<host mac='52:54:00:AA:BB:01' ip='10.0.0.5'/>"
                    "<host mac='52:54:00:aa:bb:02' ip='10.0.0.6'/></dhcp></ip></network>")

        def update(self, command, section, index, xml, flags):
            updates.append(xml)

    class Conn:
        def listAllNetworks(self):
            return [Net()]

    assert KvmDriver._drop_reservations(Conn(), {"52:54:00:aa:bb:01"}) == ["10.0.0.5 en default"]
    assert len(updates) == 1 and "10.0.0.5" in updates[0]
