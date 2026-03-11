# Remote Desktop Plugin

CTFd plugin for provisioning on-demand remote desktop sessions across a distributed cluster of Docker hosts.

## Architecture

This plugin orchestrates containerized desktop environments across multiple remote hosts, using SSH for control plane communication and per-container VNC passwords for access control. Container creation runs asynchronously via gevent greenlets to avoid blocking request handlers.

Students connect directly to the container's noVNC endpoint, the plugin generates a random password per container, passes it as an env var, and builds a URL with the password embedded as a query param so noVNC auto-connects with no dialog and no typing. Admins can peek at any student's desktop through the same stored password.

### System Topology

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

## Components

### Config
Loads `hosts.yml` containing workspace host definitions, container resource limits, and session timer defaults. Falls back to localhost if config is missing.

### ConnectionPool
Per-host paramiko SSH connection pool with max 20 connections. Uses paramiko's default SSH authentication (SSH agent, default key locations, agent forwarding). Validates connection health on checkout/checkin to handle stale TCP connections.

Lock contention minimized by keeping `available_connections.get()` call inside lock only when pool is exhausted, preventing connection count from exceeding max under concurrent load.

### HostOrchestrator
Manages multiple connection pools and tracks per-host container counts. Implements least-loaded scheduling by sorting hosts by active container count. Marks hosts unhealthy on failure to remove from rotation.

On startup, tests connectivity to all configured hosts:
- SSH connection test
- Docker daemon accessibility (`docker ps`)
- Docker image presence (`docker image inspect`)

Hosts that fail any check are marked unhealthy and excluded from scheduling. Results logged to admin event feed.

Single global lock protects all host state since operations are infrequent and contention is minimal.

### ContainerManager
Core state machine for container lifecycle:

**Creation flow:**
1. User requests session via `/api/create`
2. Spawns gevent greenlet with Flask app context
3. Background task selects least-loaded healthy host
4. Checks out SSH connection from pool
5. Generates random VNC password via `secrets.token_urlsafe(6)[:8]`
6. Executes `docker run` with dynamic port mapping (0:5900, 0:6080), `VNC_PASSWORD`, `CTFD_USERNAME`, and `RESOLUTION` env vars
7. Polls `docker port` to discover mapped ports
8. HTTP polls noVNC endpoint until ready (max 90s)
9. Builds direct URL `http://{host}:{port}/vnc.html?autoconnect=true&password={pw}&resize=remote&reconnect=true`
10. Stores password and URL in `active_containers` dict, initializes session timer
11. Returns connection to pool

**Destruction flow:**
1. User or periodic cleanup triggers destruction
2. SSH to host, execute `docker stop` and `docker rm`
3. Release host slot, clear timer and status

**Session timers:**
- Initial duration: 3600s (configurable)
- Extension duration: 1200s (configurable)
- Max extensions: 3 (configurable)
- Timer starts on first status poll after container ready
- Periodic cleanup (300s interval) auto-destroys expired sessions

All state mutations protected by single lock. Status dicts (active_containers, session_timers, creation_status) keyed by user_id.

Error handling in creation path individually wraps SSH checkin, slot release, and health marking to prevent cascading failures from masking root cause.

### EventLogger
Thread-safe event log using deque with 500 event limit. Supports real-time listener callbacks for SSE streaming to admin dashboard. Failed listeners are removed and logged.

## VNC Authentication

Students connect directly to the container's noVNC port on the runner host, no nginx proxy in the path. Access control is handled by VNC's native password auth rather than a reverse proxy layer.

### Connection Flow

1. Plugin generates a random 8-char password and passes it as `VNC_PASSWORD` to `docker run`
2. Container's startup script writes a VNC passwd file and launches Xvnc with `-SecurityTypes VncAuth`
3. Plugin builds a direct URL with the password as a query param, noVNC reads it and sends it to VNC automatically
4. Student gets the URL in an iframe, connects with zero interaction
5. Admin dashboard uses the same stored password to build peek URLs

