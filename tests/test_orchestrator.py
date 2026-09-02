import threading
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock, patch

from orchestrator import (
    Orchestrator,
    ReservationClaim,
    ReservationOwnershipError,
    derive_max_containers,
    rank_candidates,
    plan_count_audit,
    DERIVED_CAP_FALLBACK,
)
from docker_host_manager import IMAGE_CONTRACT_VERSION
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
        self.reserve_fences = []
        self.reserve_claims = []

    def snapshot(self, names):
        with self.lock:
            return {n: self.entries[n] for n in names if n in self.entries}

    def try_reserve(self, name, cap, fence, claim=None):
        with self.lock:
            self.reserve_fences.append(fence)
            self.reserve_claims.append(claim)
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
    for context_id, (name, weight, healthy) in enumerate(contexts, start=1):
        cap = (caps or {}).get(name, 999)
        o.health[name] = healthy
        o.context_fences[name] = (context_id, None, f"{name}.example.com", weight, cap, 1)
        entries[name] = ((counts or {}).get(name, 0), cap, weight)
    store = FakeCapacityStore(entries)
    o._capacity_snapshot = store.snapshot
    o._try_reserve = store.try_reserve
    o.release_slot = store.release
    o._catalog_is_current = lambda: True
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


@pytest.mark.parametrize(
    "image_info,healthy,event_type,reason",
    [
        (
            {"contract": IMAGE_CONTRACT_VERSION, "contract_status": "compatible"},
            True,
            "host_healthy",
            None,
        ),
        (
            {"contract": "1", "contract_status": "incompatible"},
            False,
            "host_unhealthy",
            "incompatible image contract",
        ),
        (None, False, "host_unhealthy", "image not found"),
    ],
)
def test_load_health_uses_one_image_probe_and_reports_contract_reason(image_info, healthy, event_type, reason):
    host = MagicMock()
    host.get_connected_contexts.return_value = ["runner-a"]
    host.get_image_info.return_value = image_info
    host.check_storage_limit_compatibility.return_value = True
    host.get_host_memory.return_value = 16 * 1024**3
    context = SimpleNamespace(
        id=41,
        context_name="runner-a",
        hostname=None,
        pub_hostname="runner-a.example.edu",
        weight=1,
        max_containers=5,
    )
    context_model = MagicMock()
    context_model.query.filter_by.return_value.all.return_value = [context]
    settings = {
        "_settings_revision": 19,
        "docker_image": "desktop:latest",
        "memory_limit": "2g",
        "capacity_ram_fraction": 0.7,
        "storage_limit": "",
    }
    o = Orchestrator(host)

    with (
        patch("models.DesktopDockerContextModel", context_model),
        patch("models.get_setting", side_effect=lambda key: settings[key]),
        patch("orchestrator.event_logger") as events,
    ):
        o.load_from_db()

    assert o.health == {"runner-a": healthy}
    host.get_image_info.assert_called_once_with("runner-a", "desktop:latest")
    host.check_storage_limit_compatibility.assert_called_once_with("runner-a", "")
    host.check_image.assert_not_called()
    event = next(call for call in events.log_event.call_args_list if call.args[0] == event_type)
    metadata = event.kwargs["metadata"]
    if reason is None:
        assert metadata["image"] == image_info
    else:
        assert metadata["reason"] == reason
        assert metadata["docker_image"] == "desktop:latest"
        if image_info is not None:
            assert metadata["image"] == image_info
            assert metadata["expected_contract"] == IMAGE_CONTRACT_VERSION


# -- select_and_reserve / admission_check -----------------------------------


