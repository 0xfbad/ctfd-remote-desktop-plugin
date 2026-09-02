from __future__ import annotations

import os
import json
import time
import threading
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
import docker
import gevent.monkey
import gevent.threadpool
import paramiko

from .models import DesktopDockerContextModel, DISPLAY_DATETIME_FORMAT
from .exceptions import HostsUnavailableException
from .utils import normalize_public_hostname

logger = logging.getLogger(__name__)

SESSION_LABEL_MANAGED = "org.ctfd.remote-desktop.managed"
SESSION_LABEL_USER_ID = "org.ctfd.remote-desktop.user-id"
SESSION_LABEL_UUID = "org.ctfd.remote-desktop.session-uuid"

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
ContainerState = Literal["running", "paused", "created", "exited", "not_found", "unknown"]


def normalize_container_state(value: object) -> ContainerState:
    """Map Docker's open-ended status strings to the plugin's closed state set."""
    if value == "running":
        return "running"
    if value == "paused":
        return "paused"
    if value == "created":
        return "created"
    if value in ("exited", "dead", "removing"):
        return "exited"
    return "unknown"


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


def _metadata_name_and_endpoint(meta: object) -> tuple[str | None, str | None]:
    """Extract only the typed fields used from Docker's untrusted JSON shape."""
    if not isinstance(meta, Mapping):
        return None, None
    name = meta.get("Name")
    endpoints = meta.get("Endpoints")
    if not isinstance(endpoints, Mapping):
        return name if isinstance(name, str) and name else None, None
    docker_endpoint = endpoints.get("docker")
    if not isinstance(docker_endpoint, Mapping):
        return name if isinstance(name, str) and name else None, None
    endpoint = docker_endpoint.get("Host")
    return (
        name if isinstance(name, str) and name else None,
        endpoint if isinstance(endpoint, str) and endpoint else None,
    )