### Security

VNC passwords are capped at 8 chars by the protocol, `secrets.token_urlsafe(6)[:8]` gives 48 bits of entropy which is plenty for preventing port-scan drive-bys in a classroom setting. The password appears in the browser URL bar and history, fine for a lab environment. Students who share their URL share their desktop, which is expected

## Concurrency Model

CTFd runs under gunicorn with gevent workers. This plugin uses:

**gevent.spawn()** for container creation to avoid blocking request threads during SSH operations and container startup polling.

**threading.Lock** for state protection since gevent greenlets within same worker share memory. All state dicts are guarded by component-level locks.

**Background cleanup thread** runs via `threading.Thread(daemon=True)` to periodically scan for expired sessions. Uses `Event.wait(300)` for cancellable sleep.

## Configuration

### hosts.yml Structure
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

### SSH Authentication
ConnectionPool uses paramiko's default SSH authentication which automatically tries:
- SSH agent (if available)
- Default key locations (~/.ssh/id_rsa, ~/.ssh/id_ed25519, etc.)
- SSH agent forwarding (if configured)

No explicit key paths needed. Works with standard SSH setups and deployment tools.

### Docker Container Requirements
Image must:
- Expose VNC on port 5900
- Expose noVNC on port 6080
- Accept `CTFD_USERNAME` env var and use it as the linux account name (sanitize it, CTFd display names can have spaces and special chars)
- Accept `VNC_PASSWORD` env var and configure Xvnc with VncAuth using it (fall back to a random password if not set)
- Accept `RESOLUTION` env var
- Serve noVNC web client at `/vnc.html`
- Provide WebSocket endpoint at `/websockify`

## API Endpoints

### User Endpoints
- `GET /remote-desktop` - Main UI page
- `POST /remote-desktop/api/create` - Request new session
- `GET /remote-desktop/api/creation-status` - Poll creation progress
- `GET /remote-desktop/api/status` - Get current session status
- `POST /remote-desktop/api/destroy` - Destroy current session
- `POST /remote-desktop/api/extend` - Extend session timer

### Admin Endpoints
- `GET /remote-desktop/admin` - Admin dashboard
- `GET /remote-desktop/admin/api/containers` - List all active sessions
- `POST /remote-desktop/admin/api/kill` - Force kill any user session
- `POST /remote-desktop/admin/api/extend` - Extend any user session
- `GET /remote-desktop/admin/api/events/stream` - SSE event stream
- `GET /remote-desktop/admin/api/events/recent` - Recent event log

## Host Health Management

Hosts are marked unhealthy when:
- Startup connectivity test fails (SSH, docker daemon, or image missing)
- Container creation fails
- SSH connection fails

Startup tests check each host for:
- SSH connectivity
- Docker daemon accessibility
- Required image presence (does not pull automatically)

Unhealthy hosts are excluded from scheduling but pools remain active. Currently no automatic recovery mechanism - requires manual intervention via admin API.

## Cleanup Mechanisms

**Periodic cleanup (300s):**
Scans session_timers for expired entries and auto-destroys containers.

**Signal handlers (SIGTERM, SIGINT):**
Spawns cleanup_all_containers via gevent with 2s grace period before exit.

**atexit handler:**
Calls cleanup_all_containers to stop all containers on process shutdown.

All cleanup operations iterate over snapshot of active_containers to avoid dict mutation during iteration.

## Thread Safety

All shared state protected by component-level locks:
- ConnectionPool.lock - connection count and creation
- ContainerManager.lock - active_containers, session_timers, creation_status
- HostOrchestrator.global_lock - host_container_counts, host_health
- EventLogger.lock - events deque, listeners list

Lock acquisition order is deterministic (never nested) to prevent deadlock.

## Extension Points

To add custom session lifecycle hooks:
- Add listener to EventLogger for session_created, session_destroyed, session_expired events
- Subscribe to admin events stream for real-time monitoring
- Extend ContainerManager timer logic for custom expiration policies
