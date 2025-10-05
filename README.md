# Remote Desktop Plugin

CTFd plugin for provisioning on-demand remote desktop sessions across a distributed cluster of Docker hosts.

## Architecture

This plugin orchestrates containerized desktop environments across multiple remote hosts, using SSH for control plane communication and nginx for VNC traffic proxying. Container creation runs asynchronously via gevent greenlets to avoid blocking request handlers.

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
                      +-- nginx auth_request (validates, proxies VNC traffic)
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
5. Executes `docker run` with dynamic port mapping (0:5900, 0:6080)
6. Polls `docker port` to discover mapped ports
7. HTTP polls noVNC endpoint until ready (max 90s)
8. Updates `active_containers` dict and initializes session timer
9. Returns connection to pool

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

## Nginx Integration

The plugin relies on nginx auth_request subrequest pattern for access control and dynamic backend routing.

### VNC Proxy Flow

1. Client requests `/remote-desktop/vnc/123/websockify`
2. Nginx extracts user_id=123 from URL via regex capture
3. Nginx triggers internal subrequest to `/remote-desktop/auth-check` with `X-User-ID: 123` header
4. CTFd validates:
   - User is authenticated
   - User owns container 123 OR user is admin
   - Container exists
5. CTFd returns 200 with `X-VNC-Host: hostname` and `X-VNC-Port: 12345` headers
6. Nginx captures headers via `auth_request_set` and proxies to `http://$vnc_host:$vnc_port/websockify`
7. WebSocket upgrade headers preserved for VNC connection

Static assets (vnc.html, noVNC JS) follow same pattern but without WebSocket upgrade.

### Why This Pattern

- Nginx handles WebSocket proxying and load distribution
- CTFd maintains session state and authorization logic
- No need for CTFd to serve binary VNC traffic
- Per-request authorization without shared session store
- Dynamic backend routing without nginx config reloads

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
- Accept `VNC_PASSWORD` and `RESOLUTION` env vars
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

### Internal Endpoints
- `GET /remote-desktop/api/auth-check` - Nginx subrequest for VNC auth

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
