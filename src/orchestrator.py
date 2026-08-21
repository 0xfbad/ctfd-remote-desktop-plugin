from __future__ import annotations

import time
import logging
from threading import Lock

from .docker_host_manager import DockerHostManager, ImageInfo, parse_size
from .exceptions import HostsUnavailableException, HostsAtCapacityException, CAPACITY_MESSAGE
from .event_logger import event_logger

logger = logging.getLogger(__name__)

HostStatus = dict[str, str | int | bool | None]

# used when a host's RAM can't be read (host down at derivation time); flagged
# stale and re-derived on recovery so the small runner isn't over-admitted
DERIVED_CAP_FALLBACK = 5
CAPACITY_EVENT_MIN_INTERVAL = 300
# reserve happens up to ~2 min before the session row commits (create semaphore
# queue + VNC wait), so a single-audit overcount is routinely legitimate; only a
# persistent streak is a leak worth healing
AUDIT_OVERCOUNT_WARN_STREAK = 2
AUDIT_OVERCOUNT_HEAL_STREAK = 3


def derive_max_containers(mem_total: int | None, mem_limit: int, fraction: float) -> int:
    if not mem_total or mem_limit <= 0:
        return DERIVED_CAP_FALLBACK
    return max(1, int(fraction * mem_total // mem_limit))


def rank_candidates(healthy: list[str], snapshot: dict[str, tuple[int, int, int]]) -> list[str]:
    """snapshot: {name: (active_sessions, effective_cap, weight)}. returns
    under-cap candidates best-first (score = weight/(active+1), alpha tiebreak).
    >= not ==: recovery or a lowered cap can leave active above cap -> excluded (drain)"""
    out = []
    for name in healthy:
        if name not in snapshot:
            continue
        active, cap, weight = snapshot[name]
        if active >= cap:
            continue
        out.append((-(weight / (active + 1)), name))
    return [n for _, n in sorted(out)]


def plan_count_audit(
    counters: dict[str, int],
    rows: dict[str, int],
    grace: int,
    streaks: dict[str, int],
) -> tuple[dict[str, int], list[tuple[str, int, int, str]], dict[str, int]]:
    """pure audit planner. returns (counter_updates, events[(name, counter, rows, kind)], new_streaks)"""
    updates: dict[str, int] = {}
    events: list[tuple[str, int, int, str]] = []
    new_streaks: dict[str, int] = {}
    for name, counter in counters.items():
        dbn = rows.get(name, 0)
        if counter < dbn:
            # undercount = over-admission risk; rows are committed live sessions, heal up now
            updates[name] = dbn
            events.append((name, counter, dbn, "healed_up"))
        elif counter > dbn + grace:
            s = streaks.get(name, 0) + 1
            if s >= AUDIT_OVERCOUNT_HEAL_STREAK:
                # persistent leaked reservations (create crashed without release); heal down
                updates[name] = dbn
                events.append((name, counter, dbn, "healed_down"))
            else:
                new_streaks[name] = s
                if s >= AUDIT_OVERCOUNT_WARN_STREAK:
                    events.append((name, counter, dbn, "overcount"))
    return updates, events, new_streaks


class Orchestrator:
    def __init__(self, host_manager: DockerHostManager) -> None:
        self.host_manager = host_manager
        self.health: dict[str, bool] = {}
        # per-worker derived caps for NULL-column contexts; explicit caps and the
        # session counter live in the DB row (shared across gunicorn workers)
        self.auto_caps: dict[str, int] = {}
        self._cap_stale: set[str] = set()
        self._capacity_refusals: int = 0
        self._last_capacity_event_ts: float = 0.0
        self._audit_streaks: dict[str, int] = {}
        self.lock = Lock()

    def _derive_auto_cap(self, context_name: str, connected: bool) -> tuple[int, bool]:
        """returns (cap, stale). stale means RAM was unreadable and the fallback was used"""
        from .models import get_setting

        mem_total = self.host_manager.get_host_memory(context_name) if connected else None
        cap = derive_max_containers(
            mem_total,
            parse_size(str(get_setting("memory_limit"))),
            float(get_setting("capacity_ram_fraction") or 0.7),  # type: ignore[arg-type]
        )
        return cap, not mem_total

    def load_from_db(self) -> None:
        from .models import DesktopDockerContextModel, get_setting

        contexts = DesktopDockerContextModel.query.filter_by(enabled=True).all()

        self.host_manager.load_contexts(contexts)
        connected = set(self.host_manager.get_connected_contexts())
        docker_image = str(get_setting("docker_image"))

        # health-check each context outside the lock (network I/O)
        new_health: dict[str, bool] = {}
        new_auto_caps: dict[str, int] = {}
        new_cap_stale: set[str] = set()
        events: list[tuple[str, str, str, dict[str, str | int | ImageInfo | None]]] = []
        for ctx in contexts:
            name = ctx.context_name
            is_connected = name in connected

            if is_connected:
                has_image = self.host_manager.check_image(name, docker_image)
                image_info = self.host_manager.get_image_info(name, docker_image) if has_image else None
            else:
                has_image = False
                image_info = None

            # derive for EVERY context (not just NULL-cap ones) so a later
            # explicit->NULL edit has a real value on every worker
            auto_cap, stale = self._derive_auto_cap(name, is_connected)
            new_auto_caps[name] = auto_cap
            if stale:
                new_cap_stale.add(name)

            healthy = is_connected and has_image
            new_health[name] = healthy

            if ctx.max_containers is not None:
                cap_value, cap_source = ctx.max_containers, "explicit"
            elif stale:
                cap_value, cap_source = auto_cap, "fallback"
            else:
                cap_value, cap_source = auto_cap, "auto"

            if healthy:
                meta: dict[str, str | int | ImageInfo | None] = {
                    "context_name": name,
                    "max_containers": cap_value,
                    "cap_source": cap_source,
                }
                if image_info:
                    meta["image"] = image_info
                events.append(("host_healthy", f"context {name} is healthy", "info", meta))
            else:
                reason = "connection failed" if not is_connected else "image not found"
                events.append(
                    (
                        "host_unhealthy",
                        f"context {name} marked unhealthy: {reason}",
                        "warning",
                        {"context_name": name, "reason": reason},
                    )
                )

        with self.lock:
            self.health = new_health
            self.auto_caps = new_auto_caps
            self._cap_stale = new_cap_stale

        for event_type, message, level, metadata in events:
            event_logger.log_event(event_type, message, level=level, metadata=metadata)  # type: ignore[arg-type]

        healthy_count = sum(1 for h in new_health.values() if h)
        logger.info(f"loaded {len(contexts)} contexts, {healthy_count} healthy")

    def has_healthy_context(self) -> bool:
        with self.lock:
            return any(self.health.values())

    def _effective_cap(self, name: str, column_value: int | None) -> int:
        if column_value is not None:
            return column_value
        return self.auto_caps.get(name, DERIVED_CAP_FALLBACK)

    # -- DB seams (unit tests override these three) --------------------------

    def _capacity_snapshot(self, names: list[str]) -> dict[str, tuple[int, int, int]]:
        from .models import DesktopDockerContextModel

        rows = DesktopDockerContextModel.query.filter(
            DesktopDockerContextModel.context_name.in_(names),
            DesktopDockerContextModel.enabled.is_(True),
        ).all()
        return {
            r.context_name: (
                int(r.active_sessions or 0),
                self._effective_cap(r.context_name, r.max_containers),
                int(r.weight or 1),
            )
            for r in rows
        }

    def _try_reserve(self, name: str, cap: int) -> bool:
        # atomic conditional increment; the WHERE clause IS the admission check,
        # evaluated against committed state under the row lock -> cross-worker
        # safe. mariadb rowcount counts CHANGED rows and the +1 always changes,
        # so rowcount is reliable - never refactor into a form that can no-op
        from CTFd.models import db
        from .models import DesktopDockerContextModel

        n = DesktopDockerContextModel.query.filter(
            DesktopDockerContextModel.context_name == name,
            DesktopDockerContextModel.active_sessions < cap,
        ).update(
            {DesktopDockerContextModel.active_sessions: DesktopDockerContextModel.active_sessions + 1},
            synchronize_session=False,
        )
        db.session.commit()
        return n == 1

    def release_slot(self, context_name: str) -> None:
        # best-effort, never raises; a lost decrement heals via the leader audit.
        # NOTE: commits the ambient scoped session - callers must not hold an
        # uncommitted multi-statement transaction they care about
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
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.error(f"release_slot failed for {context_name}", exc_info=True)

    # ------------------------------------------------------------------------

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
                metadata=meta,  # type: ignore[arg-type]
            )
        raise HostsAtCapacityException(CAPACITY_MESSAGE)

    def select_and_reserve(self) -> str:
        with self.lock:
            healthy = [n for n, h in self.health.items() if h]
        if not healthy:
            raise HostsUnavailableException("no healthy contexts available")
        snapshot = self._capacity_snapshot(healthy)
        for name in rank_candidates(healthy, snapshot):
            if self._try_reserve(name, snapshot[name][1]):
                logger.debug(f"select_and_reserve: {name}")
                return name
        # every healthy context at/over cap (or lost every UPDATE race)
        logger.info("select_and_reserve refused: all healthy hosts at capacity")
        self._refuse_at_capacity(snapshot)
        raise AssertionError("unreachable")

    def admission_check(self) -> None:
        # fast-path probe, no reservation. raises HostsUnavailableException /
        # HostsAtCapacityException when no session could be admitted right now
        with self.lock:
            healthy = [n for n, h in self.health.items() if h]
        if not healthy:
            raise HostsUnavailableException("no healthy docker contexts available")
        snapshot = self._capacity_snapshot(healthy)
        if rank_candidates(healthy, snapshot):
            return
        self._refuse_at_capacity(snapshot)

    def audit_counts(self) -> None:
        # leader-only (called from periodic_cleanup). heals counter drift against
        # the committed session rows
        from collections import Counter
        from CTFd.models import db
        from .models import DesktopDockerContextModel, DesktopContainerInfoModel, get_setting

        counters = {c.context_name: int(c.active_sessions or 0) for c in DesktopDockerContextModel.query.all()}
        rows = Counter(
            r.docker_context
            for r in DesktopContainerInfoModel.query.with_entities(DesktopContainerInfoModel.docker_context).all()
        )
        grace = max(2, int(get_setting("max_concurrent_creates") or 2))

        updates, events, new_streaks = plan_count_audit(counters, dict(rows), grace, self._audit_streaks)
        self._audit_streaks = new_streaks

        for name, target in updates.items():
            DesktopDockerContextModel.query.filter(DesktopDockerContextModel.context_name == name).update(
                {DesktopDockerContextModel.active_sessions: target}, synchronize_session=False
            )
        if updates:
            db.session.commit()

        for name, counter, dbn, kind in events:
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

    def health_check(self) -> None:
        with self.lock:
            names = list(self.health.keys())

        for name in names:
            reachable = self.host_manager.ping(name)
            with self.lock:
                was_healthy = self.health.get(name)

            if reachable and not was_healthy:
                self.mark_healthy(name)
                logger.info(f"health_check: context {name} recovered")
                # a cap derived while the host was down used the fallback and
                # can over-admit the small runner; re-derive from real RAM now
                if name in self._cap_stale:
                    cap, stale = self._derive_auto_cap(name, connected=True)
                    if not stale:
                        with self.lock:
                            self.auto_caps[name] = cap
                            self._cap_stale.discard(name)
            elif not reachable and was_healthy:
                self.mark_unhealthy(name)
                logger.warning(f"health_check: context {name} unreachable")
