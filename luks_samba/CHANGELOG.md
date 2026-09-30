# Changelog

## 0.1.2

- Fix user access selectors not rendering in the share dialog.
- Panel banner names missing capabilities.
- Per-share guest access (none / read only / read-write).

## 0.1.1

- Request SYS_ADMIN/SYS_PTRACE/SYS_RAWIO/DAC_READ_SEARCH explicitly: on current
  Supervisor versions `full_access` does not make the container privileged.

## 0.1.0

- First release: unlock/lock LUKS disks, mount at `/media/<name>`, built-in
  Samba server with per-user read/write shares, Ingress panel.
