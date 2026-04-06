# Remote Desktop Plugin

CTFd plugin that provisions on-demand desktops across a pool of Docker hosts, users click a button and get a browser VNC session with per-container auth and automatic cleanup

## How it works

When a user requests a session the plugin picks the least-loaded healthy Docker context, hits the Docker API over an SSH tunnel via the docker SDK, runs the container with dynamic port mapping, generates a random VNC password, and builds a noVNC URL that routes through nginx. The whole thing runs in a gevent greenlet so it doesn't block the request thread, and the frontend polls for creation status updates

All VNC traffic goes through nginx via `auth_request` so the Docker hosts don't need to be publicly accessible. nginx makes an internal subrequest to CTFd to validate the session and get the backend address, then proxies directly to the container for both HTTP assets and the WebSocket connection. Admins can peek at any user's desktop from the dashboard using the same stored password

## Access control

The user-facing page at `/remote-desktop` checks two things before letting a user through. First, the `remote_desktop_enabled` setting must be on, if an admin flips it off in the dashboard settings all users see a full-page message saying the feature has been disabled by an administrator. Second, if CTFd has email verification enabled (`verify_emails` in CTFd config), unverified users get a message telling them to verify their email with a button linking to `/confirm`, matching how CTFd's own challenges page gates access. Admins bypass the verification check but still see the disabled page when the feature is turned off

Both checks also gate the `/api/create` endpoint so session creation can't be triggered by hitting the API directly. Existing sessions are unaffected when the feature gets disabled mid-use, they continue running and expire naturally through the periodic cleanup job

## Setup

### Installing the plugin

Clone this repo into CTFd's plugin directory

```bash
cd CTFd/CTFd/plugins
git clone <repo-url>
```

CTFd picks up plugins on startup so you'll need to restart after cloning

### Quick setup

Run the setup script from the CTFd root directory. It handles docker-compose volumes, permissions, and nginx config automatically, and skips anything already configured.

```bash
bash CTFd/plugins/ctfd-remote-desktop/setup.sh
docker compose up -d
```

If you prefer to do it manually or need to understand what the script does, read on.

### Docker access

CTFd runs as a non-root user (uid 1001, home `/home/ctfd`) inside the container. Everything needs to be mounted to paths it can read, not under `/root`.

1. Get your docker group GID

```bash
stat -c '%g' /var/run/docker.sock
```

2. Open up file permissions so the container user can read them

```bash
chmod 755 ~/.docker ~/.ssh
chmod 644 ~/.ssh/known_hosts ~/.ssh/id_ed25519  # or whatever your key is
```

3. Add these to your CTFd service in `docker-compose.yml`, replacing `DOCKER_GID` with the number from step 1

```yaml
services:
  ctfd:
    group_add:
      - "DOCKER_GID"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ~/.ssh:/home/ctfd/.ssh:ro
      - ~/.docker:/home/ctfd/.docker:ro
```

The socket mount gives local Docker access, the SSH mount lets the Docker SDK tunnel to remote hosts, and the docker config mount has the context metadata files the plugin reads to find your configured contexts. Everything is mounted read-only.

If you're only using remote contexts and don't need a local daemon you can skip the socket and `group_add`, but you still need the SSH and docker config mounts

### VNC proxy (nginx)

VNC sessions are proxied through nginx so users never need direct access to container ports, everything goes through your existing HTTPS setup

The setup script adds the required nginx location blocks to both `conf/nginx/http.conf` and `conf/nginx/https.conf` if they exist. If you have a custom config, add the blocks manually. CTFd ships with `http.conf` but production deployments typically use a separate `https.conf` with TLS termination, make sure the blocks go into whichever file nginx is actually loading (check the volume mount in docker-compose.yml). The blocks needed are:

