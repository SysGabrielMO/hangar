from hangar import common, db
from hangar.drivers.base import Machine


class FakeDriver:
    kind = "fake"
    label = "Fake"

    def __init__(self, machine):
        self.machine = machine

    def list(self):
        return [self.machine]


def test_list_all_uses_configured_ip(monkeypatch):
    machine = Machine(kind="fake", id="vm-1", name="vm-1", state="stopped")
    monkeypatch.setattr(common, "DRIVERS", {"fake": FakeDriver(machine)})
    monkeypatch.setattr(db, "all_last_ips", lambda: {})
    monkeypatch.setattr(db, "all_iface_ips",
                        lambda: {("fake", "vm-1"): ["10.10.0.5"]})
    monkeypatch.setattr(db, "save_last_ips", lambda *args: None)

    machines, errors = common.list_all()
    assert errors == {}
    assert machines[0].ips == ["10.10.0.5"]


def test_list_all_uses_last_seen_ip(monkeypatch):
    machine = Machine(kind="fake", id="vm-2", name="vm-2", state="stopped")
    monkeypatch.setattr(common, "DRIVERS", {"fake": FakeDriver(machine)})
    monkeypatch.setattr(db, "all_last_ips",
                        lambda: {("fake", "vm-2"): ["192.168.1.20"]})
    monkeypatch.setattr(db, "all_iface_ips", lambda: {})
    monkeypatch.setattr(db, "save_last_ips", lambda *args: None)

    machines, _ = common.list_all()
    assert machines[0].ips == ["192.168.1.20"]


def test_list_all_remembers_live_ip(monkeypatch):
    machine = Machine(kind="fake", id="vm-3", name="vm-3", state="running",
                      ips=["172.16.0.3"])
    saved = []
    monkeypatch.setattr(common, "DRIVERS", {"fake": FakeDriver(machine)})
    monkeypatch.setattr(db, "all_last_ips", lambda: {})
    monkeypatch.setattr(db, "all_iface_ips", lambda: {})
    monkeypatch.setattr(db, "save_last_ips",
                        lambda kind, machine_id, ips: saved.append((kind, machine_id, ips)))

    common.list_all()
    assert saved == [("fake", "vm-3", ["172.16.0.3"])]
