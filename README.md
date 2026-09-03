# Remote Desktop Plugin

CTFd plugin for on-demand Docker desktop sessions: browser desktop over noVNC,
optional web terminal and SSH, per-container credentials, database-backed
state, and automatic cleanup.

## Enable

Copy or clone this directory to `CTFd/plugins/ctfd-remote-desktop`, then from
the CTFd root:

```bash
bash CTFd/plugins/ctfd-remote-desktop/setup.sh
docker compose up -d --build
```

## Notes

- Fresh installs only: start with an empty database that has no `desktop_*`
  tables.
- Image contract: the configured desktop image must carry the label
  `edu.ucsc.ctfd-remote-desktop.contract=3`.
- Settings: the plugin starts disabled; configure and enable it under
  **Admin > Config > Remote Desktop**. Setting specs live in `src/settings.py`.

## Development

Run `pytest tests/` (local-only suite, not tracked) and `ruff check .`.