```nginx
# inside your server block, before location /

location ~ ^/remote-desktop/vnc/(?<vnc_user_id>\d+)/(?<vnc_path>.+)$ {
  resolver 127.0.0.11 valid=30s;
  auth_request /remote-desktop/vnc/auth;
  auth_request_set $vnc_host $upstream_http_x_vnc_host;
  auth_request_set $vnc_port $upstream_http_x_vnc_port;

  proxy_pass http://$vnc_host:$vnc_port/$vnc_path$is_args$args;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_read_timeout 86400s;
  proxy_send_timeout 86400s;
  proxy_buffering off;
  proxy_cache off;
  add_header Cache-Control "no-store";
}

location = /remote-desktop/vnc/auth {
  internal;
  proxy_pass http://app_servers;
  proxy_pass_request_body off;
  proxy_set_header Content-Length "";
  proxy_set_header X-VNC-User-ID $vnc_user_id;
  proxy_set_header Cookie $http_cookie;
}
```

`resolver 127.0.0.11` is Docker's internal DNS, required because nginx can't resolve hostnames at runtime in a dynamic `proxy_pass` without it. `Cache-Control: no-store` prevents the browser from caching vnc.html with an old session's password baked into the query params

### Docker contexts

For single-server deployments you don't need to configure anything. On first boot with an empty contexts table the plugin checks if the local Docker socket is reachable, and if so creates a `local` context automatically using the machine's hostname as the public address. If you delete it and restart CTFd it comes back

For multi-host setups, create docker contexts on the machine running CTFd (or inside the container if you mounted the config)

```bash
docker context create server1 --docker "host=ssh://user@server1.example.com"
docker context create server2 --docker "host=ssh://user@server2.example.com"
```

Then go to the Docker Contexts section on the admin config page (`/admin/config` under the Remote Desktop tab) and click Import Contexts. The plugin scans `~/.docker/contexts/meta/` for available contexts and checks if the local Docker socket is accessible, then shows what it found with a connectivity status for each. Set the public hostname (what users see in VNC URLs) and click Import. You can adjust weight and other settings after importing by clicking Edit on the context in the table

### Container image

The image needs to be pre-pulled on every Docker host before users can use it. Pull it manually on each host or use a CI pipeline to push it out

The image needs to expose VNC on port 5900 and noVNC on port 6080, accept `CTFD_USERNAME` (already sanitized to `[a-z0-9]` by the plugin, but the container should still sanitize as defense in depth), `VNC_PASSWORD` (configure Xvnc with VncAuth, fall back to a random password if unset), `RESOLUTION`, and `MAX_LIFETIME` env vars, and serve the noVNC web client at `/vnc.html` with a WebSocket endpoint at `/websockify`

`MAX_LIFETIME` is the absolute ceiling in seconds, calculated as `initial_duration + (extension_duration * max_extensions) + 300`. The container image can use it to run something like `sleep $MAX_LIFETIME && kill 1` as a safety net if the plugin's cleanup job doesn't reach it. The container's hostname is set to the Docker context name (e.g. `local`, `server1`)

### Database

The plugin creates its tables automatically on first load, no manual migration needed. It creates `desktop_docker_contexts` for the context pool, `desktop_container_info` for active session state, `desktop_session_history` for completed session records, and `desktop_settings` for configuration. On first startup it seeds all settings with defaults and creates a `local` Docker context if the socket is available, so the admin UI is immediately usable without any manual context setup

## Container lifecycle

### Creation

1. User requests session via `/api/create`
2. Gevent greenlet spawns with Flask app context
3. Orchestrator picks least-loaded healthy context via weighted scoring
4. Acquires the per-context creation semaphore (limits concurrent creates per host)
5. Generates random VNC password via `secrets.token_urlsafe(6)[:8]`
6. Calls `DockerHostManager.run_container()` which talks to the Docker API through the SDK's SSH tunnel, creates the container with dynamic port mapping (0:5900, 0:6080), security hardening (`cap_drop=ALL` + selective `cap_add`, pids limit), resource limits, and the VNC/resolution env vars
7. Polls `container.reload()` for mapped ports (up to 5 attempts with 0.3s sleep)
8. HTTP polls noVNC via the internal `check_hostname` until it responds (configurable attempts, default 180). For local socket contexts this is the docker host gateway IP, for SSH contexts it's the pub_hostname
9. Builds a relative VNC URL like `/remote-desktop/vnc/{user_id}/vnc.html?autoconnect=true&password={pw}&resize=remote&reconnect=true` that routes through the nginx proxy
10. Writes a `DesktopContainerInfoModel` row to the database with all session state including timer config

