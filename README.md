# Home Assistant add-on: LUKS Disks & Samba

Unlock LUKS-encrypted disks and manage Samba shares, users and permissions
from a panel in Home Assistant. It replaces Samba NAS2 and the manual
`drive.sh` workflow.

## Install

1. **Settings → Add-ons → Add-on store → ⋮ → Repositories**, then add this
   repository's URL.
2. Install **LUKS Disks & Samba**, turn **Protection mode off**, and start it.
3. Open **Disks & Shares** in the sidebar.

See [luks_samba/DOCS.md](luks_samba/DOCS.md) for details.

## How it works

| Step | What happens |
| --- | --- |
| Find disks | Reads sysfs and runs `blkid -p` on the kernel's live devtmpfs (mounted at `/hostdev`), so hot-plugged disks show up |
| Unlock | `cryptsetup open` inside the add-on. The passphrase goes through stdin and is never stored |
| Mount | `nsenter -t 1 -m mount /dev/dm-N <host media>/<name>`, in the host mount namespace like `drive.sh`. It then appears as `/media/<name>` in HA and every app |
| Share | `smb.conf` is generated from `/data/state.json`. Shares on locked disks get `available = no` |
| Lock | Close affected shares → `umount` → `rmdir` → `cryptsetup close` |

The add-on uses `host_pid`, so it can't use s6-overlay (which must be PID 1).
`python3 -m luks_samba run` supervises `smbd`/`nmbd` and serves the panel.

## Development

```sh
docker build -t luks-samba:dev luks_samba
```

Code lives in `luks_samba/rootfs/usr/src/app/luks_samba/`. Set
`LS_ALLOW_ANY_CLIENT=1` to reach the panel without Ingress when testing
locally.
