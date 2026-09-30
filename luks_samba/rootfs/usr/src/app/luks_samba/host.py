"""Helpers for running commands and acting in the host's mount namespace.

With `host_pid: true` PID 1 is the host init, so `nsenter -t 1 -m` runs a
command where Home Assistant and every other app see mounts - the same trick
drive.sh used through `docker run --pid=host`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

HOSTDEV = os.environ.get("LS_HOSTDEV", "/hostdev")
MEDIA_ROOT = "/media"

# Fallbacks if the host path of /media cannot be derived from mountinfo.
_MEDIA_CANDIDATES = [
    "/mnt/data/supervisor/media",      # Home Assistant OS
    "/usr/share/hassio/media",         # Supervised (Debian)
    "/var/lib/homeassistant/media",
]


class CommandError(RuntimeError):
    def __init__(self, cmd: str, rc: int, stderr: str):
        self.cmd, self.rc, self.stderr = cmd, rc, stderr.strip()
        super().__init__(f"{cmd} failed ({rc}): {self.stderr}")


@dataclass
class Result:
    rc: int
    out: str
    err: str


async def run(*args: str, stdin: bytes | None = None, env: dict | None = None,
              check: bool = True, timeout: float = 120) -> Result:
    """Run a command without a shell. stdin is never logged."""
    _LOGGER.debug("run: %s", " ".join(args))
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise CommandError(args[0], -1, "timed out")
    res = Result(proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"))
    if check and res.rc != 0:
        raise CommandError(args[0], res.rc, res.err or res.out)
    return res


async def host_run(*args: str, check: bool = True) -> Result:
    """Run a host binary inside the host's mount namespace."""
    res = await run("nsenter", "-t", "1", "-m", "--", *args, check=False)
    if check and res.rc != 0:
        raise CommandError(args[0], res.rc, res.err or res.out)
    return res


_CAPS = {19: "SYS_PTRACE", 21: "SYS_ADMIN", 17: "SYS_RAWIO", 2: "DAC_READ_SEARCH"}


def _missing_caps() -> tuple[list[str], str]:
    """Required capabilities missing from our effective set, plus raw CapEff."""
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            capeff = next(l.split()[1] for l in fh if l.startswith("CapEff:"))
    except (OSError, StopIteration):
        return [], "?"
    mask = int(capeff, 16)
    return [name for bit, name in _CAPS.items() if not mask & (1 << bit)], capeff


def host_access() -> tuple[bool, str]:
    """Check that we can see and enter the host's mount namespace."""
    missing, capeff = _missing_caps()
    try:
        if os.readlink("/proc/1/ns/mnt") == os.readlink("/proc/self/ns/mnt"):
            return False, "PID 1 is not the host init (host_pid not active)"
    except PermissionError:
        if missing:
            return False, (f"Missing capabilities {', '.join(missing)} (CapEff={capeff}) - "
                           "make sure Protection mode is OFF and the add-on is up to date")
        return False, f"Permission denied reading host PID 1 despite capabilities (CapEff={capeff})"
    except OSError as err:
        return False, f"Cannot inspect host namespaces: {err.strerror}"
    if missing:
        return False, f"Missing capabilities {', '.join(missing)} (CapEff={capeff})"
    if not os.path.isdir(posixpath.join(HOSTDEV, "mapper")):
        return False, f"{HOSTDEV} is not a devtmpfs mount"
    return True, ""


def _unescape(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(pid: str = "1") -> list[dict]:
    mounts = []
    try:
        with open(f"/proc/{pid}/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                lf, rf = left.split(), right.split()
                mounts.append({
                    "devno": lf[2],
                    "root": _unescape(lf[3]),
                    "mountpoint": _unescape(lf[4]),
                    "fstype": rf[0] if rf else "",
                    "source": _unescape(rf[1]) if len(rf) > 1 else "",
                })
    except OSError as err:
        _LOGGER.warning("Cannot read /proc/%s/mountinfo: %s", pid, err)
    return mounts


def host_mount_at(path: str) -> dict | None:
    """Return the host mount whose mountpoint is exactly `path` (last wins)."""
    found = None
    for m in parse_mountinfo():
        if m["mountpoint"] == path:
            found = m
    return found


_media_host_dir: str | None = None


async def media_host_dir() -> str:
    """Find where our /media lives on the host (e.g. /mnt/data/supervisor/media).

    Our /media is a bind mount; its mountinfo `root` is the path inside the
    backing filesystem. Find where the host mounts that filesystem and join.
    """
    global _media_host_dir
    if _media_host_dir:
        return _media_host_dir

    candidates: list[str] = []
    ours = next((m for m in parse_mountinfo("self") if m["mountpoint"] == MEDIA_ROOT), None)
    if ours:
        for m in parse_mountinfo():
            if m["devno"] != ours["devno"] or m["mountpoint"].startswith(("/var/lib/docker", "/run")):
                continue
            root = m["root"].rstrip("/")
            if ours["root"] == root or ours["root"].startswith(root + "/"):
                candidates.append(posixpath.normpath(m["mountpoint"] + ours["root"][len(root):]))
    candidates.sort(key=len)
    candidates += _MEDIA_CANDIDATES

    for path in candidates:
        if (await host_run("test", "-d", path, check=False)).rc == 0:
            _LOGGER.info("Host media folder: %s", path)
            _media_host_dir = path
            return path
    raise RuntimeError("Could not find the host path of /media")


async def blockers(host_path: str) -> list[dict]:
    """List processes keeping the host filesystem at `host_path` busy."""
    res = await run("fuser", "-m", f"/proc/1/root{host_path}", check=False)
    procs = []
    for pid in sorted({int(p) for p in re.findall(r"\d+", res.out)}):
        if pid == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
                comm = fh.read().strip()
        except OSError:
            comm = "?"
        procs.append({"pid": pid, "name": comm, "container": _container_of(pid)})
    return procs


def _container_of(pid: int) -> str:
    """Best-effort: short docker container id of a host process."""
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as fh:
            m = re.search(r"docker[-/]([0-9a-f]{12})", fh.read())
        return m.group(1) if m else ""
    except OSError:
        return ""