def test_atomic_reserve_rechecks_complete_loaded_fence():
    o = Orchestrator(MagicMock())
    model = MagicMock()
    settings_model = MagicMock()
    query = model.query

    context_id_predicate = object()
    context_name_predicate = object()
    hostname_predicate = object()
    pub_hostname_predicate = object()
    weight_predicate = object()
    max_containers_predicate = object()
    enabled_predicate = object()
    cap_predicate = object()
    settings_key_predicate = object()
    settings_value_predicate = object()
    settings_match = object()

    model.id.__eq__.return_value = context_id_predicate
    model.context_name.__eq__.return_value = context_name_predicate
    model.hostname.__eq__.return_value = hostname_predicate
    model.pub_hostname.__eq__.return_value = pub_hostname_predicate
    model.weight.__eq__.return_value = weight_predicate
    model.max_containers.__eq__.return_value = max_containers_predicate
    model.enabled.is_.return_value = enabled_predicate
    model.active_sessions.__lt__.return_value = cap_predicate
    settings_model.key.__eq__.return_value = settings_key_predicate
    settings_model.value.__eq__.return_value = settings_value_predicate
    settings_model.query.filter.return_value.exists.return_value = settings_match
    query.filter.return_value.update.return_value = 1
    fence = (41, "ssh-user@runner-a", "runner-a.example.edu", 3, 7, 19)

    with (
        patch("models.DesktopDockerContextModel", model),
        patch("models.DesktopSettingsModel", settings_model),
        patch("CTFd.models.db") as database,
    ):
        assert o._try_reserve("runner-a", 7, fence) is True

    settings_model.query.filter.assert_called_once_with(settings_key_predicate, settings_value_predicate)

    query.filter.assert_called_once_with(
        context_id_predicate,
        context_name_predicate,
        hostname_predicate,
        pub_hostname_predicate,
        weight_predicate,
        max_containers_predicate,
        enabled_predicate,
        cap_predicate,
        settings_match,
    )
    query.filter.return_value.update.assert_called_once()
    database.session.commit.assert_called_once_with()
    database.session.rollback.assert_not_called()


def test_claimed_reserve_commits_context_and_operation_as_one_transaction():
    o = Orchestrator(MagicMock())
    context_model = MagicMock()
    settings_model = MagicMock()
    operation_model = MagicMock()
    database = MagicMock()
    timeline: list[str] = []

    context_model.active_sessions.__lt__.return_value = object()
    context_model.query.filter.return_value.update.side_effect = lambda *_args, **_kwargs: (
        timeline.append("context") or 1
    )
    settings_model.query.filter.return_value.exists.return_value = object()
    operation_model.query.filter.return_value.update.side_effect = lambda *_args, **_kwargs: (
        timeline.append("operation") or 1
    )
    database.session.commit.side_effect = lambda: timeline.append("commit")

    operation_predicates = [object() for _ in range(7)]
    operation_model.user_id.__eq__.return_value = operation_predicates[0]
    operation_model.session_uuid.__eq__.return_value = operation_predicates[1]
    operation_model.worker_lease_uuid.__eq__.return_value = operation_predicates[2]
    operation_model.container_name.__eq__.return_value = operation_predicates[3]
    operation_model.state.__eq__.return_value = operation_predicates[4]
    operation_model.cancel_requested.is_.return_value = operation_predicates[5]
    operation_model.capacity_reserved.is_.return_value = operation_predicates[6]

    claim = ReservationClaim(
        user_id=7,
        session_uuid="01234567-89ab-4def-8123-456789abcdef",
        worker_lease_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        container_name="rd-session-7-01234567-89a",
    )
    fence = (41, "ssh-user@runner-a", "runner-a.example.edu", 3, 7, 19)

    with (
        patch("models.DesktopDockerContextModel", context_model),
        patch("models.DesktopSettingsModel", settings_model),
        patch("models.DesktopSessionOperationModel", operation_model),
        patch("CTFd.models.db", database),
        patch("orchestrator.time.time", return_value=1234.5),
    ):
        assert o._try_reserve("runner-a", 7, fence, claim) is True

    assert timeline == ["context", "operation", "commit"]
    database.session.commit.assert_called_once_with()
    database.session.rollback.assert_not_called()
    operation_model.user_id.__eq__.assert_called_once_with(claim.user_id)
    operation_model.session_uuid.__eq__.assert_called_once_with(claim.session_uuid)
    operation_model.worker_lease_uuid.__eq__.assert_called_once_with(claim.worker_lease_uuid)
    operation_model.container_name.__eq__.assert_called_once_with(claim.container_name)
    operation_model.state.__eq__.assert_called_once_with("selecting")
    operation_model.cancel_requested.is_.assert_called_once_with(False)
    operation_model.capacity_reserved.is_.assert_called_once_with(False)
    operation_model.query.filter.assert_called_once_with(*operation_predicates)
    update_values = operation_model.query.filter.return_value.update.call_args.args[0]
    assert update_values[operation_model.state] == "reserved"
    assert update_values[operation_model.docker_context] == "runner-a"
    assert update_values[operation_model.container_name] == claim.container_name
    assert update_values[operation_model.capacity_reserved] is True
    assert update_values[operation_model.updated_at] == 1234.5
    assert update_values[operation_model.heartbeat_at] == 1234.5


