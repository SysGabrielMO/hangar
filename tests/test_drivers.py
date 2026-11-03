import pytest

from hangar.drivers.base import human_bytes
from hangar.drivers.lxc import LxcDriver, _memory_mib, _parse_seconds, _parse_size


@pytest.mark.parametrize("value, mib", [
    ("2G", 2048), ("512M", 512), ("1073741824", 1024),
    ("1024K", 1), ("max", 0), ("", 0),
])
def test_lxc_memory_mib(value, mib):
    assert _memory_mib(value) == mib


@pytest.mark.parametrize("value, seconds", [
    ("0.53s", 0.53), ("1.23 seconds", 1.23), ("12", 12.0), ("-", 0.0),
])
def test_lxc_parse_seconds(value, seconds):
    assert _parse_seconds(value) == seconds


@pytest.mark.parametrize("value, size", [
    ("12.34 MiB", int(12.34 * 1024 ** 2)),
    ("1 KiB", 1024),
    ("2 GiB", 2 * 1024 ** 3),
    ("0 B", 0),
    ("sin datos", 0),
])
def test_lxc_parse_size(value, size):
    assert _parse_size(value) == size


def test_human_bytes():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1024) == "1 KiB"
    assert human_bytes(2 * 1024 ** 3) == "2.0 GiB"


LXC_DUMP = """==> c1
Name:           c1
State:          RUNNING
PID:            1234
IP:             10.0.0.5
CPU use:        1.23 seconds
Memory use:     12.34 MiB
--- config
lxc.cgroup2.cpuset.cpus = 0-1
lxc.cgroup2.memory.max = 1G
"""


def test_lxc_usage_from_lxc_info(monkeypatch):
    driver = LxcDriver()
    monkeypatch.setattr(driver, "_run", lambda *args: LXC_DUMP)

    usage = driver.usage("c1")
    assert usage.state == "running"
    assert usage.cpu_time == 1.23
    assert usage.mem_used == int(12.34 * 1024 ** 2)
    assert usage.mem_limit == 1024 ** 3


def test_lxc_usage_stopped_from_lxc_info(monkeypatch):
    driver = LxcDriver()
    stopped = LXC_DUMP.replace("State:          RUNNING", "State:          STOPPED")
    monkeypatch.setattr(driver, "_run", lambda *args: stopped)

    usage = driver.usage("c1")
    assert usage.state == "stopped"
    assert usage.mem_used == 0
