from hangar.drivers.base import Usage
from hangar.drivers.kvm import KvmDriver


class FakeDomain:
    def __init__(self, state=1):
        self.state = state

    def info(self):
        # [estado, maxMem KiB, memoria KiB, vCPU, tiempo de CPU ns]
        return [self.state, 8 * 1024 ** 2, 8 * 1024 ** 2, 4, 137_650_000_000]

    def memoryStats(self):
        # libvirt lo devuelve en KiB: 4.129.452 KiB son ~3,94 GiB.
        return {"rss": 4_129_452, "actual": 8 * 1024 ** 2}


class FakeConnection:
    def close(self):
        pass


def test_kvm_usage_converts_kib_to_bytes(monkeypatch):
    driver = KvmDriver()
    monkeypatch.setattr(driver, "_conn", lambda: FakeConnection())
    monkeypatch.setattr(driver, "_domain", lambda conn, machine_id: FakeDomain())

    usage = driver.usage("vm-1")
    assert isinstance(usage, Usage)
    assert usage.state == "running"
    assert usage.mem_used == 4_129_452 * 1024
    assert usage.mem_limit == 8 * 1024 ** 3
    assert usage.cpu_time == 137.65


def test_kvm_usage_stopped(monkeypatch):
    driver = KvmDriver()
    monkeypatch.setattr(driver, "_conn", lambda: FakeConnection())
    monkeypatch.setattr(driver, "_domain", lambda conn, machine_id: FakeDomain(state=5))

    usage = driver.usage("vm-1")
    assert usage.state == "stopped"
    assert usage.mem_used == 0