If no healthy contexts are available, creation fails immediately instead of spawning a background task that will fail anyway. If creation fails after the container was already started (e.g. VNC timeout), the error handler stops the orphaned container before releasing the slot

### Destruction

User or periodic cleanup triggers it. Before deleting the DB row the plugin writes a `DesktopSessionHistoryModel` entry capturing who used it, which host, start/end times, duration, how many extensions were used, and why the session ended (`user_destroyed`, `expired`, `admin_killed`, or `reconciliation`). Then it deletes the row, calls `DockerHostManager.stop_container()` to hit the Docker API, and releases the context slot in the orchestrator

### Session timers

Timers start on first status poll after the container is ready. Default is 3600s with up to 3 extensions of 1800s each, all configurable via the admin web UI without restart. An APScheduler job runs on a configurable interval (default 300s) to query the database for expired sessions and auto-destroy them

## State storage

All container and timer state lives in the database via `DesktopContainerInfoModel`, so if CTFd restarts your sessions survive. The model stores the container ID, user ID, container name, VNC ports, VNC password, the full noVNC URL, which Docker context it's on, the public hostname, creation timestamp, and all timer fields (started flag, start time, duration, extensions used, max extensions)

On startup the plugin runs a reconciliation pass that checks every DB record against Docker to see if the container is still running. Records where the container is gone get a history entry written with reason `reconciliation` before being deleted. Records where it's still alive get their orchestrator slots reserved so the load balancer counts them correctly. This replaces the old approach of blanket-killing any `rd-session-*` container on startup, which was destructive if you had a rolling restart

## Session history

Every session that ends gets a row in `desktop_session_history` recording user_id, username, docker_context, started_at, ended_at, duration, end_reason, and extensions_used. The end_reason field tracks how the session ended: `user_destroyed` when the user clicks destroy, `expired` when the timer runs out, `admin_killed` when an admin kills it from the dashboard, or `reconciliation` when the startup check finds a stale record

The admin dashboard has a Usage Stats section that queries this history. Summary cards show total sessions, average duration, and peak concurrent sessions (calculated with a sweep-line algorithm over all start/end intervals). A top users chart shows the 15 heaviest users by total duration, and a daily usage chart shows session counts over time. Both charts filter by a shared period dropdown (past week, past month, all time)

Admins can also kill all active sessions at once with the Kill All button in the sessions card header. It iterates every active session and destroys them with reason `admin_killed`, logging a single admin event

## Container security

Every container gets hardened defaults

- `cap_drop=["ALL"]` drops all Linux capabilities, then `cap_add` re-grants only the ones needed: CHOWN, SETUID, SETGID, FOWNER, DAC_OVERRIDE for startup user creation and su, NET_RAW and NET_ADMIN for wireshark/nmap, SETFCAP for granting dumpcap packet capture. Users get full sudo inside their container which is intentional for a CTF lab
- `pids_limit` from settings (default 512) caps the process count to prevent fork bombs
- `auto_remove=True` so Docker cleans up the filesystem when the container stops

## Container usernames

The plugin sanitizes CTFd display names down to `[a-z0-9]` (lowercase, strip everything non-alphanumeric, truncate to 32 chars) before passing them to the container as `CTFD_USERNAME`. The `username_source` setting controls what gets sanitized

- `name` (default): uses the CTFd display name, so a user named `Alice B.` becomes `aliceb`
- `email`: uses the local part of the user's email, so `jdoe@ucsc.edu` becomes `jdoe`

