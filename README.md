# Remote Desktop Plugin

CTFd plugin that provisions on-demand Kali desktops across a pool of Docker hosts, students click a button and get a browser VNC session with per-container auth and automatic cleanup

## How it works

When a student requests a session the plugin picks the least-loaded healthy host, SSHs in, runs the container with dynamic port mapping, generates a random VNC password, and builds a direct noVNC URL with the password embedded as a query param so the browser auto-connects with no dialog. The whole thing runs in a gevent greenlet so it doesn't block the request thread, and the frontend polls for creation status updates

Students connect directly to the container's noVNC port on the runner host, no reverse proxy in the path. Admins can peek at any student's desktop from the dashboard using the same stored password

```
CTFd (gevent WSGI)
  |
  +-- ContainerManager (spawns gevent greenlets)
       |
       +-- HostOrchestrator (load balances across hosts)
            |
            +-- ConnectionPool[] (SSH to each Docker host)
                 |
                 +-- Remote Docker Hosts (run VNC containers)
                      |
                      +-- Students connect directly via noVNC URL with embedded password
```

## Container lifecycle

### Creation

1. User requests session via `/api/create`
2. Gevent greenlet spawns with Flask app context
3. Picks least-loaded healthy host
4. Checks out SSH connection from pool
5. Generates random VNC password via `secrets.token_urlsafe(6)[:8]`
6. Runs `docker run` with dynamic port mapping (0:5900, 0:6080), `VNC_PASSWORD`, `CTFD_USERNAME`, and `RESOLUTION` env vars
7. Polls `docker port` for mapped ports
8. HTTP polls noVNC until it responds (max ~90s)
9. Builds direct URL -- `http://{host}:{port}/vnc.html?autoconnect=true&password={pw}&resize=remote&reconnect=true`
10. Stores password and URL in `active_containers`, starts session timer

### Destruction

User or periodic cleanup triggers it, plugin SSHs to the host, runs `docker stop`, releases the host slot, and clears all timer and status state

### Session timers

Timers start on first status poll after the container is ready. Default is 3600s with up to 3 extensions of 1200s each, all configurable in `hosts.yml`. A background thread runs every 300s to scan for expired sessions and auto-destroy them

## VNC auth

The plugin generates a random 8-char password per container and passes it as `VNC_PASSWORD` to `docker run`. The container's startup script writes a VNC passwd file and launches Xvnc with `-SecurityTypes VncAuth`. The plugin then builds a direct URL with the password as a query param, noVNC reads it and sends it to VNC automatically so the student connects with zero interaction

VNC passwords are capped at 8 chars by the protocol, `secrets.token_urlsafe(6)[:8]` gives 48 bits of entropy which is plenty for preventing port-scan drive-bys in a classroom setting. The password shows up in the browser URL bar and history, fine for a lab environment

## Components

### Config

Loads `hosts.yml` with host definitions, container resource limits, and session timer defaults. Falls back to localhost if the config is missing

### ConnectionPool

Per-host paramiko SSH pool with max 20 connections, uses paramiko's default auth (SSH agent, default key locations, agent forwarding) so no explicit key paths needed. Validates connection health on checkout and checkin to handle stale TCP connections

### HostOrchestrator

Manages multiple connection pools and tracks per-host container counts, sorts hosts by active containers for least-loaded scheduling. On startup it tests every configured host for SSH connectivity, docker daemon access (`docker ps`), and image presence (`docker image inspect`), any host that fails gets marked unhealthy and pulled from rotation. Results show up in the admin event feed

### ContainerManager

Holds all the in-memory state for active containers, session timers, and creation status, everything keyed by user_id and protected by a single lock. Error handling in the creation path wraps SSH checkin, slot release, and health marking individually so a failure in one doesn't mask the others

### EventLogger

Thread-safe event log backed by a deque with 500 event limit, supports real-time listener callbacks for SSE streaming to the admin dashboard

## Concurrency

CTFd runs under gunicorn with gevent workers. Container creation uses `gevent.spawn()` to avoid blocking request threads during SSH and startup polling. State protection uses `threading.Lock` since greenlets within the same worker share memory. The cleanup thread runs via `threading.Thread(daemon=True)` with `Event.wait(300)` for cancellable sleep

All shared state is guarded by component-level locks -- ConnectionPool.lock for connection count and creation, ContainerManager.lock for active_containers/session_timers/creation_status, HostOrchestrator.global_lock for host counts and health, EventLogger.lock for the events deque and listeners list. Lock acquisition is never nested so there's no deadlock risk

## Configuration

### hosts.yml

```yaml
workspace_hosts:
  - hostname: host1.internal
    user: docker_user
    pub_hostname: host1.external.com

docker_image: ctfd-remote-desktop:latest

container_defaults:
  memory_limit: 4g
  shm_size: 2gb
  resolution: 1920x1080
  cpu_limit: 2

session_defaults:
  initial_duration: 3600
  extension_duration: 1200
  max_extensions: 3
```

### Container image requirements

The image needs to expose VNC on port 5900 and noVNC on port 6080, accept `CTFD_USERNAME` (sanitize it, CTFd display names can have spaces and special chars), `VNC_PASSWORD` (configure Xvnc with VncAuth, fall back to a random password if unset), and `RESOLUTION` env vars, and serve the noVNC web client at `/vnc.html` with a WebSocket endpoint at `/websockify`

## API endpoints

**User** -- `GET /remote-desktop` (main UI), `POST /api/create` (request session), `GET /api/creation-status` (poll progress), `GET /api/status` (current session), `POST /api/destroy` (destroy session), `POST /api/extend` (extend timer)

**Admin** -- `GET /admin` (dashboard), `GET /admin/api/containers` (list sessions), `POST /admin/api/kill` (force kill), `POST /admin/api/extend` (extend any session), `GET /admin/api/events/stream` (SSE), `GET /admin/api/events/recent` (event log)

All user endpoints are under `/remote-desktop/`, admin endpoints under `/remote-desktop/admin/`

## Host health

Hosts get marked unhealthy when the startup connectivity test fails (SSH, docker daemon, or missing image), when container creation fails, or when an SSH connection drops. Unhealthy hosts stay out of scheduling rotation but their connection pools remain active. There's no automatic recovery, you have to fix the host and restart CTFd

## Cleanup

A background thread scans session timers every 300s and auto-destroys expired containers. On shutdown, signal handlers (SIGTERM, SIGINT) spawn `cleanup_all_containers` via gevent with a 2s grace period, and there's an atexit handler as a fallback. All cleanup operations iterate over a snapshot of `active_containers` to avoid dict mutation during iteration
