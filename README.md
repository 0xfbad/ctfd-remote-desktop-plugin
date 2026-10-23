# remote desktop

CTFd plugin for temporary desktops on local Docker or SSH Docker contexts, with noVNC, a ttyd web terminal, optional SSH access, session limits, and an admin dashboard.

Put this repo at `CTFd/plugins/ctfd-remote-desktop`, then run from the CTFd root:

```bash
bash CTFd/plugins/ctfd-remote-desktop/setup.sh
docker compose up -d --build
```

Configure runners, the desktop image, and access under Admin Config, then enable it. Build the separate [desktop image](https://git.ucsc.edu/intro-hacking-competitions/remote-desktop) on each runner. The setup script mounts the local Docker socket, installs the noVNC nginx mount and authenticated proxy routes, and validates Compose. For SSH runners, set `CTFD_RD_STAGE_SSH=1 CTFD_RD_STAGE_DOCKER_CONFIG=1` when running setup to stage the current account's SSH keys and Docker contexts into CTFd; keep the flags you used on subsequent setup runs.

Fresh installs only, start with no `desktop_*` tables. Startup creates and validates the current schema, it does not migrate older plugin schemas. Gunicorn preload is unsupported. Each worker runs its own event subscriber and persistence queue, and one elected scheduler reconciles sessions and collects command logs.

Sessions use the image's `workspace-runtime` wrapper with a private disk quota, seccomp, and the runner's AppArmor support when available. The runner must use Linux 5.14 or newer with rootful Docker and seccomp. Keep runner ports private to CTFd and nginx; browser VNC and terminal connections pass through CTFd authorization.

The image's `remote-desktop-command-collector` records shell commands in `/var/log/.session-init/data.jsonl` through `/run/.session-init.sock`. This plugin collects that journal into the database every 30 seconds and displays it in the admin command logs. It does not collect full terminal output. Workspace context is a separate opt-in command summary for the AI tutor, controlled by `workspace_context_enabled`.

Run `nix flake check` for formatting, types, dead code, and shell checks.

Bundled noVNC comes from Debian `novnc 1:1.6.0-2`, with the focused UI changes recorded in its `source.json`. Its version directory is a content hash, so asset edits require a new hash and `NOVNC_VERSION`. Keep the bundled license files. `JetBrainsMonoNerdFontMono-Regular.woff2` is JetBrains Mono 2.304 patched by [Nerd Fonts 3.4.0](https://github.com/ryanoasis/nerd-fonts/tree/v3.4.0/patched-fonts/JetBrainsMono/Ligatures/Regular), under `src/static/fonts/OFL.txt`, with SHA-256 `9a9579b8988a1209afb089803a65f747fac876e620b7fd6e8c868a96a37ba097`.
