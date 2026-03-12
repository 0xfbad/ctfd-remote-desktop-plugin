# Remote Desktop Plugin

CTFd plugin that provisions on-demand Kali desktops across a pool of Docker hosts, students click a button and get a browser VNC session with per-container auth and automatic cleanup

## How it works

When a student requests a session the plugin picks the least-loaded healthy Docker context, hits the Docker API over an SSH tunnel via aiodocker, runs the container with dynamic port mapping, generates a random VNC password, and builds a direct noVNC URL with the password embedded as a query param so the browser auto-connects with no dialog. The whole thing runs in a gevent greenlet so it doesn't block the request thread, and the frontend polls for creation status updates

Students connect directly to the container's noVNC port on the runner host, no reverse proxy in the path. Admins can peek at any student's desktop from the dashboard using the same stored password

## Container lifecycle

### Creation

1. User requests session via `/api/create`
2. Gevent greenlet spawns with Flask app context
3. Orchestrator picks least-loaded healthy context (weighted)
4. Generates random VNC password via `secrets.token_urlsafe(6)[:8]`
5. Calls `DockerHostManager.run_container()` which hits the Docker API through aiodocker's SSH tunnel, creates the container with dynamic port mapping (0:5900, 0:6080), `VNC_PASSWORD`, `CTFD_USERNAME`, and `RESOLUTION` env vars
6. Inspects container for mapped ports via the Docker API
7. HTTP polls noVNC until it responds (configurable attempts, default 180)
8. Builds direct URL like `http://{pub_hostname}:{port}/vnc.html?autoconnect=true&password={pw}&resize=remote&reconnect=true`
9. Stores password and URL in `active_containers`, starts session timer

### Destruction

User or periodic cleanup triggers it, plugin calls `DockerHostManager.stop_container()` which hits the Docker API to stop the container, releases the context slot, and clears all timer and status state

### Session timers

Timers start on first status poll after the container is ready. Default is 3600s with up to 3 extensions of 1800s each, all configurable via the admin web UI without restart. A background thread runs on a configurable interval (default 300s) to scan for expired sessions and auto-destroy them

## VNC auth

The plugin generates a random 8-char password per container and passes it as `VNC_PASSWORD` to the container. The container's startup script writes a VNC passwd file and launches Xvnc with `-SecurityTypes VncAuth`. The plugin then builds a direct URL with the password as a query param, noVNC reads it and sends it to VNC automatically so the student connects with zero interaction

VNC passwords are capped at 8 chars by the protocol, `secrets.token_urlsafe(6)[:8]` gives 48 bits of entropy which is plenty for preventing port-scan drive-bys in a classroom setting. The password shows up in the browser URL bar and history, fine for a lab environment

## Components

### DockerHostManager

Manages aiodocker.Docker clients for each configured docker context, one persistent SSH tunnel per host that stays open for the lifetime of the client object so there's no connection pool to deal with. Context loading queries `DesktopDockerContextModel` for enabled entries, tries resolving the endpoint from the docker context meta file at `~/.docker/contexts/meta/{name}/meta.json` first, falls back to `ssh://{hostname}` from the DB record. All async Docker API calls go through an `AsyncBridge` that runs an asyncio event loop in a real OS thread (using `gevent.monkey.get_original('threading', 'Thread')` to bypass monkey-patching), sync callers submit coroutines via `asyncio.run_coroutine_threadsafe()` and block on the future which is gevent-safe because `concurrent.futures.Future` uses patched threading primitives for its wait

### Orchestrator

Tracks per-context container counts, health status, and weights, picks the next context via weighted least-connections (container count divided by weight, lowest score wins). On `load_from_db()` it queries enabled contexts, tells DockerHostManager to connect, then pings each context and checks for the configured docker image. Contexts that fail either step get marked unhealthy and pulled from rotation. Results show up in the admin event feed

### ContainerManager

Holds all the in-memory state for active containers, session timers, and creation status, everything keyed by user_id and protected by a single lock. Stores `docker_context` per session so it knows which context to hit for stop operations even if the context list changes. Error handling in the creation path wraps slot release and health marking individually so a failure in one doesn't mask the others

