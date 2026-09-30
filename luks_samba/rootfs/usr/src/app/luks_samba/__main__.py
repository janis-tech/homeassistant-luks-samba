"""Entry point and process supervisor.

The add-on runs with `host_pid: true`, so it can never be PID 1 and
s6-overlay (which insists on being PID 1) cannot be used. This process
prepares the container, keeps smbd/nmbd running and serves the panel.

    python3 -m luks_samba run        # everything (container entrypoint)
    python3 -m luks_samba bootstrap  # prepare state + smb.conf only
    python3 -m luks_samba serve      # panel only
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys

import aiohttp
from aiohttp import web

from . import host
from .manager import Manager
from .server import build_app
from .state import State

_LOGGER = logging.getLogger("luks_samba")

OPTIONS_FILE = "/data/options.json"
RUNTIME_DIRS = ["/data/samba", "/run/samba", "/run/cryptsetup", "/var/lib/samba/private",
                "/var/cache/samba", "/var/log/samba", host.HOSTDEV]
SMB_ARGS = ["--foreground", "--debug-stdout", "--no-process-group", "-s", "/etc/samba/smb.conf"]


def load_options() -> dict:
    try:
        with open(OPTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s (%(name)s) %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def prepare() -> None:
    for path in RUNTIME_DIRS:
        os.makedirs(path, exist_ok=True)
    # A privileged container's /dev is a snapshot taken at start, so disks
    # plugged in later never show up there. devtmpfs is the kernel's single
    # live /dev instance - the same one the host sees.
    if not os.path.ismount(host.HOSTDEV):
        res = await host.run("mount", "-t", "devtmpfs", "devtmpfs", host.HOSTDEV, check=False)
        if res.rc != 0:
            _LOGGER.error("Cannot mount devtmpfs (is Protection mode OFF?): %s", res.err.strip())


async def ingress_port() -> int:
    if os.environ.get("LS_PORT"):
        return int(os.environ["LS_PORT"])
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "http://supervisor/addons/self/info",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    return int(data["data"]["ingress_port"])
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Could not get ingress port from Supervisor: %s", err)
    return 8099


async def keep_running(name: str, args: list[str]) -> None:
    """Run a daemon forever, restarting it with backoff if it exits."""
    delay = 1
    while True:
        _LOGGER.info("Starting %s", name)
        # Own session: on exit smbd signals its whole process group.
        proc = await asyncio.create_subprocess_exec(*args, start_new_session=True)
        try:
            rc = await proc.wait()
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 10)
                except asyncio.TimeoutError:
                    proc.kill()
            raise
        _LOGGER.warning("%s exited with %s, restarting in %ss", name, rc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


async def run_all() -> None:
    await prepare()
    manager = Manager(State())
    await manager.bootstrap()

    port = await ingress_port()
    runner = web.AppRunner(build_app(manager), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, os.environ.get("LS_HOST", "0.0.0.0"), port).start()
    _LOGGER.info("Panel listening on port %s", port)

    daemons = [
        asyncio.create_task(keep_running("smbd", ["smbd", *SMB_ARGS])),
        asyncio.create_task(keep_running("nmbd", ["nmbd", *SMB_ARGS])),
    ]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    _LOGGER.info("Stopping (unlocked disks stay mounted)")
    for task in daemons:
        task.cancel()
    await asyncio.gather(*daemons, return_exceptions=True)
    await runner.cleanup()


def main() -> None:
    options = load_options()
    setup_logging(os.environ.get("LS_LOG_LEVEL") or options.get("log_level", "info"))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "run":
        asyncio.run(run_all())
    elif cmd == "bootstrap":
        async def _bootstrap():
            await prepare()
            await Manager(State()).bootstrap()
        asyncio.run(_bootstrap())
    elif cmd == "serve":
        manager = Manager(State())
        web.run_app(build_app(manager), host=os.environ.get("LS_HOST", "0.0.0.0"),
                    port=int(os.environ.get("LS_PORT", "8099")), print=None)
    else:
        sys.exit(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