def test_claimed_reserve_rolls_back_counter_when_operation_ownership_is_lost():
    o = Orchestrator(MagicMock())
    context_model = MagicMock()
    settings_model = MagicMock()
    operation_model = MagicMock()
    database = MagicMock()
    context_model.active_sessions.__lt__.return_value = object()
    context_model.query.filter.return_value.update.return_value = 1
    settings_model.query.filter.return_value.exists.return_value = object()
    operation_model.query.filter.return_value.update.return_value = 0
    claim = ReservationClaim(7, "session", "worker", "rd-session-7-session")
    fence = (41, None, "runner-a.example.edu", 3, 7, 19)

    with (
        patch("models.DesktopDockerContextModel", context_model),
        patch("models.DesktopSettingsModel", settings_model),
        patch("models.DesktopSessionOperationModel", operation_model),
        patch("CTFd.models.db", database),
        pytest.raises(ReservationOwnershipError, match="no longer owned"),
    ):
        o._try_reserve("runner-a", 7, fence, claim)

    database.session.rollback.assert_called_once_with()
    database.session.commit.assert_not_called()


def test_candidate_miss_rolls_back_before_another_host_can_be_tried():
    o = Orchestrator(MagicMock())
    context_model = MagicMock()
    settings_model = MagicMock()
    operation_model = MagicMock()
    database = MagicMock()
    context_model.active_sessions.__lt__.return_value = object()
    context_model.query.filter.return_value.update.return_value = 0
    settings_model.query.filter.return_value.exists.return_value = object()
    claim = ReservationClaim(7, "session", "worker", "rd-session-7-session")
    fence = (41, None, "runner-a.example.edu", 3, 7, 19)

    with (
        patch("models.DesktopDockerContextModel", context_model),
        patch("models.DesktopSettingsModel", settings_model),
        patch("models.DesktopSessionOperationModel", operation_model),
        patch("CTFd.models.db", database),
    ):
        assert o._try_reserve("runner-a", 7, fence, claim) is False

    database.session.rollback.assert_called_once_with()
    database.session.commit.assert_not_called()
    operation_model.query.filter.assert_not_called()


def test_select_and_reserve_passes_typed_claim_to_candidate_update():
    o = make_orchestrator([("runner-a", 1, True)], caps={"runner-a": 5})
    claim = ReservationClaim(7, "session", "worker", "rd-session-7-session")

    assert o.select_and_reserve(claim) == "runner-a"

    assert o._store.reserve_claims == [claim]


def test_missed_catalog_event_reloads_before_reserving():
    o = make_orchestrator([("runner-a", 1, True)], caps={"runner-a": 5})
    refreshed_fence = (1, "student@new-runner", "new-runner.example.edu", 1, 5, 2)

    o._catalog_is_current = MagicMock(side_effect=[False, False])

    def complete_reload():
        with o.lock:
            o.context_fences["runner-a"] = refreshed_fence

    o._load_from_db_serialized = MagicMock(side_effect=complete_reload)

    assert o.select_and_reserve() == "runner-a"
    o._load_from_db_serialized.assert_called_once_with()
    assert o._store.reserve_fences == [refreshed_fence]