def _validate_endpoint(candidate: str, context_name: str) -> str | None:
    """Allow only the one local socket or a well-formed SSH transport.

    Docker context metadata is operator-controlled state mounted into CTFd, but
    it must not silently widen the control plane to unauthenticated TCP daemons
    or arbitrary Unix sockets. Keep metadata and manually-entered endpoints on
    the same allowlist.
    """
    if not candidate or candidate != candidate.strip() or any(c.isspace() for c in candidate):
        return None
    if candidate.startswith("unix://"):
        if context_name == LOCAL_CONTEXT_NAME and candidate == f"unix://{LOCAL_SOCKET_PATH}":
            return candidate
        return None
    try:
        parsed = urlsplit(candidate)
        parsed_port = parsed.port  # force validation of malformed/out-of-range ports
        parsed_hostname = parsed.hostname
        parsed_username = parsed.username
        parsed_password = parsed.password
    except (UnicodeError, ValueError):
        return None
    has_userinfo = "@" in parsed.netloc
    if (
        parsed.scheme != "ssh"
        or not parsed_hostname
        or parsed.netloc.endswith(":")
        or (parsed_port is not None and parsed_port < 1)
        or (has_userinfo and (not parsed_username or "@" in parsed_username or parsed.netloc.count("@") != 1))
        or parsed_password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        return None
    return candidate


def _resolve_endpoint(context_name: str, hostname: str | None) -> str | None:
    # docker stores context dirs by hash, not name, so scan for a match
    meta = _scan_context_meta(context_name)
    if meta:
        _name, endpoint = _metadata_name_and_endpoint(meta)
        if endpoint is not None:
            validated = _validate_endpoint(endpoint, context_name)
            if validated is not None:
                return validated

    if hostname:
        # The admin API also accepts an explicit Docker endpoint for manually
        # configured contexts. Do not corrupt ssh://host into
        # ssh://root@ssh://host when context metadata is unavailable.
        candidate = hostname if "://" in hostname else f"ssh://{hostname if '@' in hostname else f'root@{hostname}'}"
        validated = _validate_endpoint(candidate, context_name)
        if validated is not None:
            return validated

    if context_name == LOCAL_CONTEXT_NAME and os.path.exists(LOCAL_SOCKET_PATH):
        return f"unix://{LOCAL_SOCKET_PATH}"

    return None


def discover_contexts() -> list[DiscoveredContext]:
    discovered: list[DiscoveredContext] = []
    metadata = _scan_context_meta()
    for meta in metadata if isinstance(metadata, list) else []:
        name, endpoint = _metadata_name_and_endpoint(meta)
        if name is not None and endpoint is not None:
            validated = _validate_endpoint(endpoint, name)
            if validated is not None:
                discovered.append({"name": name, "endpoint": validated})

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
        # pool.apply needs the gevent hub. CLI paths have no
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

    def _init_semaphores(self, limit: int) -> None:
        new_semaphores: dict[str, threading.BoundedSemaphore] = {}
        for ctx_name in self._context_configs:
            new_semaphores[ctx_name] = threading.BoundedSemaphore(limit)

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
        from .models import get_all_settings

        new_configs: dict[str, str] = {}
        new_pub_hostnames: dict[str, str] = {}

        effective_profile = get_all_settings()
        rd_network = str(effective_profile["rd_network_name"] or "bridge")
        storage_limit = str(effective_profile["storage_limit"] or "").strip()

        for ctx in contexts:
            endpoint = _resolve_endpoint(ctx.context_name, ctx.hostname)
            if not endpoint:
                logger.warning(f"no endpoint for context '{ctx.context_name}', skipping")
                continue
            try:
                public_hostname = normalize_public_hostname(ctx.pub_hostname)
            except ValueError as exc:
                logger.error(f"invalid public hostname for context '{ctx.context_name}': {exc}")
                continue

            def _check(endpoint=endpoint, ctx_name=ctx.context_name):
                client = None
                try:
                    client = docker.DockerClient(base_url=endpoint, timeout=DEFAULT_CLIENT_TIMEOUT)
                    client.ping()
                    if rd_network != "bridge":
                        try:
                            found = client.networks.list(names=[rd_network])
                            if not found:
                                logger.warning(
                                    f"context {ctx_name} missing docker network '{rd_network}' "
                                    "- container creates will fail"
                                )
                        except Exception as e:
                            logger.warning(f"context {ctx_name} network check failed for '{rd_network}': {e}")
                    if storage_limit:
                        # storage_opt is a silent brick on non-xfs data-roots: the
                        # daemon refuses every create. surface it at load instead
                        try:
                            backing = dict(client.info().get("DriverStatus") or []).get("Backing Filesystem", "")
                            if backing and backing != "xfs":
                                logger.warning(
                                    f"context {ctx_name} storage_limit={storage_limit} but backing "
                                    f"filesystem is {backing} (needs xfs+pquota) - creates WILL fail"
                                )
                        except Exception as e:
                            logger.debug(f"context {ctx_name} backing-fs check failed: {e}")
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
                new_pub_hostnames[ctx.context_name] = public_hostname
                logger.info(f"connected to context '{ctx.context_name}' at {endpoint}")
            else:
                logger.error(f"could not connect to context '{ctx.context_name}': {err}")

        with self._lock:
            self._context_configs = new_configs
            self._pub_hostnames = new_pub_hostnames
            self._config_generation += 1

        create_limit = effective_profile["max_concurrent_creates"]
        if type(create_limit) is not int:
            raise ValueError("max_concurrent_creates must be an integer")
        self._init_semaphores(create_limit)

    def get_pub_hostname(self, context_name: str) -> str | None:
        with self._lock:
            return self._pub_hostnames.get(context_name)

    def get_check_hostname(self, context_name: str) -> str | None:
        return self.get_connection_hostnames(context_name)[1]

    def get_connection_hostnames(self, context_name: str) -> tuple[str | None, str | None]:
        """Snapshot the user-facing and readiness addresses atomically."""
        with self._lock:
            configured = self._pub_hostnames.get(context_name)
            endpoint = self._context_configs.get(context_name, "")
        # A local Docker daemon publishes ports on the host. CTFd itself runs
        # in a container, so readiness/proxy traffic must use the bridge
        # gateway even when users need a different public DNS name for SSH.
        if endpoint.startswith("unix://"):
            return configured or None, _get_host_gateway()
        # Remote runners expose their published ports at the configured runner
        # address; a local bridge-gateway fallback is not meaningful for them.
        if configured:
            return configured, configured
        return None, None

    def get_connected_contexts(self) -> list[str]:
        with self._lock:
            return list(self._context_configs)

    def ping(self, context_name: str) -> bool:
        # use a fresh ephemeral client. cached clients share paramiko transports
        # that wedge on dead-but-unreaped TCP sockets after idle periods, blocking
        # the 30s health_check past its interval for the full kernel retransmit cycle
        url = self._context_configs.get(context_name)
        if not url:
            return False
        if ping_endpoint(url, timeout=3):
            return True
        self._clear_client(context_name)
        return False

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
        labels: dict[str, str] | None = None,
    ) -> ContainerResult:
        from .models import get_all_settings

        effective_profile = get_all_settings()
        pids_limit = effective_profile["pids_limit"]
        cap_drop = [c.strip() for c in str(effective_profile["cap_drop"]).split(",") if c.strip()]
        cap_add = [c.strip() for c in str(effective_profile["cap_add"]).split(",") if c.strip()]

        # settings-gated hardening kwargs, validated here so a bad value fails
        # loudly before any docker call. "" / 0 omits the kwarg entirely, which
        # is load-bearing on the ext4 dev box (storage_opt is xfs-only)
        extra_kwargs: dict = {}

        storage_limit = str(effective_profile["storage_limit"] or "").strip()
        if storage_limit:
            parse_size(storage_limit)
            extra_kwargs["storage_opt"] = {"size": storage_limit}

        log_max_size = str(effective_profile["log_max_size"] or "").strip()
        if log_max_size:
            # max-file must be a string: daemon log-opts are map[string]string
            extra_kwargs["log_config"] = {
                "type": "json-file",
                "config": {"max-size": log_max_size, "max-file": str(int(effective_profile["log_max_file"] or 3))},
            }

        mem_reservation_raw = str(effective_profile["memory_reservation"] or "").strip()
        if mem_reservation_raw not in ("", "0"):
            mem_reservation = parse_size(mem_reservation_raw)
            if memory is not None and mem_reservation > memory:
                raise ValueError(f"memory_reservation {mem_reservation} exceeds memory limit {memory}")
            extra_kwargs["mem_reservation"] = mem_reservation

        if memory is not None:
            # swap headroom above the RAM limit. a desktop's memory drifts up
            # over a long session (browser tabs, RE tools); with zero swap a
            # transient spike past mem_limit OOM-kills a live app (firefox).
            # default gives a swap cushion equal to the RAM limit so spikes
            # spill to swap instead of dying. "0" = strict no-swap isolation,
            # "-1" = unlimited, "<size>" = explicit swap amount
            swap_raw = str(effective_profile["swap_limit"] or "").strip()
            if swap_raw == "-1":
                extra_kwargs["memswap_limit"] = -1
            elif swap_raw == "0":
                extra_kwargs["memswap_limit"] = memory
            else:
                swap_bytes = parse_size(swap_raw) if swap_raw else memory
                extra_kwargs["memswap_limit"] = memory + swap_bytes

        oom_score_adj = max(0, min(1000, int(effective_profile["oom_score_adj"] or 0)))
        if oom_score_adj:
            extra_kwargs["oom_score_adj"] = oom_score_adj

        nofile_soft = int(effective_profile["nofile_soft"] or 0)
        nofile_hard = int(effective_profile["nofile_hard"] or 0)
        if nofile_soft > 0:
            if nofile_hard < nofile_soft:
                raise ValueError(f"nofile_hard {nofile_hard} < nofile_soft {nofile_soft}")
            extra_kwargs["ulimits"] = [docker.types.Ulimit(name="nofile", soft=nofile_soft, hard=nofile_hard)]

        cgroup_parent = str(effective_profile["cgroup_parent"] or "").strip()
        if cgroup_parent:
            if not cgroup_parent.endswith(".slice"):
                raise ValueError(f"cgroup_parent must end in .slice on systemd-cgroup hosts, got {cgroup_parent!r}")
            extra_kwargs["cgroup_parent"] = cgroup_parent

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
                        labels=labels or {},
                        **extra_kwargs,
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

    def remove_paused_managed_orphan(
        self,
        context_name: str,
        container_id: str,
        container_name: str,
        expected_labels: dict[str, str],
    ) -> dict[str, str]:
        """Remove one exact paused plugin container and return its labels.

        Re-resolving by immutable ID and comparing the full ID/name prevents a
        stale dashboard request from deleting a replacement container.
        """

        def _do() -> dict[str, str]:
            client = self._get_client(context_name)
            try:
                container = client.containers.get(container_id)
                container.reload()
                actual_id = str(container.id or "")
                actual_name = str(container.name or "")
                labels = dict(((container.attrs or {}).get("Config") or {}).get("Labels") or {})
                if actual_id != container_id or actual_name != container_name:
                    raise ValueError("paused orphan identity changed; refresh and retry")
                expected_identity = {
                    SESSION_LABEL_MANAGED: expected_labels.get(SESSION_LABEL_MANAGED),
                    SESSION_LABEL_USER_ID: expected_labels.get(SESSION_LABEL_USER_ID),
                    SESSION_LABEL_UUID: expected_labels.get(SESSION_LABEL_UUID),
                }
                actual_identity = {key: labels.get(key) for key in expected_identity}
                if expected_identity[SESSION_LABEL_MANAGED] != "true":
                    raise ValueError("expected labels do not identify a managed container")
                if actual_identity != expected_identity:
                    raise ValueError("paused orphan ownership labels changed; refresh and retry")
                if normalize_container_state(container.status) != "paused":
                    raise ValueError("container is no longer paused; refusing removal")
                container.remove(force=True)
                return {str(key): str(value) for key, value in labels.items()}
            except docker.errors.NotFound as exc:
                raise ValueError("paused orphan no longer exists; refresh and retry") from exc
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                raise
            except ValueError:
                raise
            except Exception as exc:
                self._clear_client(context_name)
                raise HostsUnavailableException(f"transient client failure on {context_name}") from exc

        return self._call(context_name, _do)

    @staticmethod
    def _parse_created_ts(created_raw: str) -> float:
        if not created_raw:
            return 0.0
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
            return datetime.fromisoformat(iso).timestamp()
        except (ValueError, AttributeError):
            return 0.0

    def _list_by_prefix(self, client, name_prefix: str) -> list[dict[str, object]]:
        containers = client.containers.list(all=True, filters={"name": name_prefix})
        results: list[dict[str, object]] = []
        for c in containers:
            # Docker's `name` filter is a partial/regex match, not a prefix
            # guarantee. Enforce the caller's namespace locally before any
            # reconciliation code can treat a returned object as a session.
            if not str(c.name or "").startswith(name_prefix):
                continue
            created_raw = c.attrs.get("Created", "") if c.attrs else ""
            results.append(
                {
                    "id": str(c.id or ""),
                    "name": c.name or "",
                    "created_ts": self._parse_created_ts(created_raw),
                    "status": c.status or "",
                    "labels": dict(((c.attrs or {}).get("Config") or {}).get("Labels") or {}),
                }
            )
        return results

    def list_containers_by_prefix(self, context_name: str, name_prefix: str) -> list[dict[str, object]]:
        # Lenient listing swallows errors and returns [] so a flapping host
        # cannot break a read-only status loop. Destructive callers must use
        # the strict variant because an error looks identical to no containers.
        def _do() -> list[dict[str, object]]:
            try:
                client = self._get_client(context_name)
                return self._list_by_prefix(client, name_prefix)
            except Exception:
                self._clear_client(context_name)
                return []

        return self._call(context_name, _do)

    def list_session_containers_strict(self, context_name: str, name_prefix: str) -> list[dict[str, object]] | None:
        # strict listing: None on any error, so callers can distinguish "host
        # answered: nothing there" from "host unreachable". The reconcile
        # sweep only acts on a non-None result.
        def _do() -> list[dict[str, object]] | None:
            try:
                client = self._get_client(context_name)
                return self._list_by_prefix(client, name_prefix)
            except Exception:
                self._clear_client(context_name)
                return None

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

    def pause_container(self, context_name: str, container_name: str) -> None:
        def _do():
            client = self._get_client(context_name)
            try:
                container = client.containers.get(container_name)
                container.pause()
            except docker.errors.NotFound:
                logger.warning(f"container {container_name} not found for pause")
                raise
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                raise
            except Exception:
                self._clear_client(context_name)
                raise HostsUnavailableException(f"transient client failure on {context_name}")

        return self._call(context_name, _do)

    def unpause_container(self, context_name: str, container_name: str) -> None:
        def _do():
            client = self._get_client(context_name)
            try:
                container = client.containers.get(container_name)
                container.unpause()
            except docker.errors.NotFound:
                logger.warning(f"container {container_name} not found for unpause")
                raise
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                raise
            except Exception:
                self._clear_client(context_name)
                raise HostsUnavailableException(f"transient client failure on {context_name}")

        return self._call(context_name, _do)

    def get_host_memory(self, context_name: str) -> int | None:
        # MemTotal in bytes from docker info; None on any failure. used by the
        # orchestrator to auto-derive per-host session caps
        if context_name not in self._context_configs:
            return None

        def _do():
            try:
                client = self._get_client(context_name)
                return int(client.info().get("MemTotal") or 0) or None
            except Exception:
                self._clear_client(context_name)
                return None

        return self._call(context_name, _do)

    def inspect_container_state(self, context_name: str, container_id: str) -> ContainerState:
        """Return a strict state without conflating transport failure with absence.

        Destructive callers must treat ``paused`` and ``unknown`` as holds.
        ``not_found`` is returned only for Docker's explicit NotFound response.
        """

        def _do():
            try:
                client = self._get_client(context_name)
                container = client.containers.get(container_id)
                return normalize_container_state(container.status)
            except docker.errors.NotFound:
                return "not_found"
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException, EOFError, OSError):
                self._clear_client(context_name)
                return "unknown"
            except Exception:
                self._clear_client(context_name)
                return "unknown"

        state = self._call(context_name, _do)
        return state if state in ("running", "paused", "created", "exited", "not_found") else "unknown"

    def is_container_running(self, context_name: str, container_id: str) -> bool:
        state = self.inspect_container_state(context_name, container_id)
        if state == "unknown":
            raise HostsUnavailableException(f"container state unavailable on {context_name}")
        # Paused is alive: it is an evidence hold, not a dead container to reap.
        return state in ("running", "paused")
