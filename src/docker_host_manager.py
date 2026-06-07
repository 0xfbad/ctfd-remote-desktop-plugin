from __future__ import annotations

import os
import json
import time
import threading
import logging
from datetime import datetime
import docker
import gevent.monkey
import gevent.threadpool
import paramiko

from .models import DesktopDockerContextModel, DISPLAY_DATETIME_FORMAT
from .exceptions import HostsUnavailableException

logger = logging.getLogger(__name__)

LOCAL_CONTEXT_NAME = "local"
LOCAL_SOCKET_PATH = "/var/run/docker.sock"
DOCKER_CONFIG_DIR = os.environ.get("DOCKER_CONFIG", os.path.expanduser("~/.docker"))

# docker SDK HTTP read timeout for control plane ops
DEFAULT_CLIENT_TIMEOUT = 10
# per-context pool size, caps concurrent in-flight blocking calls per host
THREADPOOL_SIZE = 4

ContextMeta = dict[str, str | dict[str, dict[str, str]]]
DiscoveredContext = dict[str, str]
ContainerResult = dict[str, str | dict[str, int]]
ImageInfo = dict[str, int | str]


def parse_size(s: str | int) -> int:
    s = str(s).strip().lower()
    multipliers = {"k": 1024, "m": 1024**2, "g": 1024**3, "gb": 1024**3, "mb": 1024**2, "kb": 1024}
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(s)


def _scan_context_meta(context_name: str | None = None) -> ContextMeta | list[ContextMeta] | None:
    contexts_dir = os.path.join(DOCKER_CONFIG_DIR, "contexts", "meta")
    if not os.path.isdir(contexts_dir):
        return None if context_name else []

    results: list[ContextMeta] = []
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


def _resolve_endpoint(context_name: str, hostname: str | None) -> str | None:
    # docker stores context dirs by hash, not name, so scan for a match
    meta = _scan_context_meta(context_name)
    if meta:
        endpoint = meta.get("Endpoints", {}).get("docker", {}).get("Host")  # type: ignore[union-attr]
        if endpoint:
            return endpoint

    if hostname:
        if "@" in hostname:
            return f"ssh://{hostname}"
        return f"ssh://root@{hostname}"

    if os.path.exists(LOCAL_SOCKET_PATH):
        return f"unix://{LOCAL_SOCKET_PATH}"

    return None


def discover_contexts() -> list[DiscoveredContext]:
    discovered: list[DiscoveredContext] = []
    for meta in _scan_context_meta():  # type: ignore[union-attr]
        name = meta.get("Name", "")  # type: ignore[union-attr]
        endpoint = meta.get("Endpoints", {}).get("docker", {}).get("Host", "")  # type: ignore[union-attr]
        if name:
            discovered.append({"name": str(name), "endpoint": str(endpoint)})

    if not any(d["name"] == LOCAL_CONTEXT_NAME for d in discovered):
        if os.path.exists(LOCAL_SOCKET_PATH):
            discovered.append({"name": LOCAL_CONTEXT_NAME, "endpoint": f"unix://{LOCAL_SOCKET_PATH}"})

    return discovered


def _get_host_gateway() -> str:
    # default route gateway from /proc, needed for reaching container ports on the host
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


def ping_endpoint(endpoint: str, timeout: int = 3) -> bool:
    try:
        client = docker.DockerClient(base_url=endpoint, timeout=timeout)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


