# Remote Desktop Plugin

CTFd plugin that provisions on-demand Kali desktops across a pool of Docker hosts, students click a button and get a browser VNC session with per-container auth and automatic cleanup

## How it works

When a student requests a session the plugin picks the least-loaded healthy Docker context, hits the Docker API over an SSH tunnel via the docker SDK, runs the container with dynamic port mapping, generates a random VNC password, and builds a direct noVNC URL with the password embedded as a query param so the browser auto-connects with no dialog. The whole thing runs in a gevent greenlet so it doesn't block the request thread, and the frontend polls for creation status updates

Students connect directly to the container's noVNC port on the runner host, no reverse proxy in the path. Admins can peek at any student's desktop from the dashboard using the same stored password

## Setup

### Installing the plugin

Clone this repo into CTFd's plugin directory, the folder name doesn't matter but it needs to sit directly under `CTFd/CTFd/plugins/`

```bash
cd CTFd/CTFd/plugins
git clone <repo-url> remote_desktop
```

CTFd picks up plugins on startup so you'll need to restart after cloning

### Docker access

The CTFd container needs access to Docker, both for the local socket and for remote hosts over SSH. Add these volumes to your CTFd service in `docker-compose.yml`

```yaml
services:
  ctfd:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ~/.ssh:/root/.ssh:ro
      - ~/.docker:/root/.docker:ro
```

The docker socket lets the SDK talk to the local daemon, the SSH keys let it tunnel to remote hosts, and the docker config directory has the context metadata files the plugin reads to resolve endpoints

If you're only using remote contexts and don't need a local daemon you can skip the socket mount, but you still need the SSH and docker config mounts

For remote contexts to work from inside the CTFd container you'll also want `network_mode: host` or equivalent network access so the SSH connections can reach your Docker hosts

### Docker contexts

For single-server deployments you don't need to configure anything. On first boot with an empty contexts table the plugin checks if the local Docker socket is reachable, and if so creates a `local` context automatically using the machine's hostname as the public address. If you delete it and restart CTFd it comes back

For multi-host setups, create docker contexts on the machine running CTFd (or inside the container if you mounted the config)

```bash
docker context create server1 --docker "host=ssh://user@server1.example.com"
docker context create server2 --docker "host=ssh://user@server2.example.com"
```

Then add them through the admin dashboard at `/remote-desktop/admin`, each context needs a name matching what you created above, an optional SSH hostname (used as fallback if the context meta file is missing), a public hostname (what students see in their VNC URLs), and a weight for load balancing

### Container image

The image needs to be pre-pulled on every Docker host before students can use it. Pull it manually on each host or use a CI pipeline to push it out

The image needs to expose VNC on port 5900 and noVNC on port 6080, accept `CTFD_USERNAME` (sanitize it, CTFd display names can have spaces and special chars), `VNC_PASSWORD` (configure Xvnc with VncAuth, fall back to a random password if unset), and `RESOLUTION` env vars, and serve the noVNC web client at `/vnc.html` with a WebSocket endpoint at `/websockify`

### Database

The plugin creates its tables automatically on first load, no manual migration needed. It creates `desktop_docker_contexts` for the context pool, `desktop_container_info` for active session state, and `desktop_settings` for configuration. On first startup it seeds all settings with defaults and creates a `local` Docker context if the socket is available, so the admin UI is immediately usable without any manual context setup

## Container lifecycle

### Creation

1. User requests session via `/api/create`
2. Gevent greenlet spawns with Flask app context
3. Orchestrator picks least-loaded healthy context via weighted scoring
4. Acquires the per-context creation semaphore (limits concurrent creates per host)
5. Generates random VNC password via `secrets.token_urlsafe(6)[:8]`
6. Calls `DockerHostManager.run_container()` which talks to the Docker API through the SDK's SSH tunnel, creates the container with dynamic port mapping (0:5900, 0:6080), security hardening (`cap_drop=ALL`, `no-new-privileges`, pids limit), resource limits, and the VNC/resolution env vars
7. Polls `container.reload()` for mapped ports (up to 5 attempts with 0.3s sleep)
8. HTTP polls noVNC until it responds (configurable attempts, default 180)
9. Builds direct URL like `http://{pub_hostname}:{port}/vnc.html?autoconnect=true&password={pw}&resize=remote&reconnect=true`
10. Writes a `DesktopContainerInfoModel` row to the database with all session state including timer config

