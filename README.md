# remote desktop

CTFd plugin for Docker or SSH desktops with noVNC, ttyd, optional SSH access, and an admin dashboard.
Install at `CTFd/plugins/ctfd-remote-desktop`, then run from the CTFd root:

```sh
bash CTFd/plugins/ctfd-remote-desktop/setup.sh
docker compose up -d --build
```

Configure runners and the desktop image under `/admin/config`, then enable the plugin.
Runners need Linux 5.14 or newer, rootful Docker, and seccomp; keep their ports private to CTFd/nginx.
Startup creates and validates the current schema; it does not migrate older plugin schemas. Do not use Gunicorn preload.
For SSH setup, set `CTFD_RD_STAGE_SSH=1 CTFD_RD_STAGE_DOCKER_CONFIG=1`; retain those flags on subsequent setup runs.

Bash/Zsh command events enter `/var/log/.session-init/data.jsonl`; one scheduler imports them into SQL every 30 seconds.
The admin dashboard uses these events for usage metrics. Tlog transcripts are not enabled or collected.
Optional tutor history is separate and controlled by `workspace_context_enabled`.
Run `nix flake check`. For noVNC asset edits, rehash its directory and update `NOVNC_VERSION`; retain the bundled licenses.