### EventLogger

Thread-safe event log backed by a deque with 2000 event limit, supports real-time listener callbacks for SSE streaming to the admin dashboard

## Configuration

All configuration is stored in the database via `DesktopSettingsModel` and managed through the admin web UI. No config files needed, on first load with an empty DB everything falls back to defaults

### Docker contexts

Managed through the admin dashboard, each context has a name (matching a docker context on the host or just a label), an optional SSH hostname, a public hostname (what students see in VNC URLs), a weight for load balancing, and an enabled flag. Add, edit, delete, test connectivity, and reload connections all from the UI without restarting CTFd

### Default settings

| Key | Default |
|-----|---------|
| docker_image | ctfd-remote-desktop:latest |
| memory_limit | 4g |
| shm_size | 512m |
| resolution | 1920x1080 |
| cpu_limit | 2 |
| initial_duration | 3600 |
| extension_duration | 1800 |
| max_extensions | 3 |
| vnc_ready_attempts | 180 |
| http_request_timeout | 3 |
| cleanup_interval | 300 |

### Container image requirements

The image needs to expose VNC on port 5900 and noVNC on port 6080, accept `CTFD_USERNAME` (sanitize it, CTFd display names can have spaces and special chars), `VNC_PASSWORD` (configure Xvnc with VncAuth, fall back to a random password if unset), and `RESOLUTION` env vars, and serve the noVNC web client at `/vnc.html` with a WebSocket endpoint at `/websockify`

## API endpoints

**User**: `GET /remote-desktop` (main UI), `POST /api/create` (request session), `GET /api/creation-status` (poll progress), `GET /api/status` (current session), `POST /api/destroy` (destroy session), `POST /api/extend` (extend timer)

**Admin**: `GET /admin` (dashboard), `GET /admin/api/containers` (list sessions), `POST /admin/api/kill` (force kill), `POST /admin/api/extend` (extend any session), `GET /admin/api/events/stream` (SSE), `GET /admin/api/events/recent` (event log)

**Contexts**: `GET /admin/api/contexts` (list with live status), `POST /admin/api/contexts` (add), `PUT /admin/api/contexts/<id>` (update), `DELETE /admin/api/contexts/<id>` (delete), `GET /admin/api/contexts/<id>/test` (ping + image check), `POST /admin/api/contexts/reload` (reconnect all)

**Settings**: `GET /admin/api/settings` (all settings as JSON), `PUT /admin/api/settings` (bulk upsert)

All user endpoints are under `/remote-desktop/`, admin endpoints under `/remote-desktop/admin/`

## Concurrency

CTFd runs under gunicorn with gevent workers. Container creation uses `gevent.spawn()` to avoid blocking request threads during Docker API calls and startup polling. State protection uses `threading.Lock` since greenlets within the same worker share memory. The cleanup thread runs via `threading.Thread(daemon=True)` with `Event.wait()` for cancellable sleep

The AsyncBridge runs in a real OS thread (not a gevent greenlet) so the asyncio event loop doesn't conflict with gevent's monkey-patched threading. aiodocker maintains persistent SSH tunnels per host so there's no connection pool or per-request SSH overhead

All shared state is guarded by component-level locks: ContainerManager.lock for active_containers/session_timers/creation_status, Orchestrator.lock for container counts and health, EventLogger.lock for the events deque and listeners list. Lock acquisition is never nested so there's no deadlock risk

## Context health

Contexts get marked unhealthy when the connectivity test fails (SSH tunnel, docker daemon ping, or missing image) or when container creation fails. Unhealthy contexts stay out of scheduling rotation. Unlike the old SSH-based system you can hit the Reload button in the admin UI to reconnect everything without restarting CTFd

## Cleanup

A background thread scans session timers on a configurable interval (default 300s) and auto-destroys expired containers. On shutdown, signal handlers (SIGTERM, SIGINT) spawn `cleanup_all_containers` via gevent with a 2s grace period, and there's an atexit handler as a fallback. All cleanup operations iterate over a snapshot of `active_containers` to avoid dict mutation during iteration, and container stops go through the Docker API which handles the case where a container is already gone (auto-removed) gracefully