If sanitization produces an empty string (a name like `;-;` strips to nothing) the plugin falls back to `user{id}`, for example `user42`. This is computed at container creation time, the raw display name is still used in logs and the admin dashboard

## VNC auth

The plugin generates a random 8-char password per container and passes it as `VNC_PASSWORD` to the container. The container's startup script writes a VNC passwd file and launches Xvnc with `-SecurityTypes VncAuth`. The VNC URL has the password as a query param so noVNC auto-connects with no dialog

VNC passwords are capped at 8 chars by the protocol, `secrets.token_urlsafe(6)[:8]` gives 48 bits of entropy which is plenty for preventing port-scan drive-bys in a classroom setting. The password shows up in the browser URL bar and history, fine for a lab environment. The nginx VNC location must have `Cache-Control: no-store` or the browser will cache vnc.html with the old password and auth will fail after recreating a session

## Project structure

The root `__init__.py` is a thin entry point that re-exports `load` from the `src/` subpackage. All source modules, templates, and the Blueprint live under `src/`, keeping the repo root clean for config files and project metadata. Internal relative imports resolve within `src/` so nothing changes from CTFd's perspective, it still calls `load(app)` from the plugin root

A `config.json` at the root registers the plugin's settings panel inline on CTFd's admin config page (`/admin/config`). A `setup.sh` script automates docker-compose, nginx, and file permission configuration

## Components

### DockerHostManager

Manages docker SDK clients for each configured docker context, uses thread-local client caching with a generation counter so each thread gets its own `DockerClient` instance and stale clients from old configs get dropped transparently when the generation bumps. Context loading queries `DesktopDockerContextModel` for enabled entries, resolves each endpoint by scanning all directories under `~/.docker/contexts/meta/` and matching by the `Name` field inside each `meta.json` (Docker stores these in hash-named directories, not by context name). Falls back to `ssh://{hostname}` from the DB record if no meta match is found, and as a final fallback connects via the local Docker socket at `/var/run/docker.sock` if it exists. Also exposes `discover_contexts()` which scans the same metadata directories and the local socket to find all available contexts for import. Each context gets a `BoundedSemaphore` (default limit 2) that gates concurrent container creation so a burst of users hitting start simultaneously queue up instead of overwhelming the Docker daemon with parallel SSH connections

`get_pub_hostname()` returns the address stored in the DB, `get_check_hostname()` returns where to actually connect for internal checks like VNC readiness polling. For local socket contexts these differ because `localhost` inside the CTFd container is the container itself, not the Docker host where the port mappings live, so `check_hostname` resolves to the host gateway IP (read from `/proc/net/route` default route). For SSH contexts they're the same

### Orchestrator

Tracks per-context container counts, health status, and weights, picks the next context via weighted least-connections (`weight / (count + 1)`, highest score wins, ties broken alphabetically). Context selection and slot reservation happen atomically so two concurrent requests can't race for the same slot. On `load_from_db()` it queries enabled contexts, tells DockerHostManager to connect, then pings each context and checks for the configured docker image outside the lock so network I/O doesn't block scheduling. Contexts that fail either step get marked unhealthy and pulled from rotation. Results show up in the admin event feed

### ContainerManager

Handles container creation, destruction, timer operations, and periodic cleanup. All session state is stored in the database via `DesktopContainerInfoModel`, the only in-memory state is `creation_status` which tracks the progress of in-flight container creations (selecting host, starting container, waiting for VNC, ready/failed). Checks `orchestrator.has_healthy_context()` before spawning the background greenlet to fail fast. If creation fails after the container was already started, the error handler stops it before releasing the slot. The error path wraps container stop, slot release, and health marking in individual try/except blocks so a failure in one doesn't mask the others

### EventLogger

Thread-safe event log backed by a deque with 2000 event limit, supports real-time listener callbacks for SSE streaming to the admin dashboard. Each event has a type, message, level (info/warning/error), timestamp, human-readable datetime, optional user info, and a metadata dict for domain-specific fields. Also writes to Python's logging module so events show up in CTFd's logs

## Configuration

