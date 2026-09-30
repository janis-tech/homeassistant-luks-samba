"""Persistent add-on state (/data/state.json) and input validation.

Passwords and LUKS passphrases are never stored here: Samba passwords live in
Samba's own passdb, and passphrases are only held for the duration of an unlock.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import secrets
import tempfile

STATE_FILE = os.environ.get("LS_STATE_FILE", "/data/state.json")

# Folders mapped into the add-on that may be shared.
SHARE_ROOTS = [
    "/media",
    "/share",
    "/homeassistant",
    "/addon_configs",
    "/addons",
    "/backup",
    "/ssl",
]
MEDIA_ROOT = "/media"

DEFAULT_SETTINGS = {
    "workgroup": "WORKGROUP",
    "server_string": "Home Assistant",
    "hosts_allow": "10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.1 fe80::/10 fc00::/7",
    "min_protocol": "SMB2_10",
    "macos": False,
    "veto_junk": True,
}
MIN_PROTOCOLS = ["NT1", "SMB2_02", "SMB2_10", "SMB3_00", "SMB3_11"]

RESERVED_SHARES = {"global", "homes", "printers", "print$", "ipc$"}
RESERVED_USERS = {
    "root", "nobody", "daemon", "bin", "sys", "adm", "guest", "admin",
    "administrator", "samba", "smbd", "shutdown", "halt", "operator",
}

ACCESS_LEVELS = ("ro", "rw")


class ValidationError(ValueError):
    pass


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise ValidationError(msg)


def _single_line(value, field: str, max_len: int = 256) -> str:
    _check(isinstance(value, str), f"{field} must be text")
    value = value.strip()
    _check(len(value) <= max_len, f"{field} is too long")
    _check(not re.search(r"[\x00-\x1f\x7f]", value), f"{field} contains control characters")
    return value


def validate_mount_name(value) -> str:
    value = _single_line(value, "Mount folder", 64)
    _check(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) is not None,
        "Mount folder may only contain letters, digits, '.', '_' and '-'",
    )
    return value


def validate_uuid(value) -> str:
    value = _single_line(value, "UUID", 64)
    _check(re.fullmatch(r"[A-Za-z0-9-]{4,64}", value) is not None, "Invalid UUID")
    return value


def validate_mount_options(value) -> str:
    value = _single_line(value or "", "Mount options", 200)
    _check(
        re.fullmatch(r"[A-Za-z0-9,=._:/-]*", value) is not None,
        "Mount options may only contain letters, digits and , = . _ : / -",
    )
    return value


def validate_username(value) -> str:
    value = _single_line(value, "Username", 32).lower()
    _check(
        re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value) is not None,
        "Username must start with a letter and contain only a-z, 0-9, '_' or '-'",
    )
    _check(value not in RESERVED_USERS, f"'{value}' is a reserved name")
    return value


def validate_password(value) -> str:
    _check(isinstance(value, str), "Password must be text")
    _check(4 <= len(value) <= 128, "Password must be 4-128 characters")
    _check("\n" not in value and "\r" not in value and "\x00" not in value,
           "Password contains invalid characters")
    return value


def validate_share_name(value) -> str:
    value = _single_line(value, "Share name", 80)
    _check(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}", value) is not None,
        "Share name may only contain letters, digits, spaces and . _ -",
    )
    _check(value.lower() not in RESERVED_SHARES, f"'{value}' is a reserved share name")
    return value


def normalize_share_path(value, mount_names: list[str]) -> str:
    """Return a safe absolute path inside SHARE_ROOTS.

    A path on a configured volume is accepted even while the volume is locked
    (it does not exist yet); everything else must be an existing directory.
    """
    value = _single_line(value, "Path", 1024)
    _check(value.startswith("/"), "Path must be absolute")
    _check(".." not in value.split("/"), "Path must not contain '..'")
    path = posixpath.normpath(value)
    if os.path.exists(path):
        path = os.path.realpath(path)
        _check(os.path.isdir(path), "Path is not a folder")
    else:
        _check(
            any(is_under(path, posixpath.join(MEDIA_ROOT, m)) for m in mount_names),
            "Folder does not exist",
        )
    _check(any(is_under(path, root) for root in SHARE_ROOTS),
           "Path must be inside " + ", ".join(SHARE_ROOTS))
    return path


def is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def validate_settings(data: dict) -> dict:
    out = dict(DEFAULT_SETTINGS)
    if "workgroup" in data:
        wg = _single_line(data["workgroup"], "Workgroup", 15)
        _check(re.fullmatch(r"[A-Za-z0-9_-]{1,15}", wg) is not None, "Invalid workgroup")
        out["workgroup"] = wg.upper()
    if "server_string" in data:
        out["server_string"] = _single_line(data["server_string"], "Server description", 64)
    if "hosts_allow" in data:
        ha = _single_line(data["hosts_allow"], "Allowed hosts", 512)
        _check(re.fullmatch(r"[0-9A-Za-z.:/ _-]*", ha) is not None, "Invalid allowed hosts")
        out["hosts_allow"] = " ".join(ha.split())
    if "min_protocol" in data:
        _check(data["min_protocol"] in MIN_PROTOCOLS, "Invalid minimum protocol")
        out["min_protocol"] = data["min_protocol"]
    for key in ("macos", "veto_junk"):
        if key in data:
            out[key] = bool(data[key])
    return out


def new_id() -> str:
    return secrets.token_hex(4)


class State:
    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self.data = {"version": 1, "settings": dict(DEFAULT_SETTINGS),
                     "volumes": [], "users": [], "shares": []}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except FileNotFoundError:
            return
        for key in ("volumes", "users", "shares"):
            self.data[key] = loaded.get(key, [])
        self.data["settings"] = {**DEFAULT_SETTINGS, **loaded.get("settings", {})}

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
        os.replace(tmp, self.path)

    @property
    def settings(self) -> dict:
        return self.data["settings"]

    @property
    def volumes(self) -> list[dict]:
        return self.data["volumes"]

    @property
    def users(self) -> list[dict]:
        return self.data["users"]

    @property
    def shares(self) -> list[dict]:
        return self.data["shares"]

    def mount_names(self) -> list[str]:
        return [v["mount_name"] for v in self.volumes]

    def volume(self, vol_id: str) -> dict | None:
        return next((v for v in self.volumes if v["id"] == vol_id), None)

    def user(self, name: str) -> dict | None:
        return next((u for u in self.users if u["name"] == name), None)

    def share(self, share_id: str) -> dict | None:
        return next((s for s in self.shares if s["id"] == share_id), None)