class DockerHostManager:
    def __init__(self) -> None:
        self._context_configs: dict[str, str] = {}
        self._pub_hostnames: dict[str, str] = {}
        # keyed by (context_name, thread_ident) because paramiko Channels bind
        # gevent.Event to the Hub of the creating thread
        self._clients: dict[tuple[str, int], docker.DockerClient] = {}
        self._config_generation: int = 0
        self._client_generation: int = -1
        # reentrant so wrapped ops can re-enter lock-protected helpers
        self._lock: threading.RLock = threading.RLock()
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        # per-context pool isolates blocking paramiko calls so one hung host
        # doesn't starve the others
        self._threadpools: dict[str, gevent.threadpool.ThreadPool] = {}

    def _get_threadpool(self, context_name: str) -> gevent.threadpool.ThreadPool:
        with self._lock:
            pool = self._threadpools.get(context_name)
            if pool is None:
                pool = gevent.threadpool.ThreadPool(maxsize=THREADPOOL_SIZE)
                self._threadpools[context_name] = pool
            return pool

    def _call(self, context_name: str, fn, *args, **kwargs):
        # pool.apply needs the gevent hub. cli paths (flask db upgrade) have no
        # hub and apply() hangs in futex, so fall back to inline there
        if not gevent.monkey.is_module_patched("threading"):
            return fn(*args, **kwargs)
        pool = self._get_threadpool(context_name)
        return pool.apply(fn, args=args, kwds=kwargs)

    def _get_client(self, context_name: str) -> docker.DockerClient:
        tid = threading.get_ident()
        to_close: list[docker.DockerClient] = []
        with self._lock:
            if self._client_generation != self._config_generation:
                to_close.extend(self._clients.values())
                self._clients = {}
                self._client_generation = self._config_generation
            else:
                # prune entries for dead threads. gevent threadpool workers
                # rarely die so this is a cheap safety net, not a hot path.
                # bounded by num_contexts * THREADPOOL_SIZE (3 * 4 = 12)
                live_idents = {t.ident for t in threading.enumerate()}
                dead_keys = [k for k in self._clients if k[1] not in live_idents]
                for k in dead_keys:
                    to_close.append(self._clients.pop(k))

            key = (context_name, tid)
            if key in self._clients:
                client = self._clients[key]
            else:
                url = self._context_configs.get(context_name)
                if not url:
                    # typed so callers can map a stale-row miss to a 503 instead of a 500
                    raise HostsUnavailableException(f"no client for context '{context_name}'")
                client = docker.DockerClient(base_url=url, timeout=DEFAULT_CLIENT_TIMEOUT)
                self._clients[key] = client

        # close outside the lock, paramiko teardown can block on SSH for seconds
        for old in to_close:
            try:
                old.close()
            except Exception:
                pass
        return client

    def _clear_client(self, context_name: str) -> None:
        # drop EVERY cached client for this context across all threads so any
        # worker that next calls _get_client builds a fresh one. preserves the
        # original contract (next call gets a new client) but accounts for N
        # cached entries instead of 1
        to_close: list[docker.DockerClient] = []
        with self._lock:
            keys = [k for k in self._clients if k[0] == context_name]
            for k in keys:
                to_close.append(self._clients.pop(k))
        for old in to_close:
            try:
                old.close()
            except Exception:
                pass

    def _init_semaphores(self) -> None:
        from .models import get_setting

        limit = get_setting("max_concurrent_creates")

        new_semaphores: dict[str, threading.BoundedSemaphore] = {}
        for ctx_name in self._context_configs:
            new_semaphores[ctx_name] = threading.BoundedSemaphore(int(limit))  # type: ignore[arg-type]

        self._semaphores = new_semaphores

    def acquire_semaphore(self, context_name: str, timeout: int = 10) -> bool:
        sem = self._semaphores.get(context_name)
        if sem is None:
            return True

        acquired = sem.acquire(blocking=True, timeout=timeout)
        if not acquired:
            raise Exception("server busy, please try again shortly")
        return True

    def release_semaphore(self, context_name: str) -> None:
        sem = self._semaphores.get(context_name)
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                pass

    def load_contexts(self, contexts: list[DesktopDockerContextModel]) -> None:
        from .models import get_setting

        new_configs: dict[str, str] = {}
        new_pub_hostnames: dict[str, str] = {}

        rd_network = str(get_setting("rd_network_name") or "rd-isolated")

        for ctx in contexts:
            endpoint = _resolve_endpoint(ctx.context_name, ctx.hostname)
            if not endpoint:
                logger.warning(f"no endpoint for context '{ctx.context_name}', skipping")
                continue

            def _check(endpoint=endpoint, ctx_name=ctx.context_name):
                client = None
                try:
                    client = docker.DockerClient(base_url=endpoint, timeout=DEFAULT_CLIENT_TIMEOUT)
                    client.ping()
                    # bridge is the docker default and always present, skip the probe so an operator
                    # using bridge as an emergency rollback doesn't see spurious warnings
                    if rd_network != "bridge":
                        try:
                            found = client.networks.list(names=[rd_network])
                            if not found:
                                logger.warning(
                                    f"context {ctx_name} missing docker network '{rd_network}' "
                                    "- container creates will fail. run the rd-isolated network "
                                    "runbook on this host"
                                )
                        except Exception as e:
                            logger.warning(f"context {ctx_name} network check failed for '{rd_network}': {e}")
                    return None
                except (docker.errors.DockerException, paramiko.ssh_exception.SSHException) as e:
                    return e
                finally:
                    if client:
                        try:
                            client.close()
                        except Exception:
                            pass

            try:
                err = self._call(ctx.context_name, _check)
            except Exception as e:
                err = e

            if err is None:
                new_configs[ctx.context_name] = endpoint
                new_pub_hostnames[ctx.context_name] = ctx.pub_hostname
                logger.info(f"connected to context '{ctx.context_name}' at {endpoint}")
            else:
                logger.error(f"could not connect to context '{ctx.context_name}': {err}")

        with self._lock:
            self._context_configs = new_configs
            self._pub_hostnames = new_pub_hostnames
            self._config_generation += 1

        self._init_semaphores()

    def get_pub_hostname(self, context_name: str) -> str | None:
        return self._pub_hostnames.get(context_name)

    def get_check_hostname(self, context_name: str) -> str | None:
        # local socket contexts need the host gateway since ports bind on the host, not localhost
        endpoint = self._context_configs.get(context_name, "")
        if endpoint.startswith("unix://"):
            return _get_host_gateway()
        return self._pub_hostnames.get(context_name)

    def get_connected_contexts(self) -> list[str]:
        return list(self._context_configs.keys())

    def ping(self, context_name: str) -> bool:
        def _do():
            try:
                client = self._get_client(context_name)
                client.ping()
                return True
            except Exception:
                self._clear_client(context_name)
                return False

        return self._call(context_name, _do)

    def run_container(
        self,
        context_name: str,
        image: str,
        name: str,
        env: dict[str, str],
        ports: list[str],
        shm_size: int | None = None,
        memory: int | None = None,
        nano_cpus: int | None = None,
        hostname: str | None = None,
        extra_hosts: dict[str, str] | None = None,
        network: str | None = None,
    ) -> ContainerResult:
        from .models import get_setting

        pids_limit = get_setting("pids_limit")
        cap_drop = [c.strip() for c in str(get_setting("cap_drop")).split(",") if c.strip()]
        cap_add = [c.strip() for c in str(get_setting("cap_add")).split(",") if c.strip()]

        import secrets

        _sysrand = secrets.SystemRandom()

        def _do():
            client = self._get_client(context_name)
            last_err: Exception | None = None
            container = None
            for _ in range(50):
                port_bindings = {p: _sysrand.randint(40000, 59999) for p in ports}
                try:
                    container = client.containers.run(
                        image,
                        name=name,
                        hostname=hostname or name,
                        detach=True,
                        auto_remove=True,
                        environment=env,
                        ports=port_bindings,
                        shm_size=shm_size,
                        mem_limit=memory,
                        nano_cpus=nano_cpus,
                        cap_drop=cap_drop,
                        cap_add=cap_add,
                        pids_limit=pids_limit,
                        extra_hosts=extra_hosts or {},
                        network=network,
                    )
                    break
                except docker.errors.APIError as e:
                    if "port is already allocated" in str(e) or "address already in use" in str(e):
                        last_err = e
                        continue
                    self._clear_client(context_name)
                    raise
                except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                    self._clear_client(context_name)
                    raise
            else:
                raise docker.errors.DockerException(f"failed to find available ports after retries: {last_err}")

            port_map: dict[str, int] = {}
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

        return self._call(context_name, _do)

    def stop_container(self, context_name: str, container_name: str, timeout: int = 10) -> None:
        def _do():
            client = self._get_client(context_name)
            try:
                container = client.containers.get(container_name)
                container.stop(timeout=timeout)
            except docker.errors.NotFound:
                logger.debug(f"container {container_name} already removed")
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                raise
            except Exception:
                # see is_container_running for context on the broad catch
                self._clear_client(context_name)
                raise HostsUnavailableException(f"transient client failure on {context_name}")

        return self._call(context_name, _do)

    def force_remove_container(self, context_name: str, container_name: str) -> None:
        # stop() is a no-op against Created-state containers (never started, so nothing to stop) and they don't
        # auto_remove from a no-op stop, so reconciler-style cleanup needs remove(force=True) instead.
        # also covers Running and Exited in one call without the stop->auto_remove timing dance
        def _do():
            client = self._get_client(context_name)
            try:
                container = client.containers.get(container_name)
                container.remove(force=True)
            except docker.errors.NotFound:
                logger.debug(f"container {container_name} already removed")
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                raise
            except Exception:
                # see is_container_running for context on the broad catch
                self._clear_client(context_name)
                raise HostsUnavailableException(f"transient client failure on {context_name}")

        return self._call(context_name, _do)

    def list_containers_by_prefix(self, context_name: str, name_prefix: str) -> list[dict[str, str | float]]:
        # used by the reconcile sweep; returns [{"name": str, "created_ts": float}] for matching containers (any state).
        # swallows errors and returns [] so a flapping host can't break the sweep loop
        def _do() -> list[dict[str, str | float]]:
            try:
                client = self._get_client(context_name)
                containers = client.containers.list(all=True, filters={"name": name_prefix})
                results: list[dict[str, str | float]] = []
                for c in containers:
                    created_raw = c.attrs.get("Created", "") if c.attrs else ""
                    created_ts = 0.0
                    if created_raw:
                        try:
                            from datetime import datetime

                            iso = created_raw.replace("Z", "+00:00")
                            # strip nanoseconds past microsecond precision (docker emits 9-digit fractional)
                            if "." in iso:
                                head, tail = iso.split(".", 1)
                                tz_idx = max(tail.find("+"), tail.find("-"))
                                if tz_idx == -1:
                                    frac, tz_suffix = tail, ""
                                else:
                                    frac, tz_suffix = tail[:tz_idx], tail[tz_idx:]
                                iso = f"{head}.{frac[:6]}{tz_suffix}"
                            created_ts = datetime.fromisoformat(iso).timestamp()
                        except (ValueError, AttributeError):
                            created_ts = 0.0
                    results.append({"name": c.name or "", "created_ts": created_ts})
                return results
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                return []
            except Exception:
                self._clear_client(context_name)
                return []

        return self._call(context_name, _do)

    def check_image(self, context_name: str, image: str) -> bool:
        def _do():
            try:
                client = self._get_client(context_name)
                client.images.get(image)
                return True
            except docker.errors.ImageNotFound:
                return False
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                return False
            except Exception:
                self._clear_client(context_name)
                return False

        return self._call(context_name, _do)

    def get_image_info(self, context_name: str, image: str) -> ImageInfo | None:
        def _do():
            try:
                client = self._get_client(context_name)
                img = client.images.get(image)
                attrs = img.attrs or {}
                size_mb = round((attrs.get("Size") or 0) / 1024 / 1024)
                raw = attrs.get("Created", "")[:19]
                # reproducible-build images (nix, bazel) report 1980-01-01,
                # fall back to LastTagTime for a meaningful date
                if raw.startswith("1980"):
                    last_tag = (attrs.get("Metadata") or {}).get("LastTagTime", "")
                    if last_tag:
                        raw = last_tag[:19]
                try:
                    created = datetime.strptime(raw.replace("T", " "), "%Y-%m-%d %H:%M:%S").strftime(
                        DISPLAY_DATETIME_FORMAT
                    )
                except (ValueError, AttributeError):
                    created = raw.replace("T", " ")
                short_id = img.short_id.replace("sha256:", "")
                return {"size_mb": size_mb, "created": created, "id": short_id}
            except docker.errors.ImageNotFound:
                return None
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                return None
            except Exception:
                self._clear_client(context_name)
                return None

        return self._call(context_name, _do)

    def exec_in_container(self, context_name: str, container_name_or_id: str, cmd: list[str]) -> tuple[int, str]:
        def _do():
            try:
                client = self._get_client(context_name)
                container = client.containers.get(container_name_or_id)
                exit_code, output = container.exec_run(cmd)
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                return exit_code, output
            except docker.errors.NotFound:
                return -1, ""
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                return -1, ""
            except Exception:
                self._clear_client(context_name)
                return -1, ""

        return self._call(context_name, _do)

    def is_container_running(self, context_name: str, container_id: str) -> bool:
        def _do():
            try:
                client = self._get_client(context_name)
                container = client.containers.get(container_id)
                return container.status == "running"
            except docker.errors.NotFound:
                return False
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException, EOFError, OSError):
                # raw EOFError surfaces when ssh MaxSessions is exhausted
                self._clear_client(context_name)
                raise
            except Exception:
                # gevent.InvalidThreadUseError, paramiko ChannelException etc surface when the cached
                # client is reused from a different gevent hub. drop the client and surface as a typed
                # transient so _verify_or_reap treats it optimistically instead of 500'ing the route
                self._clear_client(context_name)
                raise HostsUnavailableException(f"transient client failure on {context_name}")

        return self._call(context_name, _do)
