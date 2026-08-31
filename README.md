# Remote Desktop Plugin

CTFd plugin for on-demand Docker desktop sessions. Users can open a browser desktop through noVNC, use the browser terminal, or connect over SSH when those services are present in the configured image. Sessions have per-container credentials, database-backed state, and automatic cleanup.

## Requirements

- A fresh CTFd checkout using its Docker Compose deployment
- A local Docker Engine Unix socket accessible to the CTFd container
- A desktop image available to Docker that exposes noVNC on 6080 and VNC on 5900; ttyd on 7682 and SSH on 22 are optional
- The image must accept `CTFD_USERNAME`, `VNC_PASSWORD`, `RESOLUTION`, and `MAX_LIFETIME`. It may use `CTFD_URL`, `CTFD_COOKIE_NAME`, and `CTFD_COOKIE_VALUE` for CTFd autologin.

The repository does not build or publish the desktop image. The plugin starts disabled so an administrator can set and verify the image before allowing sessions.

## Install

Copy or clone this directory to `CTFd/plugins/ctfd-remote-desktop`, then run from the CTFd root:

```bash
bash CTFd/plugins/ctfd-remote-desktop/setup.sh
docker compose up -d --build
```

The setup script mounts the local Docker socket, adds its group ID to the CTFd service, and installs the nginx proxy locations. It does not require or copy `~/.ssh` or `~/.docker` for a local deployment. Set `DOCKER_SOCK_OVERRIDE` when the engine uses another Unix socket path.

Remote Docker contexts are optional. Credential staging is off by default and must be explicitly enabled for the material a remote context actually uses:

```bash
CTFD_RD_STAGE_DOCKER_CONFIG=1 CTFD_RD_STAGE_SSH=1 \
  bash CTFd/plugins/ctfd-remote-desktop/setup.sh
```

Both flags accept only `0` or `1` and must be supplied on every later setup run while credential staging remains installed. An enabled source directory must already exist and be readable; setup never creates host credential directories.

## Configure

Open **Admin > Config > Remote Desktop**, choose the desktop image, scan its availability, and enable Remote Desktop after the local context is healthy.

Every session joins the single configured Docker network, which defaults to Docker's built-in `bridge`. Optional `storage_limit` and `cgroup_parent` values are passed directly to Docker and should be set only when the target daemon supports them.

Key defaults:

| Key | Default | Description |
| --- | --- | --- |
| `remote_desktop_enabled` | `false` | Sessions remain disabled until configured. |
| `docker_image` | `ctfd-remote-desktop:latest` | Image Docker will run. |
| `rd_network_name` | `bridge` | Built-in local Docker network. |
| `storage_limit` | empty | Optional Docker writable-layer quota. |
| `cgroup_parent` | empty | Optional Docker parent cgroup. |

Desktop and browser-terminal traffic is authenticated through nginx `auth_request`. Direct SSH requires the published SSH port to be reachable from the user's machine.

## Development

```bash
ruff format --check .
ruff check .
mypy .
vulture .
pytest tests/ -v
```
