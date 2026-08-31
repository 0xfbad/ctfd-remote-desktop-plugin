import threading
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock, patch

from orchestrator import (
    Orchestrator,
    derive_max_containers,
    rank_candidates,
    plan_count_audit,
    DERIVED_CAP_FALLBACK,
)
from exceptions import HostsUnavailableException, HostsAtCapacityException


class FakeHostManager:
    def get_pub_hostname(self, name):
        return f"{name}.example.com"

    def close(self):
        pass

    def list_session_containers_strict(self, _name, _prefix):
        return []


class FakeCapacityStore:
    """in-memory stand-in for the desktop_docker_contexts capacity columns.
    the real _try_reserve is an atomic conditional UPDATE; the fake reproduces
    its semantics under a lock"""

    def __init__(self, entries):
        # entries: {name: (active, cap, weight)}
        self.entries = dict(entries)
        self.lock = threading.Lock()

    def snapshot(self, names):
        with self.lock:
            return {n: self.entries[n] for n in names if n in self.entries}

    def try_reserve(self, name, cap):
        with self.lock:
            active, real_cap, weight = self.entries[name]
            if active >= cap:
                return False
            self.entries[name] = (active + 1, real_cap, weight)
            return True

    def release(self, name):
        with self.lock:
            active, cap, weight = self.entries[name]
            if active > 0:
                self.entries[name] = (active - 1, cap, weight)


def make_orchestrator(contexts, counts=None, caps=None):
    """contexts: list of (name, weight, healthy) tuples"""
    o = Orchestrator(FakeHostManager())
    entries = {}
    for name, weight, healthy in contexts:
        o.health[name] = healthy
        entries[name] = ((counts or {}).get(name, 0), (caps or {}).get(name, 999), weight)
    store = FakeCapacityStore(entries)
    o._capacity_snapshot = store.snapshot
    o._try_reserve = store.try_reserve
    o.release_slot = store.release
    o._store = store
    return o


# -- pure functions ----------------------------------------------------------


def test_rank_candidates_ordering_and_exclusions():
    snapshot = {
        "under": (0, 5, 2),
        "at_cap": (5, 5, 10),
        "over_cap": (7, 5, 10),
        "drain": (0, 0, 10),
        "low_weight": (0, 5, 1),
    }
    healthy = ["under", "at_cap", "over_cap", "drain", "low_weight", "missing"]
    ranked = rank_candidates(healthy, snapshot)
    assert ranked == ["under", "low_weight"]


def test_rank_candidates_alpha_tiebreak():
    snapshot = {"zebra": (0, 5, 1), "alpha": (0, 5, 1)}
    assert rank_candidates(["zebra", "alpha"], snapshot) == ["alpha", "zebra"]


def test_rank_candidates_load_shifts_score():
    # weight 2 with 2 active scores 2/3 < weight 1 idle scoring 1
    snapshot = {"a": (2, 5, 2), "b": (0, 5, 1)}
    assert rank_candidates(["a", "b"], snapshot) == ["b", "a"]


def test_derive_max_containers():
    gib = 1024**3
    assert derive_max_containers(31 * gib, 4 * gib, 0.7) == 5
    assert derive_max_containers(16 * gib, 4 * gib, 0.7) == 2
    # clamp to 1
    assert derive_max_containers(2 * gib, 4 * gib, 0.7) == 1
    # unreadable RAM -> fallback
    assert derive_max_containers(None, 4 * gib, 0.7) == DERIVED_CAP_FALLBACK
    assert derive_max_containers(31 * gib, 0, 0.7) == DERIVED_CAP_FALLBACK


def test_plan_count_audit_heals_up_immediately():
    updates, events, streaks = plan_count_audit({"a": 1}, {"a": 3})
    assert updates == {"a": 3}
    assert events == [("a", 1, 3, "healed_up")]
    assert streaks == {}


def test_plan_count_audit_heals_any_overcount_exactly():
    updates, events, streaks = plan_count_audit({"a": 4}, {"a": 2})
    assert updates == {"a": 2}
    assert events == [("a", 4, 2, "healed_down")]
    assert streaks == {}


def test_plan_count_audit_can_suppress_only_downward_healing():
    updates, events, streaks = plan_count_audit({"a": 9}, {"a": 2}, suppress_down_heal=True)
    assert updates == {} and events == [] and streaks == {}


