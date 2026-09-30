# LUKS Disks & Samba

Unlock LUKS-encrypted disks and share folders over SMB, all from the
**Disks & Shares** panel in the Home Assistant sidebar. This add-on includes
its own Samba server, so you don't need the Samba share or Samba NAS2 add-ons.

## Before you start

1. **Turn Protection mode OFF** on the add-on's Info tab. The add-on needs to
   mount disks in the host's mount namespace so `/media/<name>` is visible to
   Home Assistant and to other apps (Frigate, Plex, ...). The panel shows a
   warning banner if it can't reach the host.
2. **Stop and disable any other Samba add-on** (Samba share, Samba NAS2). Only
   one SMB server can listen on ports 445/139.
3. Start the add-on and open **Disks & Shares** in the sidebar.

## Disks

*Detected disks* lists every partition that has a filesystem. LUKS partitions
are tagged `LUKS`. The Home Assistant system disk is hidden by default.

Click **Add** on a disk and choose a mount folder, for example `Storage`. The
disk will be mounted at `/media/Storage`, which is where `drive.sh` put it.

- **Unlock** asks for the passphrase, opens the LUKS container and mounts it.
  The passphrase is sent to `cryptsetup` once and is never stored or logged.
- **Lock** takes the disk's shares offline, disconnects SMB clients, unmounts
  the disk and closes the LUKS container. It is then safe to unplug. If another
  app is still using the disk, the panel shows which processes are holding it.
- Non-encrypted disks work too: untick *LUKS encrypted* and use Mount/Unmount.

Unlocked disks **stay mounted when the add-on restarts or updates**. After a
reboot of the host they are locked, and you unlock them again from the panel.

### Coming from drive.sh

The add-on finds LUKS containers that are already open, whatever their mapper
name (for example `securedisk`). If `drive.sh` already unlocked your disk, the
panel shows it as unlocked and **Mount** or **Lock** work as usual. You can
stop using `drive.sh` and Samba NAS2.

## Users

Samba users are separate from Home Assistant users. Create them on the
**Users** tab. Passwords are stored only in Samba's password database
(`/data/samba/passdb.tdb`, as hashes), which is part of the add-on's backup.

## Shares

A share is a folder plus the users allowed to use it, each with
*Read only* or *Read / write* access. You can share folders from:

`/media` (including unlocked disks), `/share`, `/homeassistant` (config),
`/addon_configs`, `/addons`, `/backup`, `/ssl`

- Shares on a locked disk are switched off automatically. Nothing can be
  written into the empty mount folder on your system disk.
- A share with no users and no guest access is disabled.
- **Guest access** (no password) can be set per share to *Read only* or
  *Read / write*, for TVs and media players that can't log in. Anyone on
  your network (within *Allowed hosts*) can then use that share. Guest
  logins are only enabled while at least one share allows them. Windows
  10/11 block guest logins by default, so use a user account there.
- Connect with `\\<home-assistant-ip>\<share>` (Windows) or
  `smb://<home-assistant-ip>/<share>` (macOS/Linux).

## Settings

| Setting | Meaning |
| --- | --- |
| Workgroup | Windows workgroup name |
| Allowed hosts | Networks that may connect. The default covers private LAN ranges. Leave empty to allow everyone. |
| Minimum SMB protocol | Raise it to `SMB3_00` if all your clients support it. Use `NT1` only for very old devices. |
| macOS compatibility | Enables Apple's SMB extensions (`fruit`) for Finder and Time Machine-style metadata |
| Hide junk files | Hides and cleans up `._*`, `.DS_Store`, `Thumbs.db` |

## Security notes

- This add-on runs with full hardware access, in the host's PID namespace,
  without AppArmor. This is required to unlock and mount disks. Only install it
  if you trust the code.
- The panel is available only to Home Assistant administrators, through
  Ingress. Direct connections to the panel port are refused.
- **Backups:** if a Home Assistant backup includes the *Media* folder while a
  disk is unlocked, the backup contains that disk's data **unencrypted**.
  Exclude Media from backups, or lock the disk first.

## Troubleshooting

- *"Disk not found"*: the disk isn't connected, or the UUID changed (for
  example after reformatting). Edit the disk and choose the new UUID.
- *"Unmount failed - the disk is busy"*: stop the listed apps, or any terminal
  session that has `cd`'d into the disk, then lock again.
- *Can't connect to shares*: check that no other Samba add-on is running and
  that your client's IP is covered by *Allowed hosts*.