All configuration is stored in the database via `DesktopSettingsModel` and managed through the admin web UI. No config files needed, on first load with an empty DB everything falls back to defaults which get seeded into the database automatically

### Docker contexts

Managed through the admin dashboard. Click Import Contexts to scan the host for available docker contexts, each discovered context shows its endpoint and whether it's reachable. Set a public hostname (what users see in VNC URLs) and import. After importing you can edit weight, public hostname, and enabled status. A `local` context is auto-seeded on first boot when the Docker socket is available. Test connectivity, reload connections, and delete contexts all from the UI without restarting CTFd

### Default settings

| Key | Default | Description |
|-----|---------|-------------|
| remote_desktop_enabled | false | master switch, when false the user page shows a disabled message and session creation is blocked |
| docker_image | ctfd-remote-desktop:latest | container image to run for each desktop session |
| memory_limit | 4g | max memory per container |
| shm_size | 512m | shared memory size, needs to be large enough for the browser and desktop compositor |
| resolution | 1920x1080 | desktop resolution passed to the container as an env var |
| cpu_limit | 2 | max cpu cores per container |
| initial_duration | 3600 | how long a session lasts in seconds before it expires |
| extension_duration | 1800 | how many seconds each extension adds |
| max_extensions | 3 | how many times a user can extend their session |
| vnc_ready_attempts | 180 | number of http polls to wait for novnc to come up, each attempt is 0.5s apart |
| http_request_timeout | 3 | timeout in seconds for each novnc readiness poll |
| cleanup_interval | 300 | how often the scheduler scans for expired sessions in seconds |
| pids_limit | 512 | max number of processes per container, prevents fork bombs |
| max_concurrent_creates | 2 | how many containers can be created simultaneously on a single host |
| username_source | name | what to derive the container linux username from, `name` uses the CTFd display name, `email` uses the local part before the @ |

## API endpoints

All user endpoints are under `/remote-desktop/`, admin endpoints under `/remote-desktop/admin/`

**User**

- `GET /remote-desktop` main UI
- `POST /api/create` request session
- `GET /api/creation-status` poll progress
- `GET /api/status` current session
- `POST /api/destroy` destroy session
- `POST /api/extend` extend timer
- `POST /api/cleanup` trigger cleanup (admin only)

**Admin**

- `GET /admin` dashboard
- `GET /admin/api/containers` list sessions
- `GET /admin/api/hosts` orchestrator status
- `POST /admin/api/kill` force kill
- `POST /admin/api/kill-all` kill all sessions
- `POST /admin/api/extend` extend any session
- `GET /admin/api/events/stream` SSE
- `GET /admin/api/events/recent` event log

**Stats**

- `GET /admin/api/stats/summary` total sessions, avg duration, peak concurrent
- `GET /admin/api/stats/top-users?period=week|month|all` top 15 users by duration
- `GET /admin/api/stats/usage?period=week|month|all` daily session counts

**VNC Proxy**

- `GET /vnc/auth` internal nginx auth_request endpoint, returns backend host/port in headers
- `GET /vnc/<user_id>/<path>` proxied through nginx to container (not handled by Flask)

**Contexts**

- `GET /admin/api/contexts` list with live status, `is_local` flag, and docker socket reachability
- `GET /admin/api/contexts/discover` scan host for importable contexts with reachability check
- `POST /admin/api/contexts` add
- `PUT /admin/api/contexts/<id>` update
- `DELETE /admin/api/contexts/<id>` delete
- `GET /admin/api/contexts/<id>/test` ping + image check
- `POST /admin/api/contexts/reload` reconnect all

**Settings**

- `GET /admin/api/settings` all settings as JSON
- `PUT /admin/api/settings` bulk upsert

## Concurrency

CTFd runs under gunicorn with gevent workers. Container creation uses `gevent.spawn()` to avoid blocking request threads during Docker API calls and startup polling. State protection uses `threading.Lock` since greenlets within the same worker share memory