# -- select_and_reserve / admission_check -----------------------------------


def test_single_healthy_context():
    o = make_orchestrator([("a", 1, True)])
    assert o.select_and_reserve() == "a"


def test_no_healthy_raises_unavailable_not_capacity():
    o = make_orchestrator([("a", 1, False)])
    with pytest.raises(HostsUnavailableException) as exc:
        o.select_and_reserve()
    assert not isinstance(exc.value, HostsAtCapacityException)
    with pytest.raises(HostsUnavailableException):
        o.admission_check()


def test_empty_raises():
    o = Orchestrator(FakeHostManager())
    with pytest.raises(HostsUnavailableException, match="no healthy contexts"):
        o.select_and_reserve()


def test_at_capacity_raises_typed():
    with patch("orchestrator.event_logger"):
        o = make_orchestrator([("a", 1, True)], counts={"a": 2}, caps={"a": 2})
        with pytest.raises(HostsAtCapacityException):
            o.select_and_reserve()
        # is-a HostsUnavailableException so existing routes map to 503
        with pytest.raises(HostsUnavailableException):
            o.admission_check()


def test_unhealthy_under_cap_host_does_not_rescue():
    with patch("orchestrator.event_logger"):
        o = make_orchestrator([("full", 1, True), ("idle", 10, False)], counts={"full": 3}, caps={"full": 3})
        with pytest.raises(HostsAtCapacityException):
            o.select_and_reserve()


def test_cap_boundary_and_release():
    with patch("orchestrator.event_logger"):
        o = make_orchestrator([("a", 1, True)], caps={"a": 2})
        assert o.select_and_reserve() == "a"
        assert o.select_and_reserve() == "a"
        with pytest.raises(HostsAtCapacityException):
            o.select_and_reserve()
        assert o._store.entries["a"][0] == 2  # never 3
        o.release_slot("a")
        assert o.select_and_reserve() == "a"


def test_lost_reserve_race_falls_through_to_next():
    o = make_orchestrator([("best", 5, True), ("second", 1, True)], caps={"best": 5, "second": 5})
    real_try = o._try_reserve
    o._try_reserve = lambda name, cap: False if name == "best" else real_try(name, cap)
    assert o.select_and_reserve() == "second"


def test_recovery_requires_image_before_marking_healthy():
    host = MagicMock()
    host.ping.return_value = True
    host.check_image.return_value = False
    o = Orchestrator(host)
    o.health = {"a": False}
    with patch("models.get_setting", return_value="img:latest"), patch("orchestrator.event_logger"):
        o.health_check()
    assert o.health["a"] is False

    host.check_image.return_value = True
    with patch("models.get_setting", return_value="img:latest"), patch("orchestrator.event_logger"):
        o.health_check()
    assert o.health["a"] is True


def test_admission_check_never_reserves():
    o = make_orchestrator([("a", 1, True)], caps={"a": 5})
    o.admission_check()
    assert o._store.entries["a"][0] == 0


def test_higher_weight_preferred():
    o = make_orchestrator([("a", 1, True), ("b", 5, True)])
    assert o.select_and_reserve() == "b"


def test_capacity_event_dedup():
    o = make_orchestrator([("a", 1, True)], counts={"a": 1}, caps={"a": 1})
    with patch("orchestrator.event_logger") as ev, patch("orchestrator.time") as faketime:
        faketime.time.return_value = 1000.0
        for _ in range(10):
            with pytest.raises(HostsAtCapacityException):
                o.select_and_reserve()
        capacity_events = [c for c in ev.log_event.call_args_list if c.args[0] == "capacity_refused"]
        assert len(capacity_events) == 1

        # after the window, the next refusal emits with the accumulated count
        faketime.time.return_value = 1400.0
        with pytest.raises(HostsAtCapacityException):
            o.select_and_reserve()
        capacity_events = [c for c in ev.log_event.call_args_list if c.args[0] == "capacity_refused"]
        assert len(capacity_events) == 2
        meta = capacity_events[1].kwargs["metadata"]
        assert meta["refusals"] == 10
        assert meta["hosts"]["a"]["count"] == 1 and meta["hosts"]["a"]["cap"] == 1


