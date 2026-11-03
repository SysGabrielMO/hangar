from hangar import db


def test_iface_ip_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hangar.db")

    assert db.get_iface_ips("kvm", "vm-1") == {}
    db.save_iface_ip("kvm", "vm-1", "52:54:00:00:00:01", "10.0.0.5/24")
    assert db.get_iface_ips("kvm", "vm-1") == {"52:54:00:00:00:01": "10.0.0.5/24"}

    db.save_iface_ip("kvm", "vm-1", "52:54:00:00:00:01", "")
    assert db.get_iface_ips("kvm", "vm-1") == {}


def test_delete_iface_ips(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hangar.db")

    db.save_iface_ip("kvm", "vm-1", "mac", "10.0.0.5")
    db.save_iface_ip("kvm", "vm-2", "mac", "10.0.0.6")
    db.delete_iface_ips("kvm", "vm-1")
    assert db.get_iface_ips("kvm", "vm-1") == {}
    assert db.get_iface_ips("kvm", "vm-2") == {"mac": "10.0.0.6"}


def test_last_ip_and_all_iface_ips(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hangar.db")

    db.save_iface_ip("kvm", "vm-1", "mac-1", "10.0.0.5")
    db.save_iface_ip("kvm", "vm-1", "mac-2", "10.0.0.6")
    assert set(db.all_iface_ips()[("kvm", "vm-1")]) == {"10.0.0.5", "10.0.0.6"}

    db.save_last_ips("kvm", "vm-2", ["192.168.1.10"])
    assert db.all_last_ips()[("kvm", "vm-2")] == ["192.168.1.10"]

    db.delete_iface_ips("kvm", "vm-2")
    assert db.all_last_ips() == {}
