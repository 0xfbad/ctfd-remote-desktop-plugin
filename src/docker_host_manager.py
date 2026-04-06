import os
import json
import time
import threading
import logging

import docker
import paramiko

logger = logging.getLogger(__name__)

LOCAL_CONTEXT_NAME = "local"
LOCAL_SOCKET_PATH = "/var/run/docker.sock"


def parse_size(s):
    # human-readable size string to bytes, e.g. '4g' becomes 4294967296
    s = str(s).strip().lower()
    multipliers = {"k": 1024, "m": 1024**2, "g": 1024**3, "gb": 1024**3, "mb": 1024**2, "kb": 1024}
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(s)


def _scan_context_meta(context_name=None):
    """Read docker context metadata from ~/.docker/contexts/meta/.
    If context_name is given, return that context's meta dict or None.
    Otherwise return all contexts as a list of meta dicts."""
    contexts_dir = os.path.expanduser("~/.docker/contexts/meta")
    if not os.path.isdir(contexts_dir):
        return None if context_name else []

    results = []
    for entry in os.listdir(contexts_dir):
        meta_path = os.path.join(contexts_dir, entry, "meta.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            if context_name:
                if meta.get("Name") == context_name:
                    return meta
            else:
                results.append(meta)
        except Exception:
            continue

    return None if context_name else results


def _resolve_endpoint(context_name, hostname):
    # docker stores context dirs by hash, not name, so scan for a match
    meta = _scan_context_meta(context_name)
    if meta:
        endpoint = meta.get("Endpoints", {}).get("docker", {}).get("Host")
        if endpoint:
            return endpoint

    if hostname:
        if "@" in hostname:
            return f"ssh://{hostname}"
        return f"ssh://root@{hostname}"

    if os.path.exists(LOCAL_SOCKET_PATH):
        return f"unix://{LOCAL_SOCKET_PATH}"

    return None


def discover_contexts():
    """Scan the host for available docker contexts."""
    discovered = []
    for meta in _scan_context_meta():
        name = meta.get("Name", "")
        endpoint = meta.get("Endpoints", {}).get("docker", {}).get("Host", "")
        if name:
            discovered.append({"name": name, "endpoint": endpoint})

    if not any(d["name"] == LOCAL_CONTEXT_NAME for d in discovered):
        if os.path.exists(LOCAL_SOCKET_PATH):
            discovered.append({"name": LOCAL_CONTEXT_NAME, "endpoint": f"unix://{LOCAL_SOCKET_PATH}"})

    return discovered


def _get_host_gateway():
    """Get the docker host gateway IP from /proc/net/route (the default route).
    Falls back to localhost if it can't be determined."""
    try:
        import struct
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.strip().split()
                if parts[1] == "00000000":
                    gw = struct.pack("<I", int(parts[2], 16))
                    return ".".join(str(b) for b in gw)
    except Exception:
        pass
    return "localhost"


def ping_endpoint(endpoint, timeout=3):
    """Quick connectivity check for a docker endpoint."""
    try:
        client = docker.DockerClient(base_url=endpoint, timeout=timeout)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


class _ThreadLocalClients(threading.local):
    def __init__(self):
        super().__init__()
        self.clients = {}
        self.generation = -1


class DockerHostManager:
    def __init__(self):
        self._context_configs = {}  # {context_name: endpoint_url}
        self._pub_hostnames = {}  # {context_name: pub_hostname}
        self._thread_local = _ThreadLocalClients()
        self._config_generation = 0
        self._lock = threading.Lock()
        self._semaphores = {}

    def _get_client(self, context_name):
        tl = self._thread_local

        if tl.generation != self._config_generation:
            tl.clients = {}
            tl.generation = self._config_generation

        clients = tl.clients
        if context_name in clients:
            return clients[context_name]

        url = self._context_configs.get(context_name)
        if not url:
            raise Exception(f"no client for context '{context_name}'")

        client = docker.DockerClient(base_url=url)
        clients[context_name] = client
        return client

    def _clear_thread_local_client(self, context_name):
        self._thread_local.clients.pop(context_name, None)

    def _init_semaphores(self):
        from .models import get_setting

        limit = get_setting("max_concurrent_creates")

        new_semaphores = {}
        for ctx_name in self._context_configs:
            new_semaphores[ctx_name] = threading.BoundedSemaphore(limit)

        self._semaphores = new_semaphores

    def acquire_semaphore(self, context_name, timeout=30):
        sem = self._semaphores.get(context_name)
        if sem is None:
            return True

        acquired = sem.acquire(blocking=True, timeout=timeout)
        if not acquired:
            raise Exception("server busy, please try again shortly")
        return True

    def release_semaphore(self, context_name):
        sem = self._semaphores.get(context_name)
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                pass

    def load_contexts(self, contexts):
        """(re)connect all enabled contexts. contexts is a list of
        DesktopDockerContextModel rows."""
        new_configs = {}
        new_pub_hostnames = {}

        for ctx in contexts:
            endpoint = _resolve_endpoint(ctx.context_name, ctx.hostname)
            if not endpoint:
                logger.warning(f"no endpoint for context '{ctx.context_name}', skipping")
                continue

            try:
                client = docker.DockerClient(base_url=endpoint)
                client.ping()
                client.close()
                new_configs[ctx.context_name] = endpoint
                new_pub_hostnames[ctx.context_name] = ctx.pub_hostname
                logger.info(f"connected to context '{ctx.context_name}' at {endpoint}")
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException) as e:
                logger.error(f"could not connect to context '{ctx.context_name}': {e}")

        with self._lock:
            self._context_configs = new_configs
            self._pub_hostnames = new_pub_hostnames
            self._config_generation += 1

        self._init_semaphores()

    def get_pub_hostname(self, context_name):
        return self._pub_hostnames.get(context_name)

    def get_check_hostname(self, context_name):
        """Address to use for internal connectivity checks (VNC readiness etc).
        Local socket contexts use the docker host gateway since port mappings
        are on the host, not reachable via localhost from inside a container."""
        endpoint = self._context_configs.get(context_name, "")
        if endpoint.startswith("unix://"):
            return _get_host_gateway()
        return self._pub_hostnames.get(context_name)

    def get_connected_contexts(self):
        return list(self._context_configs.keys())

    def ping(self, context_name):
        try:
            client = self._get_client(context_name)
            client.ping()
            return True
        except Exception:
            self._clear_thread_local_client(context_name)
            return False

    def run_container(self, context_name, image, name, env, ports, shm_size=None, memory=None, nano_cpus=None):
        from .models import get_setting

        client = self._get_client(context_name)
        pids_limit = get_setting("pids_limit")

        port_bindings = {p: None for p in ports}

        try:
            container = client.containers.run(
                image,
                name=name,
                detach=True,
                auto_remove=True,
                environment=env,
                ports=port_bindings,
                shm_size=shm_size,
                mem_limit=memory,
                nano_cpus=nano_cpus,
                cap_drop=["ALL"],
                cap_add=["CHOWN", "SETUID", "SETGID", "FOWNER", "DAC_OVERRIDE", "NET_RAW", "NET_ADMIN", "SETFCAP"],
                pids_limit=pids_limit,
            )
        except docker.errors.DockerException:
            self._clear_thread_local_client(context_name)
            raise

        # poll for port mappings (container might take a moment to bind)
        port_map = {}
        for attempt in range(5):
            container.reload()
            network_ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})

            all_mapped = True
            for p in ports:
                bindings = network_ports.get(p)
                if bindings and len(bindings) > 0:
                    port_map[p] = int(bindings[0]["HostPort"])
                else:
                    all_mapped = False

            if all_mapped and port_map:
                break

            if attempt < 4:
                time.sleep(0.3)

        if not port_map:
            raise Exception(f"could not get port mappings for {name}")

        return {
            "container_id": container.id,
            "container_name": name,
            "ports": port_map,
        }

    def stop_container(self, context_name, container_name, timeout=10):
        try:
            client = self._get_client(context_name)
            try:
                container = client.containers.get(container_name)
                container.stop(timeout=timeout)
            except docker.errors.NotFound:
                # container already gone (auto-removed)
                logger.debug(f"container {container_name} already removed")
            except docker.errors.DockerException:
                self._clear_thread_local_client(context_name)
                raise
        except Exception as e:
            logger.error(f"error stopping container {container_name}: {e}")

    def check_image(self, context_name, image):
        try:
            client = self._get_client(context_name)
            client.images.get(image)
            return True
        except Exception:
            return False

    def is_container_running(self, context_name, container_id):
        try:
            client = self._get_client(context_name)
            container = client.containers.get(container_id)
            return container.status == "running"
        except docker.errors.NotFound:
            return False
        except docker.errors.DockerException:
            self._clear_thread_local_client(context_name)
            raise