### Destruction

User or periodic cleanup triggers it, plugin deletes the DB row, calls `DockerHostManager.stop_container()` which hits the Docker API to stop the container, and releases the context slot in the orchestrator

### Session timers

Timers start on first status poll after the container is ready. Default is 3600s with up to 3 extensions of 1800s each, all configurable via the admin web UI without restart. An APScheduler job runs on a configurable interval (default 300s) to query the database for expired sessions and auto-destroy them

## State storage

All container and timer state lives in the database via `DesktopContainerInfoModel`, so if CTFd restarts your sessions survive. The model stores the container ID, user ID, container name, VNC ports, VNC password, the full noVNC URL, which Docker context it's on, the public hostname, creation timestamp, and all timer fields (started flag, start time, duration, extensions used, max extensions)

On startup the plugin runs a reconciliation pass that checks every DB record against Docker to see if the container is still running. Records where the container is gone get deleted, records where it's still alive get their orchestrator slots reserved so the load balancer counts them correctly. This replaces the old approach of blanket-killing any `kali-desktop-*` container on startup, which was destructive if you had a rolling restart

## Container security

Every container gets hardened defaults

- `cap_drop=["ALL"]` drops all Linux capabilities
- `security_opt=["no-new-privileges:true"]` prevents setuid/setgid escalation
- `pids_limit` from settings (default 512) caps the process count to prevent fork bombs
- `auto_remove=True` so Docker cleans up the filesystem when the container stops

## VNC auth

The plugin generates a random 8-char password per container and passes it as `VNC_PASSWORD` to the container. The container's startup script writes a VNC passwd file and launches Xvnc with `-SecurityTypes VncAuth`. The plugin then builds a direct URL with the password as a query param, noVNC reads it and sends it to VNC automatically so the student connects with zero interaction

VNC passwords are capped at 8 chars by the protocol, `secrets.token_urlsafe(6)[:8]` gives 48 bits of entropy which is plenty for preventing port-scan drive-bys in a classroom setting. The password shows up in the browser URL bar and history, fine for a lab environment

## Project structure

The root `__init__.py` is a thin entry point that re-exports `load` from the `src/` subpackage. All source modules, templates, and the Blueprint live under `src/`, keeping the repo root clean for config files and project metadata. Internal relative imports resolve within `src/` so nothing changes from CTFd's perspective, it still calls `load(app)` from the plugin root

A `config.json` at the root registers the plugin in CTFd's admin Plugins dropdown, pointing at the `/remote-desktop/admin` route the Blueprint already serves

## Components

### DockerHostManager

Manages docker SDK clients for each configured docker context, uses thread-local client caching with a generation counter so each thread gets its own `DockerClient` instance and stale clients from old configs get dropped transparently when the generation bumps. Context loading queries `DesktopDockerContextModel` for enabled entries, tries resolving the endpoint from the docker context meta file at `~/.docker/contexts/meta/{name}/meta.json` first, falls back to `ssh://{hostname}` from the DB record, and as a final fallback connects via the local Docker socket at `/var/run/docker.sock` if it exists. Each context gets a `BoundedSemaphore` (default limit 2) that gates concurrent container creation so a burst of students hitting start simultaneously queue up instead of overwhelming the Docker daemon with parallel SSH connections

### Orchestrator

Tracks per-context container counts, health status, and weights, picks the next context via weighted least-connections (`weight / (count + 1)`, highest score wins, ties broken alphabetically). Context selection and slot reservation happen atomically so two concurrent requests can't race for the same slot. On `load_from_db()` it queries enabled contexts, tells DockerHostManager to connect, then pings each context and checks for the configured docker image outside the lock so network I/O doesn't block scheduling. Contexts that fail either step get marked unhealthy and pulled from rotation. Results show up in the admin event feed