def test_get_status_carries_capacity_fields():
    o = make_orchestrator([("a", 1, True), ("b", 2, False)], counts={"a": 3, "b": 1}, caps={"a": 3, "b": 5})
    status = {s["context_name"]: s for s in o.get_status()}
    assert status["a"]["active_containers"] == 3
    assert status["a"]["max_containers"] == 3
    assert status["a"]["at_capacity"] is True
    # unhealthy hosts are never "at capacity" (they're just down)
    assert status["b"]["at_capacity"] is False
    assert status["b"]["weight"] == 2


def test_concurrent_select_and_reserve_respects_total_cap():
    num_threads = 20
    o = make_orchestrator(
        [("a", 2, True), ("b", 3, True), ("c", 1, True)],
        caps={"a": 2, "b": 2, "c": 1},
    )
    barrier = threading.Barrier(num_threads + 1)
    results, errors = [], []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            with patch("orchestrator.event_logger"):
                r = o.select_and_reserve()
            with lock:
                results.append(r)
        except HostsAtCapacityException as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    assert len(results) == 5
    assert len(errors) == num_threads - 5
    for name, cap in (("a", 2), ("b", 2), ("c", 1)):
        assert o._store.entries[name][0] <= cap


def test_audit_counts_applies_planner():
    o = make_orchestrator([("a", 1, True)])

    ctx_row = MagicMock(context_name="a", active_sessions=7)
    info_row = MagicMock(docker_context="a")

    mock_ctx_model = MagicMock()
    mock_ctx_model.query.all.return_value = [ctx_row]
    mock_ctx_model.query.filter.return_value.update.return_value = 1
    mock_info_model = MagicMock()
    mock_info_model.query.with_entities.return_value.all.return_value = [info_row, info_row]
    mock_operation_model = MagicMock()
    mock_operation_model.query.with_entities.return_value.all.return_value = []

    with (
        patch.dict("sys.modules"),
        patch("models.DesktopDockerContextModel", mock_ctx_model),
        patch("models.DesktopContainerInfoModel", mock_info_model),
        patch("models.DesktopSessionOperationModel", mock_operation_model),
        patch("container_manager.db", MagicMock()),
        patch("orchestrator.event_logger") as ev,
    ):
        from CTFd import models as ctfd_models  # noqa: F401  (db stubbed in conftest)

        o.audit_counts()

        drift_events = [c for c in ev.log_event.call_args_list if c.args[0] == "capacity_count_drift"]
        kinds = [c.kwargs["metadata"]["kind"] for c in drift_events]
        assert kinds == ["healed_down"]


def test_audit_counts_does_not_overwrite_a_counter_changed_during_host_scan():
    o = make_orchestrator([("a", 1, True)])
    ctx_row = MagicMock(context_name="a", active_sessions=7)
    info_row = MagicMock(docker_context="a")
    mock_ctx_model = MagicMock()
    mock_ctx_model.query.all.return_value = [ctx_row]
    # A zero rowcount represents a concurrent reservation/release changing the
    # counter after the audit snapshot and before its compare-and-set.
    mock_ctx_model.query.filter.return_value.update.return_value = 0
    mock_info_model = MagicMock()
    mock_info_model.query.with_entities.return_value.all.return_value = [info_row, info_row]
    mock_operation_model = MagicMock()
    mock_operation_model.query.with_entities.return_value.all.return_value = []

    with (
        patch("models.DesktopDockerContextModel", mock_ctx_model),
        patch("models.DesktopContainerInfoModel", mock_info_model),
        patch("models.DesktopSessionOperationModel", mock_operation_model),
        patch("orchestrator.event_logger") as ev,
    ):
        o.audit_counts()

    drift_events = [call for call in ev.log_event.call_args_list if call.args[0] == "capacity_count_drift"]
    assert drift_events == []


