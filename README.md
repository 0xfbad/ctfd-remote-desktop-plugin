# Remote Desktop Plugin

CTFd plugin for on-demand Docker desktop sessions. Users can open a browser desktop through noVNC, use the browser terminal, or connect over SSH when those services are present in the configured image. Sessions have per-container credentials, database-backed state, and automatic cleanup.

## Requirements

- A fresh CTFd checkout using its Docker Compose deployment
- A local Docker Engine Unix socket accessible to the CTFd container
- A desktop image available to Docker that listens for noVNC on 6080 and container-internal VNC on 5900; ttyd on 7682 and SSH on 22 are optional
- The image must accept `CTFD_USERNAME`, `VNC_PASSWORD`, `RESOLUTION`, and `MAX_LIFETIME`. It may use `CTFD_URL`, `CTFD_COOKIE_NAME`, and `CTFD_COOKIE_VALUE` for CTFd autologin.
- Before noVNC reports ready, the image must atomically publish the actual Linux account name as a single `^[a-z_][a-z0-9_]{0,31}$` line at `/var/lib/remote-desktop/resolved-username`. Missing or invalid handoff data fails creation so the UI never advertises incorrect SSH credentials.

The repository does not build or publish the desktop image. The plugin starts disabled so an administrator can set and verify the image before allowing sessions. Session containers use Docker's built-in init process for signal forwarding and orphan reaping; use the pinned Docker SDK range from `requirements.txt` and a Docker-compatible daemon that supports the `init` create option.

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

Desktop and browser-terminal traffic is authorized through nginx `auth_request`.
The proxy also injects per-session HTTP Basic credentials when connecting to
ttyd, so discovering its random backend port does not expose an unauthenticated
writable shell. Docker still publishes the backends on random host ports in
40000–59999, but production must **not** allow that range wholesale. Docker's
forwarding firewall sees the destination after DNAT, so match the container
port: allow 6080 and 7682 only from the CTFd proxy; allow 22 only from intended
SSH client ranges; and deny other new
inbound session flows. Install this in `DOCKER-USER` for Docker's iptables
backend, or an equivalent nftables forward hook, rather than relying on the
host `INPUT` chain. The plugin does not publish raw VNC/5900; noVNC's
websockify connection to it stays inside the container. Per-session bridge
isolation remains deployment work rather than a property of the current shared
`bridge` default.

`max_concurrent_creates` is a process-local Docker-create throttle, not a
distributed host limit. With `W` CTFd worker processes and a value of `N`, as
many as `W × N` creates may enter Docker concurrently; use each context's
database-backed `max_containers` as the hard admission limit and size the local
throttle with the worker count in mind. A live settings reload preserves the
old semaphore for its in-flight callers and creates a new generation when the
limit changes, so old and new work can overlap briefly. Drain creates and
restart all workers when a strict immediate reduction is required.

Session containers use `auto_remove`, and the image ends a session after three
consecutive health failures or the loss of a supervised core service. This
fail-fast policy avoids advertising a broken desktop, but container exit also
permanently deletes the writable layer. The filesystem is not durable storage;
students must export important work, and operators requiring post-failure
recovery need a different container lifecycle before enabling the service.

## Contract-3 rollout order

Roll out the proxy, image, and strict plugin as one ordered compatibility
change. Disable new allocations first and drain existing sessions when
uninterrupted class use matters.

1. Run the updated `setup.sh`, validate nginx, and reload it so the noVNC and
   ttyd proxy behavior is in place while the currently deployed plugin/image
   pair is still available.
2. Publish the tested desktop manifest by digest, with image label
   `edu.ucsc.ctfd-remote-desktop.contract=3`, pull that exact digest on every
   Docker context, and verify the digest and label out of band on every daemon:

   ```bash
   docker --context CONTEXT image inspect REPOSITORY@sha256:DIGEST \
     --format '{{json .RepoDigests}} contract={{index .Config.Labels "edu.ucsc.ctfd-remote-desktop.contract"}}'
   ```

   Require the expected digest and `contract=3`. Do not change the active image
   setting while an older plugin/image pair is still serving sessions.
3. Deploy and restart the contract-enforcing plugin with Remote Desktop still
   disabled. Configure the exact digest, run the plugin image scan, and require
   a compatible result from every context.
4. Exercise noVNC and ttyd through the real nginx `auth_request` path (plus SSH
   if enabled), and only then enable new allocations.

Keep the previous proxy config, plugin revision, and image digest as a matched
rollback set; do not roll the strict plugin back while leaving assumptions
about a newer proxy/image contract undocumented.

## Development

```bash
ruff format --check .
ruff check .
mypy .
vulture .
pytest tests/ -v
```
