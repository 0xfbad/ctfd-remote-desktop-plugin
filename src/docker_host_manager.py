from __future__ import annotations

import os
import io
import json
import math
import tarfile
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
from .messages import HOST_UNREACHABLE, SERVER_BUSY
from .utils import normalize_public_hostname

logger = logging.getLogger(__name__)

SESSION_LABEL_MANAGED = "org.ctfd.remote-desktop.managed"
SESSION_LABEL_USER_ID = "org.ctfd.remote-desktop.user-id"
SESSION_LABEL_UUID = "org.ctfd.remote-desktop.session-uuid"
IMAGE_CONTRACT_LABEL = "edu.ucsc.ctfd-remote-desktop.contract"
IMAGE_CONTRACT_VERSION = "3"

LOCAL_CONTEXT_NAME = "local"
LOCAL_SOCKET_PATH = "/var/run/docker.sock"
DOCKER_CONFIG_DIR = os.environ.get("DOCKER_CONFIG", os.path.expanduser("~/.docker"))

DEFAULT_CLIENT_TIMEOUT = 10  # seconds of docker sdk http read time, the ssh connect phase is bounded separately
THREADPOOL_SIZE = 4  # caps concurrent in flight blocking calls per host
ContextMeta = dict[str, str | dict[str, dict[str, str]]]
DiscoveredContext = dict[str, str]
ContainerResult = dict[str, str | dict[str, int]]
ImageInfo = dict[str, int | str]
ContainerState = Literal["running", "paused", "created", "exited", "not_found", "unknown"]
ClientKey = tuple[str, int]

_SSH_ADAPTER_PATCH_LOCK = threading.RLock()
_SSH_CONNECT_TIMEOUT = threading.local()
_SSH_ADAPTER_PATCHED = False


def _apply_ssh_connect_timeouts(params: dict[str, object], timeout: int | float) -> None:
    """the docker sdk timeout is not forwarded to SSHClient.connect
    without these a blackholed runner occupies a context worker forever
    """
    bounded = max(1.0, float(timeout))
    params.update(timeout=bounded, banner_timeout=bounded, auth_timeout=bounded)


def _install_bounded_ssh_adapter() -> None:
    """process wide timeout fix for docker-py 7.x"""
    global _SSH_ADAPTER_PATCHED
    if _SSH_ADAPTER_PATCHED:
        return
    with _SSH_ADAPTER_PATCH_LOCK:
        if _SSH_ADAPTER_PATCHED:
            return
        try:
            from docker.api import client as api_client
        except (ImportError, AttributeError):
            return  # unit tests stub docker without this module, the pinned dependency always has it
        original_adapter = api_client.SSHHTTPAdapter
        if getattr(original_adapter, "_ctfd_bounded_connect", False):
            _SSH_ADAPTER_PATCHED = True
            return

        # the base is resolved at runtime so mypy cannot prove it is a class
        class BoundedSSHHTTPAdapter(original_adapter):  # type: ignore[misc, valid-type]
            _ctfd_bounded_connect = True

            def _create_paramiko_client(self, base_url):
                super()._create_paramiko_client(base_url)
                timeout = getattr(_SSH_CONNECT_TIMEOUT, "value", DEFAULT_CLIENT_TIMEOUT)
                _apply_ssh_connect_timeouts(self.ssh_params, timeout)

        api_client.SSHHTTPAdapter = BoundedSSHHTTPAdapter
        _SSH_ADAPTER_PATCHED = True


def _new_docker_client(endpoint: str, timeout: int = DEFAULT_CLIENT_TIMEOUT):
    if not endpoint.startswith("ssh://"):
        return docker.DockerClient(base_url=endpoint, timeout=timeout)
    _install_bounded_ssh_adapter()
    _SSH_CONNECT_TIMEOUT.value = timeout
    try:
        return docker.DockerClient(base_url=endpoint, timeout=timeout)
    finally:
        try:
            del _SSH_CONNECT_TIMEOUT.value
        except AttributeError:
            pass


