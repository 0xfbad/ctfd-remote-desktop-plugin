# Remote Desktop Plugin

CTFd plugin that provisions on-demand desktops across a pool of Docker hosts, users click a button and get a browser VNC session with per-container auth and automatic cleanup

## How it works

When a user requests a session the plugin picks the least-loaded healthy Docker host, creates a container with dynamic port mapping and a random VNC password, and builds a noVNC URL that routes through nginx. Creation runs in a background greenlet so it doesn't block the request thread, and the frontend polls for status updates

All VNC and terminal traffic goes through nginx via `auth_request` so the Docker hosts don't need to be publicly accessible. nginx makes a subrequest to CTFd to validate the session and get the backend address, then proxies directly to the container. Admins can peek at any user's desktop from the dashboard using the same stored password. Session state lives in the database so active sessions survive CTFd restarts

## Connection modes

The workspace UI has three modes selectable from tabs in the bottom bar

- Desktop (noVNC), full graphical desktop in the browser proxied through nginx
- Terminal (ttyd), browser-based shell also proxied through nginx, lower overhead for command-line work
- SSH, direct connection from the user's own terminal with a copyable ssh command and the session password. Requires the mapped SSH port to be reachable from the user's machine

Desktop and Terminal go through the same nginx auth_request flow. SSH is a fallback for native terminal experience

## Autologin

The plugin mints a CTFd session for the user at spawn time and hands the cookie to the container along with the public CTFd URL. Firefox loads it at launch via autoconfig and the homepage points at `/challenges`

If CTFd is reached at `localhost` (typical for dev), the plugin swaps to `host.docker.internal` with an `extra_hosts` entry so the container can reach the host. Public URLs pass through as-is

## Setup

### Install

Clone into CTFd's plugin directory and restart CTFd

```bash
cd CTFd/CTFd/plugins
git clone <repo-url>
```

### Quick setup

Run the setup script from the CTFd root directory, it handles docker-compose volumes, permissions, and nginx config

```bash
bash CTFd/plugins/ctfd-remote-desktop/setup.sh
docker compose up -d
```

### Manual setup

CTFd runs as a non-root user (uid 1001) inside the container. Get your docker group GID and add the required mounts to docker-compose.yml

```bash
stat -c '%g' /var/run/docker.sock
```

```yaml
services:
  ctfd:
    group_add:
      - "DOCKER_GID"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ctfd-ssh:/home/ctfd/.ssh:ro
      - ~/.docker:/home/ctfd/.docker:ro
    depends_on:
      permissions:
        condition: service_completed_successfully

  permissions:
    image: alpine:3.23
    user: root
    volumes:
      - ~/.ssh:/mnt/host-ssh:ro
      - ctfd-ssh:/mnt/ctfd-ssh
    command: >
      sh -c '
        cp -a /mnt/host-ssh/. /mnt/ctfd-ssh/ &&
        chown -R 1001:1001 /mnt/ctfd-ssh
      '

volumes:
  ctfd-ssh:
```

The socket mount gives local Docker access, the SSH keys are copied into a named volume by the permissions init container with correct ownership (uid 1001 matches ctfd inside the container), and the docker config mount has context metadata. Don't bind-mount `~/.ssh` directly, the host UID won't match the container user and paramiko will fail to read `known_hosts`. If you're only using remote contexts you can skip the socket and group_add

The setup script also handles nginx location blocks for VNC and terminal proxying. For custom nginx configs, see the location blocks in `setup.sh` and add them to whichever config nginx is actually loading

### Docker contexts

Single-server deployments need no configuration, on first boot the plugin auto-creates a `local` context if the Docker socket is reachable

For multi-host setups, create docker contexts and import them from Admin > Config > Remote Desktop

```bash
docker context create server1 --docker "host=ssh://user@server1.example.com"
docker context create server2 --docker "host=ssh://user@server2.example.com"
```

### Container image

The image needs to be pre-pulled on every Docker host. It should expose VNC on 5900, noVNC on 6080, ttyd on 7682, and SSH on 22. Env vars consumed: `CTFD_USERNAME`, `VNC_PASSWORD`, `RESOLUTION`, `MAX_LIFETIME`, plus `CTFD_URL`, `CTFD_COOKIE_NAME`, `CTFD_COOKIE_VALUE` for autologin

## Configuration

All settings live in the database and are managed through Admin > Config > Remote Desktop. On first load everything gets seeded with defaults

| Key | Default | Description |
|-----|---------|-------------|
| remote_desktop_enabled | false | master switch for the feature |
| docker_image | ctfd-remote-desktop:latest | container image to run |
| memory_limit | 4g | max memory per container |
| shm_size | 512m | shared memory, needs to fit browser and compositor |
| resolution | 1920x1080 | desktop resolution |
| cpu_limit | 2 | max cpu cores per container |
| initial_duration | 3600 | session length in seconds |
| extension_duration | 1800 | seconds added per extension |
| max_extensions | 3 | max extension count |
| vnc_ready_attempts | 180 | polls waiting for noVNC, 0.5s each |
| cleanup_interval | 300 | seconds between expired session scans |
| pids_limit | 512 | max processes per container |
| max_concurrent_creates | 2 | concurrent creates per host |
| username_source | name | derive container username from CTFd `name` or `email` |
| require_verified | true | require email verification, only applies if CTFd has verification enabled |
| command_logging_enabled | false | periodically ingest shell command logs from running containers |
| cap_drop | ALL | linux capabilities to drop |
| cap_add | CHOWN,SETUID,SETGID,FOWNER,DAC_OVERRIDE,NET_RAW,NET_BIND_SERVICE,AUDIT_WRITE,SYS_CHROOT | linux capabilities to add back |

## Troubleshooting

- `PermissionError(13)` on the Docker socket: add `group_add: ["DOCKER_GID"]` to docker-compose where DOCKER_GID is from `stat -c '%g' /var/run/docker.sock`
- Sessions won't create: check that contexts are configured and the image is pulled on all hosts, use the Test button in the admin UI
- VNC never becomes ready: increase `vnc_ready_attempts` in settings if the container is slow to start
- VNC auth fails after recreating a session: browser cached the old password, hard refresh to clear it. Make sure the nginx VNC location has `Cache-Control: no-store`
- 502 on VNC proxy: check the nginx error log, usually means `resolver 127.0.0.11` is missing from the VNC location block or the Docker host isn't resolvable from nginx
- A tool won't run with "Operation not permitted": the binary has file capabilities set, run `getcap /path/to/binary` in the container and add the missing caps to Cap Add in settings
- Containers piling up on one host: check context weights in the admin UI, a context with weight 2 gets twice the scheduling score

## Development

```
ruff format --check .
ruff check .
mypy .
vulture .
pytest tests/ -v
```
