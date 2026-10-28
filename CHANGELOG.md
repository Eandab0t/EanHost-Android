# Changelog

All notable changes to EanHost Android are recorded here. This project uses
[Semantic Versioning](https://semver.org/).

## v1.0.0-beta.1 — 2026-10-28

First tagged beta: the fleet dashboard, device workers, bot runner, and Fast
Mode are all in place and validated against a four-tablet fleet.

### Fixed

- **File browser on devices with a stripped-down `ls`.** Some Android/BusyBox
  builds omit the link-count column (and the size for directories), which made
  the panel fall back to printing raw `ls -l` lines. The parser now treats both
  columns as optional, handles CRLF, and types directories and symlinks
  correctly.
- **Logout left the session valid.** `POST /api/logout` now revokes the token and
  expires the `ean_sess` cookie, so the old credentials return 401 immediately.
- **Fast Mode tunnel could stay stale.** A failed `adb forward` is detected and
  the cached tunnel is dropped, so the next request re-creates it after a device
  reconnect or adb-server restart.

### Added

- **Per-node Fast Mode toggle.** Each device has a Fast on/off button on the
  dashboard and in Settings (`POST /api/config`), so Fast Mode can be rolled out
  one tablet at a time. Turning off the last node clears the master switch;
  turning every node back on restores "all nodes".
- **Incremental log following.** Log views request only bytes appended since the
  last poll instead of re-reading the file, including a distinct `runner` log
  source.
- Project logo (`docs/assets/eanhost-logo.*`) and a logo header in the README.

### Changed

- `panel/config.json` is no longer tracked. It holds the admin password hash and
  the real node list, so it is ignored by Git; `panel/config.example.json` is
  the template and `provision-node.sh` copies it on first run.
- Repository hygiene: generated screenshots, `__pycache__`, backups, and
  release artifacts are ignored.

### Security

- Panel sessions are in-memory and expire after seven days. Set a strong
  administrator password with `python3 panel/panel.py --set-pass`.
- The panel serves HTTP on `0.0.0.0:8443` by default; keep it on a trusted LAN
  or behind a TLS-terminating proxy.