def test_catalog_change_during_conditional_update_refreshes_and_retries():
    o = make_orchestrator([("runner-a", 1, True)], caps={"runner-a": 5})
    original_fence = o.context_fences["runner-a"]
    refreshed_fence = (1, "student@new-runner", "new-runner.example.edu", 1, 5, 2)
    o._catalog_is_current = MagicMock(side_effect=[True, False, False, False])

    def complete_reload():
        with o.lock:
            o.context_fences["runner-a"] = refreshed_fence

    o._load_from_db_serialized = MagicMock(side_effect=complete_reload)
    real_reserve = o._try_reserve
    seen_fences = []

    def stale_once(name, cap, fence, claim=None):
        seen_fences.append(fence)
        if len(seen_fences) == 1:
            return False
        return real_reserve(name, cap, fence, claim)

    o._try_reserve = stale_once

    assert o.select_and_reserve() == "runner-a"
    assert seen_fences == [original_fence, refreshed_fence]
    o._load_from_db_serialized.assert_called_once_with()


def test_catalog_change_retry_is_bounded_to_one_second_pass():
    o = make_orchestrator([("runner-a", 1, True)], caps={"runner-a": 5})
    refreshed_fence = (1, "student@new-runner", "new-runner.example.edu", 1, 5, 2)
    o._catalog_is_current = MagicMock(side_effect=[True, False, False, False])

    def complete_reload():
        with o.lock:
            o.context_fences["runner-a"] = refreshed_fence

    o._load_from_db_serialized = MagicMock(side_effect=complete_reload)
    attempted_fences = []

    def lose_reservation(_name, _cap, fence, _claim=None):
        attempted_fences.append(fence)
        return False

    o._try_reserve = lose_reservation

    with patch("orchestrator.event_logger"), pytest.raises(HostsAtCapacityException):
        o.select_and_reserve()

    assert len(attempted_fences) == 2
    assert attempted_fences[-1] == refreshed_fence
    o._load_from_db_serialized.assert_called_once_with()


def test_catalog_comparison_detects_missed_context_change():
    o = Orchestrator(FakeHostManager())
    o.context_fences = {"runner-a": (4, None, "old.example.edu", 1, 8, 3)}
    row = SimpleNamespace(
        id=4,
        context_name="runner-a",
        hostname=None,
        pub_hostname="new.example.edu",
        weight=1,
        max_containers=0,
    )
    context_model = MagicMock()
    context_model.query.filter_by.return_value.all.return_value = [row]

    with (
        patch("models.DesktopDockerContextModel", context_model),
        patch("models.get_setting", return_value=3),
        patch("CTFd.models.db") as database,
    ):
        assert o._catalog_is_current() is False

    database.session.rollback.assert_called_once_with()


def test_concurrent_missed_event_refresh_is_single_flight():
    o = Orchestrator(FakeHostManager())
    worker_count = 12
    outer_checks = threading.Barrier(worker_count)
    caller_state = threading.local()
    state_lock = threading.Lock()
    fresh = False
    reload_count = 0
    errors = []

    def catalog_is_current():
        nonlocal fresh
        with state_lock:
            observed = fresh
        if not getattr(caller_state, "completed_outer_check", False):
            caller_state.completed_outer_check = True
            outer_checks.wait(timeout=5)
        return observed

    def complete_reload():
        nonlocal fresh, reload_count
        with state_lock:
            reload_count += 1
            fresh = True

    o._catalog_is_current = catalog_is_current
    o._load_from_db_serialized = complete_reload

    def worker():
        try:
            o._refresh_catalog_if_stale()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert reload_count == 1


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
    o = make_orchestrator([])
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
    o._try_reserve = lambda name, cap, fence, claim=None: False if name == "best" else real_try(name, cap, fence, claim)
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


def test_audit_counts_does_not_treat_selecting_row_as_a_reservation():
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

    # The claimed counter increment and OP_RESERVED transition are atomic now;
    # a selecting row cannot explain an otherwise leaked counter.
    mock_ctx_model.query.filter.return_value.update.assert_called_once()


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
