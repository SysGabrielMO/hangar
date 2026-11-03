import os

import pytest

from hangar import metrics
from hangar.drivers import DRIVERS
from hangar.drivers.base import Usage


class FakeDriver:
    kind = "fake"

    def __init__(self):
        self.values = [
            Usage(state="running", cpu_time=0.0, mem_used=512, mem_limit=1024),
            Usage(state="running", cpu_time=1.0, mem_used=512, mem_limit=1024),
        ]

    def usage(self, machine_id):
        return self.values.pop(0)


@pytest.fixture(autouse=True)
def clean_metrics(monkeypatch):
    monkeypatch.setattr(metrics, "CACHE_SECONDS", 0)
    metrics._cache.clear()
    metrics._prev.clear()
    metrics._history.clear()


def test_sample_stopped(monkeypatch):
    class Stopped:
        kind = "fake"

        def usage(self, machine_id):
            return Usage(state="stopped")

    monkeypatch.setitem(DRIVERS, "fake", Stopped())
    data = metrics.sample("fake", "m1")
    assert data["state"] == "stopped"
    assert data["history"] == []


def test_sample_cpu_and_memory(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setitem(DRIVERS, "fake", driver)
    ticks = iter([100.0, 100.0, 101.0, 101.0])
    monkeypatch.setattr(metrics.time, "monotonic", lambda: next(ticks))

    first = metrics.sample("fake", "m1")
    assert first["cpu_pct"] is None
    assert first["history"][0]["cpu"] is None
    second = metrics.sample("fake", "m1")
    assert second["cpu_pct"] == pytest.approx(100 / (os.cpu_count() or 1), abs=0.2)
    assert second["mem_pct"] == 50.0
    assert len(second["history"]) == 2