def test_audit_counts_never_heals_down_when_host_listing_fails():
    o = make_orchestrator([("a", 1, True)])
    o.host_manager.list_session_containers_strict = MagicMock(return_value=None)

    ctx_row = MagicMock(context_name="a", active_sessions=7)
    mock_ctx_model = MagicMock()
    mock_ctx_model.query.all.return_value = [ctx_row]
    mock_info_model = MagicMock()
    mock_info_model.query.with_entities.return_value.all.return_value = []
    mock_operation_model = MagicMock()
    mock_operation_model.query.with_entities.return_value.all.return_value = []

    with (
        patch("models.DesktopDockerContextModel", mock_ctx_model),
        patch("models.DesktopContainerInfoModel", mock_info_model),
        patch("models.DesktopSessionOperationModel", mock_operation_model),
        patch("orchestrator.event_logger"),
    ):
        for _ in range(4):
            o.audit_counts()

    mock_ctx_model.query.filter.return_value.update.assert_not_called()


def test_audit_counts_uses_live_container_count_after_row_removed():
    o = make_orchestrator([("a", 1, True)])
    o.host_manager.list_session_containers_strict = MagicMock(
        return_value=[
            {
                "name": "rd-session-1",
                "status": "running",
                "labels": {"org.ctfd.remote-desktop.managed": "true"},
            }
        ]
    )

    ctx_row = MagicMock(context_name="a", active_sessions=0)
    mock_ctx_model = MagicMock()
    mock_ctx_model.query.all.return_value = [ctx_row]
    mock_info_model = MagicMock()
    mock_info_model.query.with_entities.return_value.all.return_value = []
    mock_operation_model = MagicMock()
    mock_operation_model.query.with_entities.return_value.all.return_value = []

    with (
        patch("models.DesktopDockerContextModel", mock_ctx_model),
        patch("models.DesktopContainerInfoModel", mock_info_model),
        patch("models.DesktopSessionOperationModel", mock_operation_model),
        patch("orchestrator.event_logger"),
    ):
        o.audit_counts()

    update = mock_ctx_model.query.filter.return_value.update
    update.assert_called_once()


def test_audit_counts_excludes_unmanaged_prefix_collisions():
    o = make_orchestrator([("a", 1, True)])
    o.host_manager.list_session_containers_strict = MagicMock(
        return_value=[{"name": "rd-session-unrelated", "status": "running", "labels": {}}]
    )
    ctx_row = MagicMock(context_name="a", active_sessions=1)
    mock_ctx_model = MagicMock()
    mock_ctx_model.query.all.return_value = [ctx_row]
    mock_ctx_model.query.filter.return_value.update.return_value = 1
    mock_info_model = MagicMock()
    mock_info_model.query.with_entities.return_value.all.return_value = []
    mock_operation_model = MagicMock()
    mock_operation_model.query.with_entities.return_value.all.return_value = []

    with (
        patch("models.DesktopDockerContextModel", mock_ctx_model),
        patch("models.DesktopContainerInfoModel", mock_info_model),
        patch("models.DesktopSessionOperationModel", mock_operation_model),
        patch("orchestrator.event_logger"),
    ):
        o.audit_counts()

    mock_ctx_model.query.filter.return_value.update.assert_called_once()


def test_audit_counts_includes_durable_operation_reservations():
    o = make_orchestrator([("a", 1, True)])
    ctx_row = MagicMock(context_name="a", active_sessions=1)
    operation_row = SimpleNamespace(
        docker_context="a",
        container_name="rd-session-7-reserved",
        capacity_reserved=True,
        state="reserved",
    )
    mock_ctx_model = MagicMock()
    mock_ctx_model.query.all.return_value = [ctx_row]
    mock_info_model = MagicMock()
    mock_info_model.query.with_entities.return_value.all.return_value = []
    mock_operation_model = MagicMock()
    mock_operation_model.query.with_entities.return_value.all.return_value = [operation_row]

    with (
        patch("models.DesktopDockerContextModel", mock_ctx_model),
        patch("models.DesktopContainerInfoModel", mock_info_model),
        patch("models.DesktopSessionOperationModel", mock_operation_model),
        patch("orchestrator.event_logger"),
    ):
        o.audit_counts()

    mock_ctx_model.query.filter.return_value.update.assert_not_called()


