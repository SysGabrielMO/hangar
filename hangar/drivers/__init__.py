from .base import (DeletePlan, Disk, Driver, DriverError, Interface, Machine, Snapshot, Usage,
                   User)
from .docker_driver import DockerDriver
from .kvm import KvmDriver
from .lxc import LxcDriver

DRIVERS: dict[str, Driver] = {d.kind: d for d in (KvmDriver(), LxcDriver(), DockerDriver())}

__all__ = ["DRIVERS", "DeletePlan", "Driver", "DriverError", "Interface", "Machine", "Snapshot", "Usage", "User", "Disk"]