def _image_contract(image: object) -> str | None:
    attrs = getattr(image, "attrs", None)
    if not isinstance(attrs, Mapping):
        return None
    config = attrs.get("Config")
    if not isinstance(config, Mapping):
        return None
    labels = config.get("Labels")
    if not isinstance(labels, Mapping):
        return None
    contract = labels.get(IMAGE_CONTRACT_LABEL)
    return str(contract) if contract is not None else None


def normalize_container_state(value: object) -> ContainerState:
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
    """allow only the one local socket or a well formed ssh transport
    context metadata is operator controlled but must not widen the control plane to unauthenticated tcp daemons
    """
    if not candidate or candidate != candidate.strip() or any(c.isspace() for c in candidate):
        return None
    if candidate.startswith("unix://"):
        if context_name == LOCAL_CONTEXT_NAME and candidate == f"unix://{LOCAL_SOCKET_PATH}":
            return candidate
        return None
    try:
        parsed = urlsplit(candidate)
        parsed_port = parsed.port  # reading the port is what rejects a malformed or out of range value
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
        # a manually configured hostname may already carry a scheme, never build ssh://root@ssh://host
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

    local_missing = not any(d["name"] == LOCAL_CONTEXT_NAME for d in discovered)
    if local_missing and os.path.exists(LOCAL_SOCKET_PATH):
        discovered.append({"name": LOCAL_CONTEXT_NAME, "endpoint": f"unix://{LOCAL_SOCKET_PATH}"})

    return discovered


def _get_host_gateway() -> str:
    try:
        import struct

        with open("/proc/net/route") as f:
            for line in f:
                parts = line.strip().split()
                if parts[1] == "00000000":  # destination 0 is the default route
                    gw = struct.pack("<I", int(parts[2], 16))
                    return ".".join(str(b) for b in gw)
    except Exception:
        pass
    return "localhost"


def ping_endpoint(endpoint: str, timeout: int = 3) -> bool:
    client = None
    try:
        client = _new_docker_client(endpoint, timeout=timeout)
        client.ping()
        return True
    except Exception:
        return False
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


_DEADLINE_STATE_DIR = "/var/lib/remote-desktop"
_DEADLINE_FILENAME = "max-lifetime-deadline"


def _read_deadline_state(container) -> tuple[int, float]:
    stream, _stat = container.get_archive(f"{_DEADLINE_STATE_DIR}/{_DEADLINE_FILENAME}")
    archive_buffer = bytearray()
    for chunk in stream:
        archive_buffer.extend(chunk)
        if len(archive_buffer) > 1024 * 1024:
            raise ValueError("maximum-lifetime deadline archive was oversized")
    archive = bytes(archive_buffer)
    if not archive:
        raise ValueError("maximum-lifetime deadline archive was empty")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
        members = [
            member
            for member in tar.getmembers()
            if member.isfile() and os.path.basename(member.name.rstrip("/")) == _DEADLINE_FILENAME
        ]
        if len(members) != 1:
            raise ValueError("maximum-lifetime deadline file was missing or ambiguous")
        member = members[0]
        if member.uid != 0 or member.gid != 0 or member.mode & 0o777 != 0o600:
            raise ValueError("maximum-lifetime deadline file has unsafe metadata")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise ValueError("maximum-lifetime deadline file was unreadable")
        raw = extracted.read(32)
        if extracted.read(1):
            raise ValueError("maximum-lifetime deadline file was oversized")
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("maximum-lifetime deadline was not ASCII") from exc
    if not value.isdigit() or value.startswith("0") or len(value) > 10:
        raise ValueError("maximum-lifetime deadline was invalid")
    return int(value), float(member.mtime or 0)


def _build_deadline_archive(deadline: int, mtime: int) -> bytes:
    payload = f"{deadline}\n".encode("ascii")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        member = tarfile.TarInfo(_DEADLINE_FILENAME)
        member.size = len(payload)
        member.mode = 0o600
        member.uid = 0
        member.gid = 0
        member.mtime = mtime
        tar.addfile(member, io.BytesIO(payload))
    return buffer.getvalue()


