import pytest

from hangar import db, modules
from hangar.drivers import DRIVERS
from hangar.drivers.base import Machine


class FakeDriver:
    kind = "fake"
    label = "Fake"
    can_enable_serial = False

    def __init__(self):
        self.state = "stopped"
        self.started = []
        self.stopped = []

    def start(self, machine_id):
        self.started.append(machine_id)
        self.state = "running"

    def stop(self, machine_id):
        self.stopped.append(machine_id)
        self.state = "stopped"

    def get(self, machine_id):
        return Machine(kind=self.kind, id=machine_id, name=machine_id,
                       state=self.state,
                       ips=["10.0.0.9"] if self.state == "running" else [])


@pytest.fixture
def fake_driver(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setitem(DRIVERS, "fake", driver)
    return driver


def machine(state="stopped"):
    return Machine(kind="fake", id="m1", name="m1", state=state)


def test_prepare_starts_in_order_and_waits(fake_driver):
    logs = []
    modules._prepare("ASO", [(machine(), db.Meta())], logs.append)
    assert fake_driver.started == ["m1"]
    assert any("lista" in line for line in logs)
    assert any("Resumen: 1 lista" in line for line in logs)


def test_prepare_no_machines(fake_driver):
    logs = []
    modules._prepare("ASO", [(machine("running"), db.Meta())], logs.append)
    assert fake_driver.started == []
    assert any("Ya estaba todo encendido" in line for line in logs)


def test_collect_stops_and_waits(fake_driver):
    fake_driver.state = "running"
    logs = []
    modules._collect("ASO", [(machine("running"), db.Meta())], logs.append)
    assert fake_driver.stopped == ["m1"]
    assert any("apagada" in line for line in logs)
    assert any("Resumen: 1 apagada" in line for line in logs)


def test_collect_no_machines(fake_driver):
    logs = []
    modules._collect("ASO", [(machine("stopped"), db.Meta())], logs.append)
    assert fake_driver.stopped == []
    assert any("ya estaba recogido" in line for line in logs)


def test_prepare_failure_is_reported(fake_driver, monkeypatch):
    def boom(machine_id):
        raise modules.DriverError("no arranca")

    monkeypatch.setattr(fake_driver, "start", boom)
    logs = []
    with pytest.raises(modules.DriverError, match="no arrancaron"):
        modules._prepare("ASO", [(machine(), db.Meta())], logs.append)
    assert any("no arranca" in line for line in logs)