The docker SDK maintains SSH tunnels per client instance, thread-local caching means each thread gets its own connection so there's no contention on a shared client. Per-context semaphores limit concurrent container creation (default 2) so a burst of requests doesn't overwhelm the Docker daemon

All shared state is guarded by component-level locks: ContainerManager.lock for creation_status, Orchestrator.lock for container counts and health, EventLogger.lock for the events deque and listeners list. Lock acquisition is never nested so there's no deadlock risk

## Scheduling

The plugin uses APScheduler instead of a daemon thread for background jobs. Under gunicorn with gevent it uses `GeventScheduler`, otherwise `BackgroundScheduler`. Two independent jobs run

- **Expiry check**: every `cleanup_interval` seconds (default 300), queries the database for sessions with expired timers and destroys them
- **Health check**: every 30 seconds, pings each context and updates health status

Both jobs use `misfire_grace_time=30` and `coalesce=True` so if the scheduler falls behind it catches up without firing duplicate runs

## Context health

Contexts get marked unhealthy when the connectivity test fails (SSH tunnel or docker daemon ping). During container creation, a context only gets marked unhealthy if the host is actually unreachable, transient errors like VNC startup timeouts don't affect health status. Unhealthy contexts stay out of scheduling rotation

The health check job runs every 30 seconds, pinging each context and automatically recovering ones that come back online. You can also hit the Reload button in the admin UI to reconnect everything without restarting CTFd

## Startup reconciliation

On startup the plugin queries all `DesktopContainerInfoModel` rows and checks each against Docker to see if the container is still running. Containers that are gone get a history entry written and their DB records deleted. Containers that are still alive get their orchestrator slots reserved so the load balancer has accurate counts from the start. If the Docker host is unreachable the record gets treated as stale and removed

This means a CTFd restart doesn't kill active user sessions, they survive and get picked back up automatically

## Troubleshooting

**Docker socket permission denied**: CTFd runs as a non-root user inside the container. If you see `PermissionError(13)` in the logs, add `group_add: ["DOCKER_GID"]` to the CTFd service in docker-compose.yml where DOCKER_GID is the output of `stat -c '%g' /var/run/docker.sock` on the host

**Sessions not creating**: check that Docker contexts are configured and the image is pulled on all hosts, use the Test button in the admin context UI to verify connectivity and image availability

**VNC never becomes ready**: the plugin polls `http://{check_hostname}:{novnc_port}/` up to 180 times at 0.5s intervals waiting for noVNC to respond. For local contexts `check_hostname` is the docker host gateway, for SSH contexts it's the pub_hostname. If the container takes longer to start you can increase `vnc_ready_attempts` in settings

**VNC auth failed after recreating a session**: browser cached the old vnc.html with the previous password. Hard refresh (Ctrl+Shift+R) to clear it. If it keeps happening, check the nginx VNC location has `proxy_cache off` and `add_header Cache-Control "no-store"`

**502 on VNC proxy**: check the nginx error log. Common causes: `no resolver defined` means the `resolver 127.0.0.11` directive is missing from the VNC location block, `host not found` means the Docker host isn't resolvable from the nginx container

**Sessions lost after restart**: this shouldn't happen anymore since state is in the database, if it does check the CTFd logs for reconciliation messages, you should see something like "reconciled containers on startup: N recovered, M stale records removed"

**Containers piling up on one host**: the orchestrator uses weighted least-connections scoring, check that your context weights are set appropriately in the admin UI, a context with weight 2 gets twice the score bonus compared to weight 1

**Timer showing wrong values**: MySQL `FLOAT` is 32-bit, only ~7 significant digits, which rounds Unix timestamps by thousands of seconds. The model uses `db.Float(precision=53)` which maps to `DOUBLE` but if you're upgrading from an older version the column types won't change automatically. Run `ALTER TABLE desktop_container_info MODIFY created_at DOUBLE NOT NULL` and do the same for `timer_start_time`, `timer_duration`, and the history table's `started_at`, `ended_at`, `duration`
