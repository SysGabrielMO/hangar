import pytest

from hangar.drivers.base import human_bytes
from hangar.drivers.lxc import _memory_mib, _parse_seconds, _parse_size


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