def test_audit_counts_includes_reserved_operation_without_container_name():
    o = make_orchestrator([("a", 1, True)])
    ctx_row = MagicMock(context_name="a", active_sessions=1)
    operation_row = SimpleNamespace(
        docker_context="a",
        container_name=None,
        session_uuid="12345678-90ab-4cde-8f01-234567890abc",
        capacity_reserved=True,
        state="reserved",
    )
    mock_ctx_model = MagicMock()
    mock_ctx_model.query.all.return_value = [ctx_row]
    mock_info_model = MagicMock()
    mock_info_model.query.with_entities.return_value.all.return_value = []
    mock_operation_model = MagicMock()
    mock_operation_model.query.with_entities.return_value.all.return_value = [operation_row]

    with (
        patch("models.DesktopDockerContextModel", mock_ctx_model),
        patch("models.DesktopContainerInfoModel", mock_info_model),
        patch("models.DesktopSessionOperationModel", mock_operation_model),
        patch("orchestrator.event_logger"),
    ):
        o.audit_counts()

    mock_ctx_model.query.filter.return_value.update.assert_not_called()


def test_audit_counts_deduplicates_operation_for_same_active_generation():
    o = make_orchestrator([("a", 1, True)])
    ctx_row = MagicMock(context_name="a", active_sessions=1)
    session_uuid = "12345678-90ab-4cde-8f01-234567890abc"
    container_name = "rd-session-7-12345678-90a"
    info_row = SimpleNamespace(docker_context="a", container_name=container_name, session_uuid=session_uuid)
    operation_row = SimpleNamespace(
        docker_context="a",
        container_name=container_name,
        session_uuid=session_uuid,
        capacity_reserved=True,
        state="stopping",
    )
    mock_ctx_model = MagicMock()
    mock_ctx_model.query.all.return_value = [ctx_row]
    mock_info_model = MagicMock()
    mock_info_model.query.with_entities.return_value.all.return_value = [info_row]
    mock_operation_model = MagicMock()
    mock_operation_model.query.with_entities.return_value.all.return_value = [operation_row]

    with (
        patch("models.DesktopDockerContextModel", mock_ctx_model),
        patch("models.DesktopContainerInfoModel", mock_info_model),
        patch("models.DesktopSessionOperationModel", mock_operation_model),
        patch("orchestrator.event_logger"),
    ):
        o.audit_counts()

    mock_ctx_model.query.filter.return_value.update.assert_not_called()


def test_audit_counts_suppresses_down_heal_during_select_reservation_gap():
    o = make_orchestrator([("a", 1, True)])
    ctx_row = MagicMock(context_name="a", active_sessions=1)
    operation_row = SimpleNamespace(
        docker_context=None,
        container_name="rd-session-7-selecting",
        capacity_reserved=False,
        state="selecting",
    )
    mock_ctx_model = MagicMock()
    mock_ctx_model.query.all.return_value = [ctx_row]
    mock_info_model = MagicMock()
    mock_info_model.query.with_entities.return_value.all.return_value = []
    mock_operation_model = MagicMock()
    mock_operation_model.query.with_entities.return_value.all.return_value = [operation_row]

    with (
        patch("models.DesktopDockerContextModel", mock_ctx_model),
        patch("models.DesktopContainerInfoModel", mock_info_model),
        patch("models.DesktopSessionOperationModel", mock_operation_model),
        patch("orchestrator.event_logger"),
    ):
        o.audit_counts()

    mock_ctx_model.query.filter.return_value.update.assert_not_called()


def test_audit_counts_suppresses_down_heal_for_unassigned_durable_reservation():
    o = make_orchestrator([("a", 1, True)])
    ctx_row = MagicMock(context_name="a", active_sessions=1)
    operation_row = SimpleNamespace(
        docker_context=None,
        container_name=None,
        session_uuid="12345678-90ab-4cde-8f01-234567890abc",
        capacity_reserved=True,
        state="reserved",
    )
    mock_ctx_model = MagicMock()
    mock_ctx_model.query.all.return_value = [ctx_row]
    mock_info_model = MagicMock()
    mock_info_model.query.with_entities.return_value.all.return_value = []
    mock_operation_model = MagicMock()
    mock_operation_model.query.with_entities.return_value.all.return_value = [operation_row]

    with (
        patch("models.DesktopDockerContextModel", mock_ctx_model),
        patch("models.DesktopContainerInfoModel", mock_info_model),
        patch("models.DesktopSessionOperationModel", mock_operation_model),
        patch("orchestrator.event_logger"),
    ):
        o.audit_counts()

    mock_ctx_model.query.filter.return_value.update.assert_not_called()
