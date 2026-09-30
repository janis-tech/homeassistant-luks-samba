"""Block device discovery straight from sysfs + low-level blkid probes.

We avoid relying on the udev database or the container's /dev snapshot, so
disks plugged in after the add-on started are found too.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass, field

from . import host

SYS_BLOCK = "/sys/class/block"
_SKIP = re.compile(r"^(loop|ram|zram|nbd|sr|fd)\d*|^mmcblk\d+(boot\d|rpmb)$")


@dataclass
class BlockDevice:
    name: str
    kind: str                     # disk | part | dm
    size: int
    parent: str = ""
    model: str = ""
    removable: bool = False
    fstype: str = ""
    uuid: str = ""
    label: str = ""
    dm_name: str = ""
    holders: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _sysfs_devices() -> list[BlockDevice]:
    devices = []
    for name in sorted(os.listdir(SYS_BLOCK)):
        if _SKIP.match(name):
            continue
        base = os.path.join(SYS_BLOCK, name)
        size = int(_read(f"{base}/size") or 0) * 512
        if size == 0:
            continue
        if name.startswith("dm-"):
            kind, parent = "dm", ""
        elif os.path.exists(f"{base}/partition"):
            kind = "part"
            parent = os.path.basename(os.path.dirname(os.path.realpath(base)))
        else:
            kind, parent = "disk", ""
        disk = parent or name
        model = " ".join(filter(None, (
            _read(f"{SYS_BLOCK}/{disk}/device/vendor"),
            _read(f"{SYS_BLOCK}/{disk}/device/model"),
        )))
        try:
            holders = sorted(os.listdir(f"{base}/holders"))
        except OSError:
            holders = []
        devices.append(BlockDevice(
            name=name, kind=kind, size=size, parent=parent, model=model,
            removable=_read(f"{SYS_BLOCK}/{disk}/removable") == "1",
            dm_name=_read(f"{base}/dm/name") if kind == "dm" else "",
            holders=holders,
        ))
    return devices


async def _probe(dev: BlockDevice, sem: asyncio.Semaphore) -> None:
    async with sem:
        res = await host.run("blkid", "-p", "-o", "export",
                             os.path.join(host.HOSTDEV, dev.name), check=False, timeout=20)
    if res.rc != 0:
        return
    info = dict(line.split("=", 1) for line in res.out.splitlines() if "=" in line)
    dev.fstype = info.get("TYPE", "") or ("partitioned" if info.get("PTTYPE") else "")
    dev.uuid = info.get("UUID", "")
    dev.label = info.get("LABEL", "")


async def scan() -> list[BlockDevice]:
    devices = _sysfs_devices()
    sem = asyncio.Semaphore(4)
    await asyncio.gather(*(_probe(d, sem) for d in devices))
    return devices


def find_uuid(devices: list[BlockDevice], uuid: str) -> BlockDevice | None:
    uuid = uuid.lower()
    return next((d for d in devices if d.uuid.lower() == uuid and d.kind != "dm"), None)


def dm_holder(dev_name: str) -> tuple[str, str] | None:
    """(dm-N, mapper name) of the device-mapper device sitting on dev_name."""
    try:
        holders = os.listdir(f"{SYS_BLOCK}/{dev_name}/holders")
    except OSError:
        return None
    for h in holders:
        if h.startswith("dm-"):
            return h, _read(f"{SYS_BLOCK}/{h}/dm/name")
    return None


def dm_by_name(mapper: str) -> str | None:
    for name in os.listdir(SYS_BLOCK):
        if name.startswith("dm-") and _read(f"{SYS_BLOCK}/{name}/dm/name") == mapper:
            return name
    return None
