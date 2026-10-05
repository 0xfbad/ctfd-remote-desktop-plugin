from __future__ import annotations

import time
import logging
from collections import Counter
from dataclasses import dataclass
from threading import Event, Lock

from .docker_host_manager import (
    IMAGE_CONTRACT_VERSION,
    DockerHostManager,
    ImageInfo,
    SESSION_LABEL_MANAGED,
    parse_size,
)
from .exceptions import HostsUnavailableException, HostsAtCapacityException
from .messages import AT_CAPACITY, NO_HEALTHY_HOSTS
from .event_logger import event_logger

logger = logging.getLogger(__name__)

HostStatus = dict[str, str | int | bool | None]
ContextFence = tuple[int, str | None, str, int | None, int | None, int]


@dataclass(frozen=True)
class ReservationClaim:
    user_id: int
    session_uuid: str
    worker_lease_uuid: str
    container_name: str


class ReservationOwnershipError(RuntimeError):
    pass


DERIVED_CAP_FALLBACK = 0
CAPACITY_EVENT_MIN_INTERVAL = 300


def derive_max_containers(mem_total: int | None, mem_limit: int, fraction: float) -> int:
    if not mem_total or mem_limit <= 0:
        return DERIVED_CAP_FALLBACK
    return max(0, int(fraction * mem_total // mem_limit))


def rank_candidates(healthy: list[str], snapshot: dict[str, tuple[int, int, int]]) -> list[str]:
    out = []
    for name in healthy:
        if name not in snapshot:
            continue

        active, cap, weight = snapshot[name]
        if active >= cap:  # lowered caps may leave existing sessions above capacity
            continue

        out.append((-(weight / (active + 1)), name))
    return [n for _, n in sorted(out)]


def plan_count_audit(
    counters: dict[str, int],
    rows: dict[str, int],
    *,
    suppress_down_heal: bool = False,
) -> tuple[dict[str, int], list[tuple[str, int, int, str]]]:
    updates: dict[str, int] = {}
    events: list[tuple[str, int, int, str]] = []
    for name, counter in counters.items():
        observed_count = rows.get(name, 0)
        if counter < observed_count:
            updates[name] = observed_count
            events.append((name, counter, observed_count, "healed_up"))
        elif counter > observed_count and not suppress_down_heal:
            updates[name] = observed_count
            events.append((name, counter, observed_count, "healed_down"))
    return updates, events


class Orchestrator:
    def __init__(self, host_manager: DockerHostManager) -> None:
        self.host_manager = host_manager
        self.health: dict[str, bool] = {}
        self.auto_caps: dict[str, int] = {}
        self._cap_stale: set[str] = set()
        self._capacity_refusals: int = 0
        self._last_capacity_event_ts: float = 0.0
        self.lock = Lock()
        self._load_lock = Lock()  # prevent an older reload from overwriting a newer snapshot
        self.context_fences: dict[str, ContextFence] = {}
        self._initial_probe = Event()
        self._initial_probe.set()

    def _derive_auto_cap(self, context_name: str, connected: bool) -> tuple[int, bool]:
        from .models import get_setting

        mem_total = self.host_manager.get_host_memory(context_name) if connected else None
        cap = derive_max_containers(
            mem_total,
            parse_size(str(get_setting("memory_limit"))),
            float(get_setting("capacity_ram_fraction") or 0.7),
        )
        return cap, not mem_total

    def load_from_db(self, probe: bool = True) -> None:
        with self._load_lock:
            self._load_from_db_serialized(probe=probe)

    def _probe_context(
        self,
        ctx,
        is_connected: bool,
        docker_image: str,
        storage_limit: str,
    ) -> tuple[bool, int, bool, tuple[str, str, str, dict[str, str | int | ImageInfo | None]]]:
        name = ctx.context_name
        image_info = self.host_manager.get_image_info(name, docker_image) if is_connected else None
        image_compatible = bool(
            image_info
            and image_info.get("contract") == IMAGE_CONTRACT_VERSION
            and image_info.get("contract_status") == "compatible"
        )
        storage_compatible = bool(
            is_connected and self.host_manager.check_storage_limit_compatibility(name, storage_limit)
        )

        if ctx.max_containers is not None:
            auto_cap, stale = 0, False
            cap_value, cap_source = ctx.max_containers, "explicit"
        else:
            auto_cap, stale = self._derive_auto_cap(name, is_connected)
            cap_value, cap_source = auto_cap, "fallback" if stale else "auto"

        healthy = is_connected and image_compatible and storage_compatible and not stale

        if healthy:
            meta: dict[str, str | int | ImageInfo | None] = {
                "context_name": name,
                "max_containers": cap_value,
                "cap_source": cap_source,
            }
            if image_info:
                meta["image"] = image_info
            event = ("host_healthy", f"context {name} is healthy", "info", meta)
            return healthy, auto_cap, stale, event

        if not is_connected:
            reason = "connection failed"
        elif image_info is None:
            reason = "image not found"
        elif not storage_compatible:
            reason = "configured storage limit is unsupported"
        elif stale:
            reason = "host memory unavailable for capacity validation"
        else:
            reason = "incompatible image contract"
        meta = {
            "context_name": name,
            "reason": reason,
            "docker_image": docker_image,
        }
        if image_info is not None:
            meta["image"] = image_info
            meta["expected_contract"] = IMAGE_CONTRACT_VERSION
        event = (
            "host_unhealthy",
            f"context {name} marked unhealthy: {reason}",
            "warning",
            meta,
        )
        return healthy, auto_cap, stale, event

    def _load_from_db_serialized(self, probe: bool = True) -> None:
        from .models import DesktopDockerContextModel, get_setting

        settings_revision = int(
            str(get_setting("_settings_revision"))
        )  # revision must precede settings reads to fence concurrent updates
        contexts = DesktopDockerContextModel.query.filter_by(enabled=True).all()
        new_context_fences: dict[str, ContextFence] = {
            str(ctx.context_name): (
                int(ctx.id),
                str(ctx.hostname) if ctx.hostname is not None else None,
                str(ctx.pub_hostname),
                int(ctx.weight) if ctx.weight is not None else None,
                int(ctx.max_containers) if ctx.max_containers is not None else None,
                settings_revision,
            )
            for ctx in contexts
        }

        previous_endpoints = self.host_manager.get_endpoints()
        self.host_manager.load_contexts(contexts, probe=probe)
        current_endpoints = self.host_manager.get_endpoints()
        unchanged = {name for name, url in current_endpoints.items() if previous_endpoints.get(name) == url}

        new_health: dict[str, bool] = {}
        new_auto_caps: dict[str, int] = {}
        new_cap_stale: set[str] = set()
        events: list[tuple[str, str, str, dict[str, str | int | ImageInfo | None]]] = []
        if probe:
            connected = set(self.host_manager.get_connected_contexts())
            docker_image = str(get_setting("docker_image"))
            storage_limit = str(get_setting("storage_limit") or "").strip()
            for ctx in contexts:
                name = ctx.context_name
                healthy, auto_cap, stale, event = self._probe_context(  # docker calls must stay outside the lock
                    ctx, name in connected, docker_image, storage_limit
                )
                new_health[name] = healthy
                if ctx.max_containers is None:
                    new_auto_caps[name] = auto_cap
                if stale:
                    new_cap_stale.add(name)
                events.append(event)
        else:
            with self.lock:
                carried_health = dict(self.health)
                carried_caps = dict(self.auto_caps)
                carried_stale = set(self._cap_stale)
                carried_fences = dict(self.context_fences)
            for ctx in contexts:
                name = str(ctx.context_name)
                previous_fence = carried_fences.get(name)
                auto_cap_is_current = (
                    previous_fence is not None
                    and previous_fence[-2] is None
                    and name in carried_caps
                    and name not in carried_stale
                )
                settings_unchanged = previous_fence is not None and previous_fence[-1] == settings_revision
                capacity_ready = ctx.max_containers is not None or auto_cap_is_current
                if name not in unchanged or not settings_unchanged or not capacity_ready:
                    new_health[name] = False
                    if ctx.max_containers is None:
                        new_cap_stale.add(name)
                    continue

                new_health[name] = carried_health.get(name, False)
                if ctx.max_containers is None:
                    new_auto_caps[name] = carried_caps[name]

        with self.lock:
            self.health = new_health
            self.auto_caps = new_auto_caps
            self._cap_stale = new_cap_stale
            self.context_fences = new_context_fences
            if probe:
                self._initial_probe.set()
            else:
                self._initial_probe.clear()

        for event_type, message, level, metadata in events:
            event_logger.log_event(event_type, message, level=level, metadata=metadata)  # type: ignore[arg-type]

        if probe:
            healthy_count = sum(1 for h in new_health.values() if h)
            logger.info(f"loaded {len(contexts)} contexts, {healthy_count} healthy")
        else:
            carried = sum(1 for name, healthy in new_health.items() if healthy)
            logger.info(f"loaded {len(contexts)} contexts (catalog only, {carried} health entries carried forward)")

    def note_initial_probe_done(self) -> None:
        self._initial_probe.set()

    def _await_initial_probe(self, timeout: float = 5.0) -> None:
        self._initial_probe.wait(timeout)  # wait for background health results without probing on requests

    def has_healthy_context(self) -> bool:
        with self.lock:
            return any(self.health.values())

    def _effective_cap(self, name: str, column_value: int | None) -> int:
        if column_value is not None:
            return column_value
        return self.auto_caps.get(name, DERIVED_CAP_FALLBACK)

    def _capacity_snapshot(self, names: list[str]) -> dict[str, tuple[int, int, int]]:
        from CTFd.models import db
        from .models import DesktopDockerContextModel

        rows = DesktopDockerContextModel.query.filter(
            DesktopDockerContextModel.context_name.in_(names),
            DesktopDockerContextModel.enabled.is_(True),
        ).all()
        snapshot = {
            r.context_name: (
                int(r.active_sessions or 0),
                self._effective_cap(r.context_name, r.max_containers),
                int(r.weight or 1),
            )
            for r in rows
        }
        db.session.rollback()
        return snapshot

    def _try_reserve(
        self,
        name: str,
        cap: int,
        fence: ContextFence,
        claim: ReservationClaim | None = None,
    ) -> bool:
        from CTFd.models import db
        from .models import (
            OP_RESERVED,
            OP_SELECTING,
            DesktopDockerContextModel,
            DesktopSessionOperationModel,
            DesktopSettingsModel,
        )

        context_id, hostname, pub_hostname, weight, max_containers, settings_revision = fence
        settings_match = DesktopSettingsModel.query.filter(
            DesktopSettingsModel.key == "_settings_revision",
            DesktopSettingsModel.value == str(settings_revision),
        ).exists()

        try:
            reserved_rows = DesktopDockerContextModel.query.filter(
                DesktopDockerContextModel.id == context_id,
                DesktopDockerContextModel.context_name == name,
                DesktopDockerContextModel.hostname == hostname,
                DesktopDockerContextModel.pub_hostname == pub_hostname,
                DesktopDockerContextModel.weight == weight,
                DesktopDockerContextModel.max_containers == max_containers,
                DesktopDockerContextModel.enabled.is_(True),
                DesktopDockerContextModel.active_sessions < cap,
                settings_match,
            ).update(
                {DesktopDockerContextModel.active_sessions: DesktopDockerContextModel.active_sessions + 1},
                synchronize_session=False,
            )
            if reserved_rows != 1:
                db.session.rollback()
                return False

            if claim is not None:
                now = time.time()
                claimed = DesktopSessionOperationModel.query.filter(
                    DesktopSessionOperationModel.user_id == claim.user_id,
                    DesktopSessionOperationModel.session_uuid == claim.session_uuid,
                    DesktopSessionOperationModel.worker_lease_uuid == claim.worker_lease_uuid,
                    DesktopSessionOperationModel.container_name == claim.container_name,
                    DesktopSessionOperationModel.state == OP_SELECTING,
                    DesktopSessionOperationModel.cancel_requested.is_(False),
                    DesktopSessionOperationModel.capacity_reserved.is_(False),
                ).update(
                    {
                        DesktopSessionOperationModel.state: OP_RESERVED,
                        DesktopSessionOperationModel.docker_context: name,
                        DesktopSessionOperationModel.container_name: claim.container_name,
                        DesktopSessionOperationModel.capacity_reserved: True,
                        DesktopSessionOperationModel.updated_at: now,
                        DesktopSessionOperationModel.heartbeat_at: now,
                    },
                    synchronize_session=False,
                )
                if claimed != 1:
                    db.session.rollback()
                    raise ReservationOwnershipError("creation lease is no longer owned by this worker")

            db.session.commit()  # capacity and ownership must commit together
            return True
        except ReservationOwnershipError:
            raise
        except Exception:
            db.session.rollback()
            raise

    def release_slot(self, context_name: str) -> None:
        from CTFd.models import db
        from .models import DesktopDockerContextModel

        try:
            DesktopDockerContextModel.query.filter(
                DesktopDockerContextModel.context_name == context_name,
                DesktopDockerContextModel.active_sessions > 0,
            ).update(
                {DesktopDockerContextModel.active_sessions: DesktopDockerContextModel.active_sessions - 1},
                synchronize_session=False,
            )
            db.session.commit()  # callers must not have unrelated uncommitted changes
        except Exception:  # the leader audit repairs failed decrements
            db.session.rollback()
            logger.error(f"release_slot failed for {context_name}", exc_info=True)

    @staticmethod
    def release_operation_slot_in_transaction(
        context_name: str,
        user_id: int,
        session_uuid: str,
        worker_lease_uuid: str,
    ) -> bool:  # after release, lock the operation and commit its ownership clear in this transaction
        from .models import DesktopDockerContextModel, DesktopSessionOperationModel

        owner_exists = DesktopSessionOperationModel.query.filter(
            DesktopSessionOperationModel.user_id == user_id,
            DesktopSessionOperationModel.session_uuid == session_uuid,
            DesktopSessionOperationModel.worker_lease_uuid == worker_lease_uuid,
            DesktopSessionOperationModel.docker_context == context_name,
            DesktopSessionOperationModel.capacity_reserved.is_(True),
        ).exists()

        changed = DesktopDockerContextModel.query.filter(  # match admission by locking the context first
            DesktopDockerContextModel.context_name == context_name,
            DesktopDockerContextModel.active_sessions > 0,
            owner_exists,
        ).update(
            {DesktopDockerContextModel.active_sessions: DesktopDockerContextModel.active_sessions - 1},
            synchronize_session=False,
        )
        return changed == 1

    @staticmethod
    def release_active_slot_in_transaction(
        context_name: str,
        user_id: int,
        session_uuid: str,
        worker_lease_uuid: str,
    ) -> bool:
        from .models import (
            DesktopContainerInfoModel,
            DesktopDockerContextModel,
            DesktopSessionOperationModel,
            OP_STOPPING,
        )

        owner_exists = DesktopContainerInfoModel.query.filter(
            DesktopContainerInfoModel.user_id == user_id,
            DesktopContainerInfoModel.session_uuid == session_uuid,
            DesktopContainerInfoModel.docker_context == context_name,
        ).exists()

        operation_owner_exists = DesktopSessionOperationModel.query.filter(
            DesktopSessionOperationModel.user_id == user_id,
            DesktopSessionOperationModel.session_uuid == session_uuid,
            DesktopSessionOperationModel.worker_lease_uuid == worker_lease_uuid,
            DesktopSessionOperationModel.state == OP_STOPPING,
            DesktopSessionOperationModel.docker_context == context_name,
            DesktopSessionOperationModel.capacity_reserved.is_(True),
        ).exists()

        changed = DesktopDockerContextModel.query.filter(  # match admission by locking the context first
            DesktopDockerContextModel.context_name == context_name,
            DesktopDockerContextModel.active_sessions > 0,  # the audit repairs a zero count without another decrement
            owner_exists,
            operation_owner_exists,
        ).update(
            {DesktopDockerContextModel.active_sessions: DesktopDockerContextModel.active_sessions - 1},
            synchronize_session=False,
        )
        return changed == 1

    def _note_capacity_refusal_locked(self, snapshot: dict[str, tuple[int, int, int]]) -> dict | None:
        self._capacity_refusals += 1
        now = time.time()
        if now - self._last_capacity_event_ts < CAPACITY_EVENT_MIN_INTERVAL:
            return None
        meta = {
            "refusals": self._capacity_refusals,
            "hosts": {
                n: {"count": v[0], "cap": v[1], "healthy": self.health.get(n, False)} for n, v in snapshot.items()
            },
        }
        self._last_capacity_event_ts = now
        self._capacity_refusals = 0
        return meta

    def _refuse_at_capacity(self, snapshot: dict[str, tuple[int, int, int]]) -> None:
        with self.lock:
            meta = self._note_capacity_refusal_locked(snapshot)
        if meta is not None:
            event_logger.log_event(
                "capacity_refused",
                "session refused: all healthy hosts at capacity",
                level="warning",
                metadata=meta,
            )
        raise HostsAtCapacityException(AT_CAPACITY)

    def select_and_reserve(self, claim: ReservationClaim | None = None) -> str:
        self._await_initial_probe()
        for attempt in range(2):
            self._refresh_catalog_if_stale()
            with self.lock:
                fences = dict(self.context_fences)
                healthy = [n for n, h in self.health.items() if h and n in fences]
            if not healthy:
                raise HostsUnavailableException(NO_HEALTHY_HOSTS)
            snapshot = self._capacity_snapshot(healthy)
            candidates = rank_candidates(healthy, snapshot)
            for name in candidates:
                if self._try_reserve(name, snapshot[name][1], fences[name], claim):
                    logger.debug(f"select_and_reserve: {name}")
                    return name

            if attempt == 0 and candidates and not self._catalog_is_current():
                logger.info("admission fence changed during reservation; retrying once")
                continue

            logger.info("select_and_reserve refused: all healthy hosts at capacity")
            self._refuse_at_capacity(snapshot)
        raise AssertionError("unreachable")

    def admission_check(self) -> None:
        self._await_initial_probe()
        self._refresh_catalog_if_stale()
        with self.lock:
            healthy = [n for n, h in self.health.items() if h]
        if not healthy:
            raise HostsUnavailableException(NO_HEALTHY_HOSTS)
        snapshot = self._capacity_snapshot(healthy)
        candidates = rank_candidates(healthy, snapshot)
        if candidates:
            return
        self._refuse_at_capacity(snapshot)

    def _catalog_is_current(self) -> bool:
        from CTFd.models import db
        from .models import DesktopDockerContextModel, get_setting

        try:
            settings_revision = int(str(get_setting("_settings_revision")))
            rows = DesktopDockerContextModel.query.filter_by(enabled=True).all()
            current: dict[str, ContextFence] = {
                str(row.context_name): (
                    int(row.id),
                    str(row.hostname) if row.hostname is not None else None,
                    str(row.pub_hostname),
                    int(row.weight) if row.weight is not None else None,
                    int(row.max_containers) if row.max_containers is not None else None,
                    settings_revision,
                )
                for row in rows
            }
        finally:
            db.session.rollback()
        with self.lock:
            return current == self.context_fences

    def _refresh_catalog_if_stale(self) -> None:
        if self._catalog_is_current():  # redis reload notifications can be missed
            return

        with self._load_lock:
            if self._catalog_is_current():
                return
            logger.warning("Docker context catalog changed without a local reload; refreshing before admission")
            already_probed = self._initial_probe.is_set()
            self._load_from_db_serialized(probe=False)  # requests must not wait for docker probes
            if already_probed:
                self._initial_probe.set()  # later reloads must not restart the initial wait

    def _count_committed_sessions(self) -> tuple[dict[str, int], Counter[str], dict[str, set[str]], bool]:
        from CTFd.models import db
        from .models import DesktopContainerInfoModel, DesktopDockerContextModel, DesktopSessionOperationModel

        counters = {c.context_name: int(c.active_sessions or 0) for c in DesktopDockerContextModel.query.all()}
        db_entries = DesktopContainerInfoModel.query.with_entities(
            DesktopContainerInfoModel.docker_context,
            DesktopContainerInfoModel.container_name,
            DesktopContainerInfoModel.session_uuid,
        ).all()
        db_counts = Counter(str(row.docker_context) for row in db_entries)
        known_names: dict[str, set[str]] = {}
        active_sessions: dict[str, set[str]] = {}
        for row in db_entries:
            context_name = str(row.docker_context)
            known_names.setdefault(context_name, set()).add(str(row.container_name))
            session_uuid = str(getattr(row, "session_uuid", "") or "")
            if session_uuid:
                active_sessions.setdefault(context_name, set()).add(session_uuid)
        operation_entries = DesktopSessionOperationModel.query.with_entities(
            DesktopSessionOperationModel.docker_context,
            DesktopSessionOperationModel.container_name,
            DesktopSessionOperationModel.session_uuid,
            DesktopSessionOperationModel.capacity_reserved,
        ).all()
        ambiguous_reservation = False
        for row in operation_entries:
            if not row.capacity_reserved:
                continue
            if not row.docker_context:
                ambiguous_reservation = True
                continue
            context_name = str(row.docker_context)
            container_name = str(row.container_name or "")
            session_uuid = str(getattr(row, "session_uuid", "") or "")
            already_counted = bool(
                (session_uuid and session_uuid in active_sessions.get(context_name, set()))
                or (container_name and container_name in known_names.get(context_name, set()))
            )
            if container_name:
                known_names.setdefault(context_name, set()).add(container_name)
            if not already_counted:
                db_counts[context_name] += 1

        db.session.rollback()
        return counters, db_counts, known_names, ambiguous_reservation

    def _observe_host_counts(
        self,
        counters: dict[str, int],
        db_counts: Counter[str],
        known_names: dict[str, set[str]],
    ) -> dict[str, int]:
        observed: dict[str, int] = {}
        for name, counter in counters.items():
            listing = self.host_manager.list_session_containers_strict(name, "rd-session-")
            if listing is None:  # an unreachable host cannot justify reducing its count
                observed[name] = max(db_counts.get(name, 0), counter)
                continue

            live_names: set[str] = set()
            for entry in listing:
                listed_name = entry.get("name")
                labels = entry.get("labels")
                if listed_name and isinstance(labels, dict) and labels.get(SESSION_LABEL_MANAGED) == "true":
                    live_names.add(str(listed_name))
            untracked_live = live_names - known_names.get(name, set())
            observed[name] = db_counts.get(name, 0) + len(untracked_live)
        return observed

    def _apply_count_heals(self, counters: dict[str, int], updates: dict[str, int]) -> set[str]:
        from CTFd.models import db
        from .models import DesktopDockerContextModel

        applied_names: set[str] = set()
        for name, target in updates.items():
            changed = DesktopDockerContextModel.query.filter(  # preserve reservations made after the scan
                DesktopDockerContextModel.context_name == name,
                DesktopDockerContextModel.active_sessions == counters[name],
            ).update({DesktopDockerContextModel.active_sessions: target}, synchronize_session=False)
            if changed == 1:
                applied_names.add(name)
        if applied_names:
            db.session.commit()
        elif updates:
            db.session.rollback()
        return applied_names

    def audit_counts(self) -> None:  # only the elected leader may reconcile counters
        counters, db_counts, known_names, ambiguous_reservation = self._count_committed_sessions()
        observed = self._observe_host_counts(counters, db_counts, known_names)

        updates, events = plan_count_audit(
            counters,
            observed,
            suppress_down_heal=ambiguous_reservation,  # a reservation without a context may belong to any host
        )

        applied_names = self._apply_count_heals(counters, updates)

        for name, counter, dbn, kind in events:
            if name not in applied_names:
                continue
            event_logger.log_event(
                "capacity_count_drift",
                f"session counter drift on {name}: counter={counter} rows={dbn} ({kind})",
                level="warning",
                metadata={"context_name": name, "counter": counter, "db_count": dbn, "kind": kind},
            )

    def mark_unhealthy(self, context_name: str, reason: str = "unreachable") -> None:
        with self.lock:
            self.health[context_name] = False
            logger.warning(f"context {context_name} marked unhealthy: {reason}")
            event_logger.log_event(
                "host_unhealthy",
                f"context {context_name} marked unhealthy: {reason}",
                level="warning",
                metadata={"context_name": context_name, "reason": reason},
            )

    def mark_healthy(self, context_name: str) -> None:
        with self.lock:
            self.health[context_name] = True
            logger.info(f"context {context_name} marked healthy")
            event_logger.log_event(
                "host_healthy",
                f"context {context_name} marked healthy",
                level="info",
                metadata={"context_name": context_name},
            )

    def get_status(self) -> list[HostStatus]:
        with self.lock:
            names = list(self.health.keys())
            health = dict(self.health)
        snapshot = self._capacity_snapshot(names)
        status: list[HostStatus] = []
        for name in names:
            active, cap, weight = snapshot.get(name, (0, 0, 1))
            status.append(
                {
                    "context_name": name,
                    "pub_hostname": self.host_manager.get_pub_hostname(name),
                    "active_containers": active,
                    "healthy": health[name],
                    "weight": weight,
                    "max_containers": cap,
                    "at_capacity": bool(health[name]) and active >= cap,
                }
            )
        return status

    def health_check(self, log_events: bool = True) -> None:
        from .models import get_setting

        with self.lock:
            names = list(self.health.keys())
            fences = {name: self.context_fences.get(name) for name in names}
            cap_stale = set(self._cap_stale)

        docker_image = str(get_setting("docker_image"))
        storage_limit = str(get_setting("storage_limit") or "").strip()

        for name in names:
            reachable = self.host_manager.ping(name)

            eligible = reachable
            reason = "unreachable"
            if reachable:
                image_compatible = self.host_manager.check_image(name, docker_image)
                storage_compatible = self.host_manager.check_storage_limit_compatibility(name, storage_limit)
                eligible = image_compatible and storage_compatible
                if not image_compatible:
                    reason = "image missing or incompatible"
                elif not storage_compatible:
                    reason = "configured storage limit is unsupported"

            derived_cap: int | None = None
            fence = fences.get(name)
            auto_cap_required = fence is not None and fence[-2] is None
            if eligible and auto_cap_required and name in cap_stale:
                cap, stale = self._derive_auto_cap(name, connected=True)
                if stale:
                    eligible = False
                    reason = "host memory unavailable for capacity validation"
                else:
                    derived_cap = cap

            changed_to_healthy = False
            changed_to_unhealthy = False
            with self.lock:
                if self.context_fences.get(name) != fence:  # ignore probes started before an admin reload
                    continue

                current_health = bool(self.health.get(name))
                if eligible:
                    if derived_cap is not None:
                        self.auto_caps[name] = derived_cap
                        self._cap_stale.discard(name)
                    self.health[name] = True
                    changed_to_healthy = not current_health
                else:
                    self.health[name] = False
                    changed_to_unhealthy = current_health
                self._initial_probe.set()  # one result avoids waiting for unreachable peers

            if changed_to_healthy:
                logger.info(f"health_check: context {name} recovered")
                if log_events:
                    event_logger.log_event(
                        "host_healthy",
                        f"context {name} marked healthy",
                        level="info",
                        metadata={"context_name": name},
                    )
            elif changed_to_unhealthy:
                logger.warning(f"health_check: context {name} marked unhealthy: {reason}")
                if log_events:
                    event_logger.log_event(
                        "host_unhealthy",
                        f"context {name} marked unhealthy: {reason}",
                        level="warning",
                        metadata={"context_name": name, "reason": reason},
                    )
