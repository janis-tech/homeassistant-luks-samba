"""Volumes, users and shares: the operations behind the panel."""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re

from . import disks, host, samba
from .state import (
    ACCESS_LEVELS, MEDIA_ROOT, SHARE_ROOTS, State, ValidationError, is_under, new_id,
    normalize_share_path, validate_mount_name, validate_mount_options, validate_password,
    validate_settings, validate_share_name, validate_username, validate_uuid,
)

_LOGGER = logging.getLogger(__name__)

# There is no udev in the container: let libdevmapper create its own node.
# We never rely on /dev/mapper paths - the host mounts /dev/dm-N instead.
DM_ENV = {"DM_DISABLE_UDEV": "1"}


class UserError(Exception):
    def __init__(self, message: str, details=None):
        super().__init__(message)
        self.details = details


def mapper_name(vol: dict) -> str:
    return "luks_" + re.sub(r"[^A-Za-z0-9_-]", "_", vol["mount_name"])


class Manager:
    def __init__(self, state: State):
        self.state = state
        self.lock = asyncio.Lock()

    # ---- helpers -----------------------------------------------------------

    async def host_path(self, vol: dict) -> str:
        return posixpath.join(await host.media_host_dir(), vol["mount_name"])

    async def is_mounted(self, vol: dict) -> bool:
        try:
            return host.host_mount_at(await self.host_path(vol)) is not None
        except Exception:  # no host access: fall back to what we can see
            return os.path.ismount(posixpath.join(MEDIA_ROOT, vol["mount_name"]))

    async def offline_volumes(self) -> set[str]:
        return {v["id"] for v in self.state.volumes if not await self.is_mounted(v)}

    async def apply_samba(self, extra_offline: set[str] = frozenset()) -> None:
        samba.write_conf(self.state, await self.offline_volumes() | set(extra_offline))
        await samba.reload()

    async def bootstrap(self) -> None:
        ok, reason = host.host_access()
        if not ok:
            _LOGGER.error("No host access: %s. Turn Protection mode OFF and restart.", reason)
        for user in self.state.users:
            await samba.ensure_unix_user(user["name"])
        known = {u["name"] for u in self.state.users}
        stale = await samba.passdb_users() - known
        for name in stale:
            _LOGGER.info("Removing Samba account %s (not in add-on state)", name)
            await samba.delete_user(name)
        samba.write_conf(self.state, await self.offline_volumes())

    # ---- status ------------------------------------------------------------

    async def status(self) -> dict:
        devices = await disks.scan()
        by_name = {d.name: d for d in devices}
        ok, reason = host.host_access()

        volumes = []
        for vol in self.state.volumes:
            dev = disks.find_uuid(devices, vol["uuid"])
            holder = disks.dm_holder(dev.name) if dev else None
            if not holder and vol["luks"]:
                dm = disks.dm_by_name(mapper_name(vol))
                holder = (dm, mapper_name(vol)) if dm else None
            inner = by_name.get(holder[0]) if holder else dev
            mounted = await self.is_mounted(vol)
            usage = None
            if mounted:
                try:
                    st = os.statvfs(posixpath.join(MEDIA_ROOT, vol["mount_name"]))
                    usage = {"total": st.f_blocks * st.f_frsize,
                             "free": st.f_bavail * st.f_frsize}
                except OSError:
                    pass
            volumes.append({
                **vol,
                "present": dev is not None,
                "device": dev.name if dev else "",
                "model": dev.model if dev else "",
                "size": dev.size if dev else 0,
                "unlocked": (holder is not None) if vol["luks"] else dev is not None,
                "mapper": holder[1] if holder else "",
                "fstype": inner.fstype if inner else "",
                "mounted": mounted,
                "path": posixpath.join(MEDIA_ROOT, vol["mount_name"]),
                "usage": usage,
                "shares": [s["name"] for s in self.state.shares
                           if samba.share_volume(s, self.state) is vol],
            })

        offline = {v["id"] for v in volumes if not v["mounted"]}
        shares = []
        for share in self.state.shares:
            vol = samba.share_volume(share, self.state)
            shares.append({**share, "volume": vol["id"] if vol else "",
                           "online": not (vol and vol["id"] in offline)})

        system_disks = {d.parent or d.name for d in devices if d.label.startswith("hassos")}
        configured = {v["uuid"].lower() for v in self.state.volumes}
        candidates = [
            {**d.as_dict(), "system": (d.parent or d.name) in system_disks,
             "configured": d.uuid.lower() in configured}
            for d in devices
            if d.kind != "dm" and d.uuid and d.fstype not in ("", "partitioned", "swap")
        ]

        return {
            "host_access": {"ok": ok, "reason": reason},
            "settings": self.state.settings,
            "volumes": volumes,
            "shares": shares,
            "users": [{"name": u["name"],
                       "shares": [s["name"] for s in self.state.shares
                                  if u["name"] in s.get("access", {})]}
                      for u in self.state.users],
            "disks": candidates,
            "share_roots": [r for r in SHARE_ROOTS if os.path.isdir(r)],
        }

    # ---- unlock / lock -----------------------------------------------------

    def _volume(self, vol_id: str) -> dict:
        vol = self.state.volume(vol_id)
        if not vol:
            raise UserError("Unknown volume")
        return vol

    async def unlock(self, vol_id: str, passphrase: str | None) -> None:
        async with self.lock:
            vol = self._volume(vol_id)
            devices = await disks.scan()
            dev = disks.find_uuid(devices, vol["uuid"])
            if not dev:
                raise UserError(f"Disk with UUID {vol['uuid']} not found. Is it plugged in?")

            opened = None
            if vol["luks"]:
                if dev.fstype != "crypto_LUKS":
                    raise UserError(f"/dev/{dev.name} is not a LUKS device ({dev.fstype or 'unknown'})")
                holder = disks.dm_holder(dev.name)
                if not holder:
                    if not passphrase:
                        raise UserError("Passphrase required")
                    mapper = mapper_name(vol)
                    if disks.dm_by_name(mapper):
                        raise UserError(f"Mapper name {mapper} is already used by another device")
                    _LOGGER.info("Unlocking /dev/%s as %s", dev.name, mapper)
                    res = await host.run(
                        "cryptsetup", "open", "--type", "luks", "--key-file=-",
                        posixpath.join(host.HOSTDEV, dev.name), mapper,
                        stdin=passphrase.encode(), env=DM_ENV, check=False, timeout=300,
                    )
                    if res.rc == 2:
                        raise UserError("Wrong passphrase")
                    if res.rc != 0:
                        raise UserError(f"cryptsetup failed: {res.err.strip() or res.rc}")
                    opened = mapper
                    holder = disks.dm_holder(dev.name)
                    if not holder:
                        dm = disks.dm_by_name(mapper)
                        if not dm:
                            raise UserError("Device unlocked but mapper device not found")
                        holder = (dm, mapper)
                source = f"/dev/{holder[0]}"
            else:
                source = f"/dev/{dev.name}"

            host_path = await self.host_path(vol)
            if host.host_mount_at(host_path) is None:
                _LOGGER.info("Mounting %s at %s", source, host_path)
                args = ["mount"]
                if vol.get("mount_options"):
                    args += ["-o", vol["mount_options"]]
                args += [source, host_path]
                try:
                    await host.host_run("mkdir", "-p", host_path)
                    await host.host_run(*args)
                except host.CommandError as err:
                    await host.host_run("rmdir", host_path, check=False)
                    if opened:
                        await host.run("cryptsetup", "close", opened, env=DM_ENV, check=False)
                    raise UserError(f"Mount failed: {err.stderr}") from err
            await self.apply_samba()

    async def lock_volume(self, vol_id: str) -> None:
        async with self.lock:
            vol = self._volume(vol_id)
            host_path = await self.host_path(vol)
            local_path = posixpath.join(MEDIA_ROOT, vol["mount_name"])

            if host.host_mount_at(host_path) is not None:
                # Take shares on the disk offline and kick their clients first,
                # plus parent shares (e.g. "media") that may hold files open.
                related = [s for s in self.state.shares
                           if is_under(s["path"], local_path) or is_under(local_path, s["path"])]
                await self.apply_samba(extra_offline={vol["id"]})
                for share in related:
                    await samba.close_share(share["name"])

                _LOGGER.info("Unmounting %s", host_path)
                err = ""
                for _ in range(3):
                    res = await host.host_run("umount", host_path, check=False)
                    if res.rc == 0:
                        break
                    err = res.err.strip()
                    await asyncio.sleep(1)
                else:
                    procs = await host.blockers(host_path)
                    await self.apply_samba()
                    raise UserError(
                        "Unmount failed - the disk is busy. Stop the apps using "
                        f"{local_path} and try again. ({err})",
                        details={"processes": procs},
                    )
            # Remove the empty folder so nothing gets written to the system disk.
            await host.host_run("rmdir", host_path, check=False)

            if vol["luks"]:
                dev = disks.find_uuid(await disks.scan(), vol["uuid"])
                holder = disks.dm_holder(dev.name) if dev else None
                name = holder[1] if holder else (
                    mapper_name(vol) if disks.dm_by_name(mapper_name(vol)) else None)
                if name:
                    _LOGGER.info("Locking %s", name)
                    res = await host.run("cryptsetup", "close", name, env=DM_ENV, check=False)
                    if res.rc != 0:
                        raise UserError(f"Lock failed: {res.err.strip()}")
            await self.apply_samba()

    # ---- volumes -----------------------------------------------------------

    def _volume_fields(self, data: dict, current: dict | None) -> dict:
        vol = {
            "name": (data.get("name") or "").strip()[:64] or data.get("mount_name", ""),
            "uuid": validate_uuid(data.get("uuid")),
            "luks": bool(data.get("luks", True)),
            "mount_name": validate_mount_name(data.get("mount_name")),
            "mount_options": validate_mount_options(data.get("mount_options", "")),
        }
        for other in self.state.volumes:
            if current and other["id"] == current["id"]:
                continue
            if other["mount_name"].lower() == vol["mount_name"].lower():
                raise ValidationError("Another volume already uses that mount folder")
            if other["uuid"].lower() == vol["uuid"].lower():
                raise ValidationError("That disk is already configured")
        return vol

    async def add_volume(self, data: dict) -> dict:
        async with self.lock:
            vol = {"id": new_id(), **self._volume_fields(data, None)}
            self.state.volumes.append(vol)
            self.state.save()
            await self.apply_samba()
            return vol

    async def update_volume(self, vol_id: str, data: dict) -> None:
        async with self.lock:
            vol = self._volume(vol_id)
            if await self.is_mounted(vol):
                raise UserError("Lock the disk before changing its settings")
            vol.update(self._volume_fields(data, vol))
            self.state.save()
            await self.apply_samba()

    async def delete_volume(self, vol_id: str) -> None:
        async with self.lock:
            vol = self._volume(vol_id)
            if await self.is_mounted(vol):
                raise UserError("Lock the disk before removing it")
            used = [s["name"] for s in self.state.shares if samba.share_volume(s, self.state) is vol]
            if used:
                raise UserError("Remove the shares on this disk first: " + ", ".join(used))
            self.state.volumes.remove(vol)
            self.state.save()

    # ---- users -------------------------------------------------------------

    async def add_user(self, data: dict) -> None:
        async with self.lock:
            name = validate_username(data.get("name"))
            password = validate_password(data.get("password"))
            if self.state.user(name):
                raise ValidationError("User already exists")
            await samba.set_password(name, password)
            self.state.users.append({"name": name})
            self.state.save()
            await self.apply_samba()

    async def set_password(self, name: str, data: dict) -> None:
        async with self.lock:
            if not self.state.user(name):
                raise UserError("Unknown user")
            await samba.set_password(name, validate_password(data.get("password")))

    async def delete_user(self, name: str) -> None:
        async with self.lock:
            user = self.state.user(name)
            if not user:
                raise UserError("Unknown user")
            for share in self.state.shares:
                share.get("access", {}).pop(name, None)
            self.state.users.remove(user)
            self.state.save()
            await self.apply_samba()
            await samba.delete_user(name)

    # ---- shares ------------------------------------------------------------

    def _share_fields(self, data: dict, current: dict | None) -> dict:
        name = validate_share_name(data.get("name"))
        for other in self.state.shares:
            if other is not current and other["name"].lower() == name.lower():
                raise ValidationError("A share with that name already exists")
        access = {}
        for user, level in (data.get("access") or {}).items():
            if level in (None, "", "none"):
                continue
            if not self.state.user(user):
                raise ValidationError(f"Unknown user {user}")
            if level not in ACCESS_LEVELS:
                raise ValidationError("Access must be ro, rw or none")
            access[user] = level
        guest = data.get("guest") or "none"
        if guest not in ("none", *ACCESS_LEVELS):
            raise ValidationError("Guest access must be ro, rw or none")
        comment = str(data.get("comment") or "")
        return {
            "name": name,
            "path": normalize_share_path(data.get("path"), self.state.mount_names()),
            "comment": re.sub(r"[\x00-\x1f\x7f]", "", comment).strip()[:128],
            "browseable": bool(data.get("browseable", True)),
            "access": access,
            "guest": guest,
        }

    async def add_share(self, data: dict) -> dict:
        async with self.lock:
            share = {"id": new_id(), **self._share_fields(data, None)}
            self.state.shares.append(share)
            self.state.save()
            await self.apply_samba()
            return share

    async def update_share(self, share_id: str, data: dict) -> None:
        async with self.lock:
            share = self.state.share(share_id)
            if not share:
                raise UserError("Unknown share")
            old_name = share["name"]
            share.update(self._share_fields(data, share))
            self.state.save()
            await self.apply_samba()
            if old_name != share["name"]:
                await samba.close_share(old_name)

    async def delete_share(self, share_id: str) -> None:
        async with self.lock:
            share = self.state.share(share_id)
            if not share:
                raise UserError("Unknown share")
            self.state.shares.remove(share)
            self.state.save()
            await self.apply_samba()
            await samba.close_share(share["name"])

    # ---- settings ----------------------------------------------------------

    async def update_settings(self, data: dict) -> None:
        async with self.lock:
            self.state.data["settings"] = validate_settings({**self.state.settings, **data})
            self.state.save()
            await self.apply_samba()
