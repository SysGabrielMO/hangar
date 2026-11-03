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


def test_parse_disks():
    from hangar.drivers.kvm import _parse_disks

    disks = _parse_disks("20:default:qcow2\n50:fast:raw\n", {"default", "fast"}, "default")
    assert disks == [
        {"size_gib": 20, "pool": "default", "format": "qcow2"},
        {"size_gib": 50, "pool": "fast", "format": "raw"},
    ]


def test_parse_disks_defaults_and_comments():
    from hangar.drivers.kvm import _parse_disks

    disks = _parse_disks("# disco del sistema\n20\n", {"default"}, "default")
    assert disks == [{"size_gib": 20, "pool": "default", "format": "qcow2"}]


def test_parse_disks_errors():
    import pytest

    from hangar.drivers.base import DriverError
    from hangar.drivers.kvm import _parse_disks

    for text, message in [
        ("", "al menos un disco"),
        ("0:default:qcow2", "mínimo"),
        ("20:otro:qcow2", "pool"),
        ("20:default:vmdk", "formato"),
        ("xx:default:qcow2", "no es un número"),
    ]:
        with pytest.raises(DriverError, match=message):
            _parse_disks(text, {"default"}, "default")


def test_parse_networks():
    from hangar.drivers.kvm import _parse_networks

    nets = _parse_networks("default:virtio\nred-privada:e1000e", {"default", "red-privada"})
    assert nets == [
        {"network": "default", "model": "virtio"},
        {"network": "red-privada", "model": "e1000e"},
    ]
    assert _parse_networks("", {"default"}) == []


def test_parse_networks_errors():
    import pytest

    from hangar.drivers.base import DriverError
    from hangar.drivers.kvm import _parse_networks

    with pytest.raises(DriverError, match="no existe"):
        _parse_networks("otra:virtio", {"default"})
    with pytest.raises(DriverError, match="modelo"):
        _parse_networks("default:pcnet", {"default"})


def test_next_disk_target():
    import xml.etree.ElementTree as ET

    from hangar.drivers.kvm import KvmDriver

    root = ET.fromstring("<domain><devices><disk><target dev='vda'/></disk></devices></domain>")
    assert KvmDriver._next_disk_target(root, "virtio") == "vdb"
    assert KvmDriver._next_disk_target(root, "scsi") == "sda"


class FakeVolume:
    def path(self):
        return "/var/lib/libvirt/images/test-disco2.qcow2"

    def info(self):
        return (0, 20 * 1024 ** 3, 0)

    def delete(self, flags=0):
        pass


class FakePool:
    def __init__(self):
        self.created = ""
        self.volume = FakeVolume()

    def isActive(self):
        return True

    def refresh(self, flags=0):
        pass

    def listAllVolumes(self):
        return []

    def createXML(self, xml, flags=0):
        self.created = xml
        return self.volume


class FakeDiskDomain:
    def __init__(self):
        self.attached = ""

    def name(self):
        return "test"

    def isActive(self):
        return False

    def XMLDesc(self, flags=0):
        return "<domain><devices><disk device='disk'><target dev='vda'/></disk></devices></domain>"

    def attachDeviceFlags(self, xml, flags):
        self.attached = xml


def test_add_disk_creates_volume_and_attaches(monkeypatch):
    from hangar.drivers.kvm import KvmDriver

    driver = KvmDriver()
    pool = FakePool()
    dom = FakeDiskDomain()

    class Conn:
        def storagePoolLookupByName(self, name):
            assert name == "default"
            return pool

        def close(self):
            pass

    monkeypatch.setattr(driver, "_conn", lambda: Conn())
    monkeypatch.setattr(driver, "_domain", lambda conn, machine_id: dom)

    notes = driver.add_disk("vm-1", {
        "size_gib": "20", "pool": "default", "format": "qcow2", "bus": "virtio",
    })
    assert "Disco vdb añadido" in notes[0]
    assert "<name>test-disco2.qcow2</name>" in pool.created
    assert "<target dev='vdb'" in dom.attached or 'dev="vdb"' in dom.attached


def test_create_uses_all_disks_and_networks(monkeypatch):
    from hangar.drivers import kvm as kvm_module

    driver = kvm_module.KvmDriver()
    captured = []

    class CreatedDomain:
        def UUIDString(self):
            return "uuid-1"

    class Conn:
        def lookupByName(self, name):
            return CreatedDomain()

        def close(self):
            pass

    def fake_run(cmd, log, timeout=3600):
        captured.extend(cmd)

    monkeypatch.setattr(driver, "_conn", lambda: Conn())
    monkeypatch.setattr(kvm_module, "run_streaming", fake_run)

    result = driver.create({
        "name": "test", "vcpus": 2, "memory_mib": 2048,
        "disks": [
            {"size_gib": 20, "pool": "default", "format": "qcow2"},
            {"size_gib": 50, "pool": "default", "format": "raw"},
        ],
        "networks": [
            {"network": "default", "model": "virtio"},
            {"network": "red-privada", "model": "e1000e"},
        ],
        "iso": "/tmp/debian.iso", "osinfo": "",
    }, lambda line: None)

    assert result == "uuid-1"
    assert captured.count("--disk") == 2
    assert captured.count("--network") == 2
    assert "pool=default,size=20,format=qcow2" in captured
    assert "network=red-privada,model=e1000e" in captured


def test_create_from_qcow2_image_with_cloud_init(tmp_path, monkeypatch):
    from hangar.drivers import kvm as kvm_module

    base = tmp_path / "cloud.qcow2"
    base.write_bytes(b"fake qcow2")

    class BaseVol:
        def info(self):
            return (0, 3 * 1024 ** 3, 0)

        def storagePoolLookupByVolume(self):
            return pool

    class TargetVol:
        def path(self):
            return "/var/lib/libvirt/images/test.qcow2"

        def info(self):
            return (0, 20 * 1024 ** 3, 0)

        def resize(self, size):
            pass

        def delete(self, flags=0):
            pass

    class Pool:
        def listAllVolumes(self):
            return []

        def createXML(self, xml, flags=0):
            self.created = xml
            return TargetVol()

    pool = Pool()

    class CreatedDomain:
        def UUIDString(self):
            return "uuid-cloud"

    class Conn:
        def storageVolLookupByPath(self, path):
            return BaseVol()

        def lookupByName(self, name):
            return CreatedDomain()

        def close(self):
            pass

    captured = []

    def fake_run(cmd, log, timeout=3600):
        captured.extend(cmd)

    driver = kvm_module.KvmDriver()
    monkeypatch.setattr(driver, "_conn", lambda: Conn())
    monkeypatch.setattr(kvm_module, "run_streaming", fake_run)

    result = driver._create_from_image({
        "name": "cloud-test", "vcpus": 2, "memory_mib": 2048,
        "image": str(base),
        "disks": [{"size_gib": 20, "pool": "default", "format": "qcow2"}],
        "networks": [{"network": "default", "model": "virtio"}],
        "cloud_init": "/var/lib/libvirt/images/seed.iso",
        "osinfo": "debian13",
    }, lambda line: None)

    assert result == "uuid-cloud"
    assert "backingStore" in pool.created
    assert "--import" in captured
    assert "path=/var/lib/libvirt/images/test.qcow2,format=qcow2" in captured
    assert "path=/var/lib/libvirt/images/seed.iso,device=cdrom" in captured