### ContainerManager

Handles container creation, destruction, timer operations, and periodic cleanup. All session state is stored in the database via `DesktopContainerInfoModel`, the only in-memory state is `creation_status` which tracks the progress of in-flight container creations (selecting host, starting container, waiting for VNC, ready/failed). Error handling in the creation path wraps slot release and health marking individually so a failure in one doesn't mask the others

### EventLogger

Thread-safe event log backed by a deque with 2000 event limit, supports real-time listener callbacks for SSE streaming to the admin dashboard. Each event has a type, message, level (info/warning/error), timestamp, human-readable datetime, optional user info, and a metadata dict for domain-specific fields. Also writes to Python's logging module so events show up in CTFd's logs

## Configuration

All configuration is stored in the database via `DesktopSettingsModel` and managed through the admin web UI. No config files needed, on first load with an empty DB everything falls back to defaults which get seeded into the database automatically

### Docker contexts

Managed through the admin dashboard, each context has a name (matching a docker context on the host or just a label), an optional SSH hostname, a public hostname (what students see in VNC URLs), a weight for load balancing, and an enabled flag. A `local` context is auto-seeded on first boot when the Docker socket is available. Add, edit, delete, test connectivity, and reload connections all from the UI without restarting CTFd

### Default settings

| Key | Default | Description |
|-----|---------|-------------|
| docker_image | ctfd-remote-desktop:latest | container image to run for each desktop session |
| memory_limit | 4g | max memory per container |
| shm_size | 512m | shared memory size, needs to be large enough for the browser and desktop compositor |
| resolution | 1920x1080 | desktop resolution passed to the container as an env var |
| cpu_limit | 2 | max cpu cores per container |
| initial_duration | 3600 | how long a session lasts in seconds before it expires |
| extension_duration | 1800 | how many seconds each extension adds |
| max_extensions | 3 | how many times a student can extend their session |
| vnc_ready_attempts | 180 | number of http polls to wait for novnc to come up, each attempt is 0.5s apart |
| http_request_timeout | 3 | timeout in seconds for each novnc readiness poll |
| cleanup_interval | 300 | how often the scheduler scans for expired sessions in seconds |
| pids_limit | 512 | max number of processes per container, prevents fork bombs |
| max_concurrent_creates | 2 | how many containers can be created simultaneously on a single host |

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
- `POST /admin/api/extend` extend any session
- `GET /admin/api/events/stream` SSE
- `GET /admin/api/events/recent` event log

**Contexts**

- `GET /admin/api/contexts` list with live status and `is_local` flag
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

On startup the plugin queries all `DesktopContainerInfoModel` rows and checks each against Docker to see if the container is still running. Containers that are gone get their DB records deleted. Containers that are still alive get their orchestrator slots reserved so the load balancer has accurate counts from the start. If the Docker host is unreachable the record gets treated as stale and removed

This means a CTFd restart doesn't kill active student sessions, they survive and get picked back up automatically

## Troubleshooting

**Sessions not creating**: check that Docker contexts are configured and the image is pulled on all hosts, use the Test button in the admin context UI to verify connectivity and image availability

**VNC never becomes ready**: the plugin polls `http://{pub_hostname}:{novnc_port}/` up to 180 times at 0.5s intervals waiting for noVNC to respond, if the container takes longer to start you can increase `vnc_ready_attempts` in settings, also make sure the pub_hostname is reachable from wherever CTFd is running

**Sessions lost after restart**: this shouldn't happen anymore since state is in the database, if it does check the CTFd logs for reconciliation messages, you should see something like "reconciled containers on startup: N recovered, M stale records removed"

**Containers piling up on one host**: the orchestrator uses weighted least-connections scoring, check that your context weights are set appropriately in the admin UI, a context with weight 2 gets twice the score bonus compared to weight 1