class DockerHostManager:
    def __init__(self) -> None:
        self._context_configs: dict[str, str] = {}
        self._pub_hostnames: dict[str, str] = {}
        # reachability is separate from the endpoint catalog so a startup probe failure does not drop the host
        self._connected_contexts: set[str] = set()

        # keyed per thread because paramiko channels bind gevent events to the hub of the creating thread
        self._clients: dict[ClientKey, docker.DockerClient] = {}
        self._client_generations: dict[ClientKey, int] = {}

        # an epoch bump lets each worker replace its own client instead of closing a peer paramiko transport
        self._client_epochs: dict[ClientKey, int] = {}
        self._context_client_epochs: dict[str, int] = {}
        self._client_threads: dict[ClientKey, threading.Thread] = {}

        self._config_generation: int = 0
        self._lock: threading.RLock = threading.RLock()  # reentrant so wrapped ops reenter locked helpers
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._semaphore_limits: dict[str, int] = {}

        # per context pool isolates blocking paramiko calls so one hung host cannot starve the others
        self._threadpools: dict[str, gevent.threadpool.ThreadPool] = {}

    def _get_threadpool(self, context_name: str) -> gevent.threadpool.ThreadPool:
        with self._lock:
            pool = self._threadpools.get(context_name)
            if pool is None:
                pool = gevent.threadpool.ThreadPool(maxsize=THREADPOOL_SIZE)
                self._threadpools[context_name] = pool
            return pool

    def _call(self, context_name: str, fn, *args, **kwargs):
        # cli paths have no gevent hub and pool.apply hangs in futex there, so run inline
        if not gevent.monkey.is_module_patched("threading"):
            return fn(*args, **kwargs)
        pool = self._get_threadpool(context_name)
        return pool.apply(fn, args=args, kwds=kwargs)

    def _pop_entry_locked(self, key: ClientKey) -> docker.DockerClient | None:
        client = self._clients.pop(key, None)
        self._client_generations.pop(key, None)
        self._client_epochs.pop(key, None)
        self._client_threads.pop(key, None)
        return client

    def _sweep_dead_entries_locked(self, to_close: list[docker.DockerClient]) -> None:
        # idents are reused after a worker exits, so the owner object decides who inherits a transport
        dead_keys = [
            cached_key
            for cached_key in self._clients
            if (owner := self._client_threads.get(cached_key)) is None or not owner.is_alive()
        ]
        for dead_key in dead_keys:
            dead_client = self._pop_entry_locked(dead_key)
            if dead_client is not None:
                to_close.append(dead_client)

    def _entry_is_stale_locked(self, key: ClientKey) -> bool:
        return (
            key[0] not in self._context_configs
            or self._client_generations.get(key) != self._config_generation
            or self._client_epochs.get(key) != self._context_client_epochs.get(key[0], 0)
        )

    def _sweep_owned_stale_entries_locked(
        self, current_thread: threading.Thread, to_close: list[docker.DockerClient]
    ) -> None:
        # sweep every stale key this thread owns, otherwise each retired context leaks one ssh transport
        owned_stale_keys = [
            cached_key
            for cached_key in self._clients
            if self._client_threads.get(cached_key) is current_thread and self._entry_is_stale_locked(cached_key)
        ]
        for stale_key in owned_stale_keys:
            stale_client = self._pop_entry_locked(stale_key)
            if stale_client is not None:
                to_close.append(stale_client)

    def _take_current_client_locked(
        self, key: ClientKey, current_thread: threading.Thread, to_close: list[docker.DockerClient]
    ) -> docker.DockerClient | None:
        client = self._clients.get(key)
        if client is None:
            return None
        if self._client_threads.get(key) is not current_thread or self._entry_is_stale_locked(key):
            # roll only this worker entry, closing another thread transport can abort its create
            rolled_client = self._pop_entry_locked(key)
            if rolled_client is not None:
                to_close.append(rolled_client)
            return None
        return client

    def _get_client(self, context_name: str) -> docker.DockerClient:
        tid = threading.get_ident()
        current_thread = threading.current_thread()
        to_close: list[docker.DockerClient] = []
        missing_context = False
        client_error: Exception | None = None
        with self._lock:
            key = (context_name, tid)
            self._sweep_dead_entries_locked(to_close)
            self._sweep_owned_stale_entries_locked(current_thread, to_close)
            client = self._take_current_client_locked(key, current_thread, to_close)

            if client is None:
                url = self._context_configs.get(context_name)
                if url:
                    try:
                        client = _new_docker_client(url, timeout=DEFAULT_CLIENT_TIMEOUT)
                    except Exception as exc:
                        client_error = exc  # defer the raise so entries retired above still get closed
                    else:
                        self._clients[key] = client
                        self._client_generations[key] = self._config_generation
                        self._client_epochs[key] = self._context_client_epochs.get(context_name, 0)
                        self._client_threads[key] = current_thread
                else:
                    missing_context = True

        # close outside the lock, paramiko teardown can block on ssh for seconds
        for old in to_close:
            try:
                old.close()
            except Exception:
                pass
        if client_error is not None:
            raise client_error
        if missing_context:
            # typed so callers map a stale row miss to 503 instead of 500
            raise HostsUnavailableException(f"no client for context '{context_name}'")
        assert client is not None
        return client

    def _transient_host_failure(self, context_name: str) -> HostsUnavailableException:
        logger.warning("transient client failure on %s", context_name, exc_info=True)
        self._clear_client(context_name)
        return HostsUnavailableException(HOST_UNREACHABLE)

    def _clear_client(self, context_name: str) -> None:
        # mark peers stale by epoch instead of closing a transport another worker may still be using
        key = (context_name, threading.get_ident())
        with self._lock:
            self._context_client_epochs[context_name] = self._context_client_epochs.get(context_name, 0) + 1
            old = self._pop_entry_locked(key)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def _init_semaphores(self, limit: int) -> None:
        """not distributed, every ctfd process enforces this create limit on its own
        matching semaphore objects survive a reload so in flight acquisitions stay in one generation
        """
        with self._lock:
            old_semaphores = self._semaphores
            old_limits = self._semaphore_limits
            new_semaphores: dict[str, threading.BoundedSemaphore] = {}
            for ctx_name in self._context_configs:
                old = old_semaphores.get(ctx_name)
                if old is not None and old_limits.get(ctx_name) == limit:
                    new_semaphores[ctx_name] = old
                else:
                    new_semaphores[ctx_name] = threading.BoundedSemaphore(limit)

            self._semaphores = new_semaphores
            self._semaphore_limits = dict.fromkeys(new_semaphores, limit)

    def acquire_semaphore(self, context_name: str, timeout: int = 10) -> threading.BoundedSemaphore | None:
        """returns the exact object the caller must release"""
        with self._lock:
            sem = self._semaphores.get(context_name)
        if sem is None:
            return None

        acquired = sem.acquire(blocking=True, timeout=timeout)
        if not acquired:
            raise HostsUnavailableException(SERVER_BUSY)
        return sem

    def release_semaphore(self, semaphore: threading.BoundedSemaphore | str | None) -> None:
        """a context name is still accepted for compatibility
        new callers pass the object from acquire_semaphore, name lookup is not generation safe across a reload
        """
        if isinstance(semaphore, str):
            with self._lock:
                sem = self._semaphores.get(semaphore)
        else:
            sem = semaphore
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                pass

    def load_contexts(self, contexts: list[DesktopDockerContextModel]) -> None:
        from .models import get_all_settings

        new_configs: dict[str, str] = {}
        new_pub_hostnames: dict[str, str] = {}
        new_connected_contexts: set[str] = set()

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

            # keep the resolved endpoint even if the probe below fails, the health check needs a url to recover it
            new_configs[ctx.context_name] = endpoint
            new_pub_hostnames[ctx.context_name] = public_hostname

            def _check(endpoint=endpoint, ctx_name=ctx.context_name):
                client = None
                try:
                    client = _new_docker_client(endpoint, timeout=DEFAULT_CLIENT_TIMEOUT)
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
                        # storage_opt on a non xfs data root makes the daemon refuse every create, warn at load
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
                new_connected_contexts.add(ctx.context_name)
                logger.info(f"connected to context '{ctx.context_name}' at {endpoint}")
            else:
                logger.error(f"could not connect to context '{ctx.context_name}': {err}")

        create_limit = effective_profile["max_concurrent_creates"]
        if type(create_limit) is not int:
            raise ValueError("max_concurrent_creates must be an integer")

        with self._lock:
            self._context_configs = new_configs
            self._pub_hostnames = new_pub_hostnames
            self._connected_contexts = new_connected_contexts
            self._config_generation += 1
            self._init_semaphores(create_limit)

    def get_pub_hostname(self, context_name: str) -> str | None:
        with self._lock:
            return self._pub_hostnames.get(context_name)

    def get_check_hostname(self, context_name: str) -> str | None:
        return self.get_connection_hostnames(context_name)[1]

    def get_connection_hostnames(self, context_name: str) -> tuple[str | None, str | None]:
        """returns the user facing address and the readiness address"""
        with self._lock:
            configured = self._pub_hostnames.get(context_name)
            endpoint = self._context_configs.get(context_name, "")

        # ctfd runs in a container so readiness traffic to a local daemon has to go through the bridge gateway
        if endpoint.startswith("unix://"):
            return configured or None, _get_host_gateway()

        if configured:
            return configured, configured
        return None, None

    def get_connected_contexts(self) -> list[str]:
        with self._lock:
            return [name for name in self._context_configs if name in self._connected_contexts]

    def ping(self, context_name: str) -> bool:
        with self._lock:
            url = self._context_configs.get(context_name)
        if not url:
            return False

        # fresh client, a cached paramiko transport wedges on dead tcp sockets past the health check interval
        reachable = ping_endpoint(url, timeout=3)

        # a probe that finishes after a reload must not overwrite the new catalog reachability
        with self._lock:
            endpoint_is_current = self._context_configs.get(context_name) == url
            if endpoint_is_current:
                if reachable:
                    self._connected_contexts.add(context_name)
                else:
                    self._connected_contexts.discard(context_name)
        if not endpoint_is_current:
            return False
        if reachable:
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

        # an empty or zero setting omits the kwarg entirely, storage_opt is xfs only and breaks the ext4 dev box
        extra_kwargs: dict = {}

        storage_limit = str(effective_profile["storage_limit"] or "").strip()
        if storage_limit:
            parse_size(storage_limit)
            extra_kwargs["storage_opt"] = {"size": storage_limit}

        log_max_size = str(effective_profile["log_max_size"] or "").strip()
        if log_max_size:
            # max-file must be a string, the daemon rejects integer log opts
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
            # the default cushion equals the memory limit so a spike spills to swap instead of oom killing an app
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
            try:
                resolved_image = client.images.get(image)
            except docker.errors.ImageNotFound as exc:
                raise docker.errors.DockerException(
                    f"desktop image {image!r} was not found on context {context_name!r}"
                ) from exc
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                raise

            contract = _image_contract(resolved_image)
            if contract != IMAGE_CONTRACT_VERSION:
                raise docker.errors.DockerException(
                    f"desktop image {image!r} on context {context_name!r} has "
                    f"{IMAGE_CONTRACT_LABEL}={contract!r}; expected {IMAGE_CONTRACT_VERSION!r}"
                )
            resolved_image_id = getattr(resolved_image, "id", None)
            if not isinstance(resolved_image_id, str) or not resolved_image_id:
                raise docker.errors.DockerException(
                    f"desktop image {image!r} on context {context_name!r} did not resolve to an immutable image ID"
                )

            last_err: Exception | None = None
            container = None
            for _ in range(50):
                port_bindings = {p: _sysrand.randint(40000, 59999) for p in ports}
                try:
                    container = client.containers.run(
                        resolved_image_id,
                        name=name,
                        hostname=hostname or name,
                        detach=True,
                        auto_remove=True,
                        init=True,
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

            port_map: dict[str, int] | None = None
            try:
                for attempt in range(5):
                    container.reload()
                    network_ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})

                    current_map: dict[str, int] = {}
                    for port in ports:
                        bindings = network_ports.get(port)
                        if bindings and len(bindings) > 0:
                            current_map[port] = int(bindings[0]["HostPort"])

                    if len(current_map) == len(ports) and current_map:
                        port_map = current_map
                        break

                    if attempt < 4:
                        time.sleep(0.3)
            except Exception:
                # a reload failure usually means the transport is dead, drop the client and let cleanup reconnect
                self._clear_client(context_name)
                raise

            if port_map is None:
                raise docker.errors.DockerException(
                    f"could not get all port mappings for {name}; expected {sorted(ports)!r}"
                )

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
                raise self._transient_host_failure(context_name)

        return self._call(context_name, _do)

    def force_remove_container(self, context_name: str, container_name: str) -> None:
        # stop does nothing to a created container so auto_remove never fires, forced removal covers every state in one call
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
                raise self._transient_host_failure(context_name)

        return self._call(context_name, _do)

    def remove_paused_managed_orphan(
        self,
        context_name: str,
        container_id: str,
        container_name: str,
        expected_labels: dict[str, str],
    ) -> dict[str, str]:
        """compares immutable id and name so a stale dashboard request cannot delete a replacement container"""

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
                raise self._transient_host_failure(context_name) from exc

        return self._call(context_name, _do)

    @staticmethod
    def _parse_created_ts(created_raw: str) -> float:
        if not created_raw:
            return 0.0
        try:
            from datetime import datetime

            iso = created_raw.replace("Z", "+00:00")
            # python parses at most microseconds, docker emits 9 digit fractional seconds
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
            # the docker name filter is a regex match not a prefix, so enforce the caller namespace locally
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
        # lenient, an error returns empty so a flapping host cannot break a read only status loop
        def _do() -> list[dict[str, object]]:
            try:
                client = self._get_client(context_name)
                return self._list_by_prefix(client, name_prefix)
            except Exception:
                self._clear_client(context_name)
                return []

        return self._call(context_name, _do)

    def list_session_containers_strict(self, context_name: str, name_prefix: str) -> list[dict[str, object]] | None:
        # strict, an error returns none so destructive callers can tell an empty host from an unreachable one
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
                img = client.images.get(image)
                contract = _image_contract(img)
                if contract != IMAGE_CONTRACT_VERSION:
                    logger.warning(
                        "image %s on context %s has remote-desktop contract %r; expected %r",
                        image,
                        context_name,
                        contract,
                        IMAGE_CONTRACT_VERSION,
                    )
                    return False
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

    def check_storage_limit_compatibility(self, context_name: str, storage_limit: str) -> bool:
        """a writable layer quota needs the overlay2 driver on an xfs backing filesystem
        unknown daemon metadata counts as ineligible, a create time mismatch strands a durable reservation
        """
        if not storage_limit.strip():
            return True

        def _do() -> bool:
            try:
                client = self._get_client(context_name)
                info = client.info() or {}
                driver = str(info.get("Driver") or "").strip().lower()
                status = {
                    str(key).strip().lower(): str(value).strip().lower()
                    for key, value in (info.get("DriverStatus") or [])
                }
                backing = status.get("backing filesystem", "")
                supports_dtype = status.get("supports d_type", "")
                compatible = driver == "overlay2" and backing == "xfs" and supports_dtype in ("true", "1", "yes")
                if not compatible:
                    logger.warning(
                        "context %s is ineligible for storage_limit=%s (driver=%r backing=%r supports_d_type=%r)",
                        context_name,
                        storage_limit,
                        driver or "unknown",
                        backing or "unknown",
                        supports_dtype or "unknown",
                    )
                return compatible
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException, EOFError, OSError):
                self._clear_client(context_name)
                return False
            except Exception:
                self._clear_client(context_name)
                return False

        return bool(self._call(context_name, _do))

    def get_image_info(self, context_name: str, image: str) -> ImageInfo | None:
        def _do():
            try:
                client = self._get_client(context_name)
                img = client.images.get(image)
                attrs = img.attrs or {}
                size_mb = round((attrs.get("Size") or 0) / 1024 / 1024)
                raw = attrs.get("Created", "")[:19]
                # reproducible build images report 1980-01-01, so fall back to the last tag time
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
                contract = _image_contract(img)
                contract_status = "compatible" if contract == IMAGE_CONTRACT_VERSION else "incompatible"
                return {
                    "size_mb": size_mb,
                    "created": created,
                    "id": short_id,
                    "contract": str(contract) if contract is not None else "missing",
                    "contract_status": contract_status,
                }
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
                raise self._transient_host_failure(context_name)

        return self._call(context_name, _do)

    def extend_paused_lifetime_deadline(
        self,
        context_name: str,
        container_name: str,
        paused_at: float,
        *,
        minimum_remaining: int = 60,
    ) -> int:
        """docker archive io still works while the cgroup is frozen so the watchdog cannot observe the replacement
        the file mtime records the last credited instant so a retry adds only the new part of the hold
        """

        def _do() -> int:
            client = self._get_client(context_name)
            try:
                container = client.containers.get(container_name)
                container.reload()
                if normalize_container_state(container.status) != "paused":
                    raise ValueError("container is no longer paused; refusing deadline update")

                now = time.time()
                if not math.isfinite(paused_at) or paused_at <= 0 or paused_at > now + 1:
                    raise ValueError("paused timestamp is invalid")
                current_deadline, credited_mtime = _read_deadline_state(container)
                if credited_mtime > now + 1:
                    raise ValueError("maximum-lifetime deadline mtime is in the future")

                credit_from = max(paused_at, credited_mtime)
                credit_seconds = max(0, math.ceil(now - credit_from))
                new_deadline = current_deadline + credit_seconds
                if new_deadline - now < minimum_remaining:
                    raise ValueError("maximum-lifetime deadline is too close to resume safely")
                if new_deadline > 9_999_999_999:
                    raise ValueError("maximum-lifetime deadline extension overflowed")

                if credit_seconds:
                    archive = _build_deadline_archive(new_deadline, int(now))
                    if container.put_archive(_DEADLINE_STATE_DIR, archive) is False:
                        raise ValueError("Docker rejected maximum-lifetime deadline update")

                verified_deadline, verified_mtime = _read_deadline_state(container)
                if verified_deadline != new_deadline:
                    raise ValueError("maximum-lifetime deadline readback did not match")
                if credit_seconds and verified_mtime < int(now) - 1:
                    raise ValueError("maximum-lifetime deadline metadata was not updated")
                return verified_deadline
            except docker.errors.NotFound:
                raise
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                raise
            except ValueError:
                raise
            except Exception as exc:
                raise self._transient_host_failure(context_name) from exc

        return int(self._call(context_name, _do))

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
                raise self._transient_host_failure(context_name)

        return self._call(context_name, _do)

    def get_host_memory(self, context_name: str) -> int | None:
        # total host memory in bytes, the orchestrator derives per host session caps from it
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
        """destructive callers must treat paused and unknown as holds
        not_found is returned only when docker explicitly reports the container missing
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
        # paused is alive, it is an evidence hold rather than a dead container to reap
        return state in ("running", "paused")
