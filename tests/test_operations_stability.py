from __future__ import annotations

import io
import tarfile
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from container_manager import ContainerManager
from docker_host_manager import DockerHostManager
from models import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_HELD,
    OP_FAILED,
)
from orchestrator import Orchestrator


def _deadline_archive(value: int | str, *, mtime: int, mode: int = 0o600) -> bytes:
    payload = f"{value}\n".encode("ascii")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("max-lifetime-deadline")
        member.size = len(payload)
        member.mode = mode
        member.uid = 0
        member.gid = 0
        member.mtime = mtime
        archive.addfile(member, io.BytesIO(payload))
    return buffer.getvalue()


def test_paused_deadline_extension_is_verified_and_retry_idempotent():
    manager = DockerHostManager()
    manager._context_configs = {"runner-a": "unix:///fake.sock"}
    container = MagicMock(status="paused")
    client = MagicMock()
    client.containers.get.return_value = container
    current_archive = [_deadline_archive(2100, mtime=1000)]

    container.get_archive.side_effect = lambda _path: (iter([current_archive[0]]), {})

    def put_archive(_path, payload):
        current_archive[0] = payload
        return True

    container.put_archive.side_effect = put_archive

    with (
        patch.object(manager, "_get_client", return_value=client),
        patch("docker_host_manager.time.time", return_value=2000.0),
    ):
        deadline = manager.extend_paused_lifetime_deadline(
            "runner-a",
            "rd-session-7-0123456789ab",
            paused_at=1500.0,
        )
        # a retry at the same instant reads the updated mtime and does not credit the same 500 seconds twice
        retried = manager.extend_paused_lifetime_deadline(
            "runner-a",
            "rd-session-7-0123456789ab",
            paused_at=1500.0,
        )

    assert deadline == 2600
    assert retried == 2600
    assert container.put_archive.call_count == 1


def test_paused_deadline_extension_refuses_near_expiry_without_writing():
    manager = DockerHostManager()
    manager._context_configs = {"runner-a": "unix:///fake.sock"}
    container = MagicMock(status="paused")
    container.get_archive.return_value = (iter([_deadline_archive(2010, mtime=1900)]), {})
    client = MagicMock()
    client.containers.get.return_value = container

    with (
        patch.object(manager, "_get_client", return_value=client),
        patch("docker_host_manager.time.time", return_value=2000.0),
    ):
        with pytest.raises(ValueError, match="too close"):
            manager.extend_paused_lifetime_deadline(
                "runner-a",
                "rd-session-7-0123456789ab",
                paused_at=1999.0,
                minimum_remaining=60,
            )

    container.put_archive.assert_not_called()


def _held_row() -> SimpleNamespace:
    return SimpleNamespace(
        user_id=7,
        container_id="container-id",
        container_name="rd-session-7-0123456789ab",
        docker_context="runner-a",
        session_uuid="01234567-89ab-4def-8123-456789abcdef",
        lifecycle_state=LIFECYCLE_HELD,
        paused_at=1000.0,
        timer_started=True,
        timer_start_time=500.0,
    )


def test_unpause_extends_deadline_before_thaw(container_manager):
    manager = container_manager
    row = _held_row()
    model = MagicMock()
    model.query.filter_by.return_value.first.return_value = row
    order: list[str] = []
    manager.host_manager.extend_paused_lifetime_deadline.side_effect = lambda *_args, **_kwargs: order.append(
        "deadline"
    )
    manager.host_manager.unpause_container.side_effect = lambda *_args: order.append("unpause")

    with (
        patch("container_manager.DesktopContainerInfoModel", model),
        patch("container_manager.db"),
        patch.object(manager, "_locked_operation", return_value=None),
        patch("container_manager.time.time", return_value=1600.0),
    ):
        result = manager.unpause_session(7)

    assert result["success"] is True
    assert order == ["deadline", "unpause"]
    assert row.timer_start_time == 1100.0


def test_unpause_deadline_failure_preserves_hold_without_thaw(container_manager):
    manager = container_manager
    row = _held_row()
    model = MagicMock()
    model.query.filter_by.return_value.first.return_value = row
    manager.host_manager.extend_paused_lifetime_deadline.side_effect = ValueError("deadline unreadable")

    with (
        patch("container_manager.DesktopContainerInfoModel", model),
        patch("container_manager.db"),
        patch.object(manager, "_locked_operation", return_value=None),
    ):
        result = manager.unpause_session(7)

    assert result["success"] is False
    assert "deadline unreadable" in str(result["error"])
    assert row.lifecycle_state == LIFECYCLE_HELD
    assert row.paused_at == 1000.0
    manager.host_manager.unpause_container.assert_not_called()


def test_terminal_create_update_releases_exact_owner_before_operation_lock(container_manager):
    manager = container_manager
    operation = SimpleNamespace(
        user_id=7,
        session_uuid="01234567-89ab-4def-8123-456789abcdef",
        worker_lease_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        docker_context="runner-a",
        capacity_reserved=True,
        cancel_requested=False,
        state="reserved",
        updated_at=0.0,
        heartbeat_at=0.0,
    )
    order: list[str] = []
    manager.orchestrator.release_operation_slot_in_transaction.side_effect = lambda *_args: order.append("context")

    def lock_operation(_user_id):
        order.append("operation")
        return operation

    mock_db = MagicMock()
    mock_db.session.commit.side_effect = lambda: order.append("commit")
    with patch.object(manager, "_locked_operation", side_effect=lock_operation), patch("container_manager.db", mock_db):
        updated = manager._update_operation(
            7,
            operation.session_uuid,
            operation.worker_lease_uuid,
            OP_FAILED,
            release_capacity_from="runner-a",
            capacity_reserved=False,
            docker_context=None,
        )

    assert updated is True
    assert order == ["context", "operation", "commit"]
    assert operation.capacity_reserved is False
    assert operation.docker_context is None
    manager.orchestrator.release_slot.assert_not_called()


def test_stale_recovery_releases_takeover_owner_atomically(container_manager):
    manager = container_manager
    operation = SimpleNamespace(
        user_id=7,
        session_uuid="01234567-89ab-4def-8123-456789abcdef",
        worker_lease_uuid="old-worker",
        state="cleanup_pending",
        cancel_requested=True,
        updated_at=0.0,
        docker_context="runner-a",
        container_name="rd-session-7-0123456789ab",
        capacity_reserved=True,
        error=None,
    )
    operation_model = MagicMock()
    operation_model.updated_at.__le__.return_value = MagicMock()
    operation_model.query.filter.return_value.all.return_value = [operation]
    manager.host_manager.list_session_containers_strict.return_value = []

    with (
        patch("container_manager.DesktopSessionOperationModel", operation_model),
        patch.object(manager, "_locked_operation", return_value=operation),
        patch.object(manager, "_locked_active_row", return_value=None),
        patch("container_manager.db"),
        patch("container_manager.time.time", return_value=1000.0),
        patch("container_manager.uuid.uuid4", return_value="takeover-worker"),
    ):
        manager._recover_stale_operations()

    manager.orchestrator.release_operation_slot_in_transaction.assert_called_once_with(
        "runner-a",
        7,
        operation.session_uuid,
        "takeover-worker",
    )
    manager.orchestrator.release_slot.assert_not_called()
    assert operation.state == OP_FAILED
    assert operation.capacity_reserved is False
    assert operation.docker_context is None


def test_periodic_cleanup_verifies_only_ordinary_active_rows(container_manager):
    manager = container_manager
    active = SimpleNamespace(
        user_id=7,
        lifecycle_state=LIFECYCLE_ACTIVE,
        paused_at=None,
        timer_start_time=None,
    )
    held = SimpleNamespace(
        user_id=8,
        lifecycle_state=LIFECYCLE_HELD,
        paused_at=1000.0,
        timer_start_time=500.0,
    )
    model = MagicMock()
    model.query.with_entities.return_value.all.return_value = []
    model.query.all.return_value = [active, held]
    model.query.filter_by.return_value.all.return_value = [active, held]

    with (
        patch("container_manager.DesktopContainerInfoModel", model),
        patch.object(manager, "_verify_or_reap") as verify,
        patch.object(manager, "_recover_stale_operations"),
        patch.object(manager, "_reconcile_orphans"),
    ):
        manager.periodic_cleanup()

    verify.assert_called_once_with(active)


def test_readiness_disables_proxy_and_obeys_total_monotonic_budget():
    manager = ContainerManager(MagicMock(), MagicMock())
    settings = {"vnc_ready_attempts": 4, "http_request_timeout": 1}
    clock = [0.0]
    opener = MagicMock()

    def fail_after_timeout(_request, timeout):
        clock[0] += timeout
        raise urllib.error.URLError("blackholed")

    opener.open.side_effect = fail_after_timeout
    with (
        patch.object(manager, "_get_setting", side_effect=lambda key: settings[key]),
        patch("urllib.request.build_opener", return_value=opener) as build_opener,
        patch("container_manager.time.monotonic", side_effect=lambda: clock[0]),
        patch("container_manager.time.sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)),
    ):
        assert manager.wait_for_vnc_ready("runner.internal", 40000) is False

    proxy_handler = build_opener.call_args.args[0]
    assert proxy_handler.proxies == {}
    assert opener.open.call_count == 2
    assert opener.open.call_args.kwargs["timeout"] == 0.5
    assert clock[0] == 2.0


@pytest.mark.parametrize(
    "info,expected",
    [
        (
            {
                "Driver": "overlay2",
                "DriverStatus": [("Backing Filesystem", "xfs"), ("Supports d_type", "true")],
            },
            True,
        ),
        (
            {
                "Driver": "overlay2",
                "DriverStatus": [("Backing Filesystem", "extfs"), ("Supports d_type", "true")],
            },
            False,
        ),
        ({"Driver": "overlay2", "DriverStatus": []}, False),
    ],
)
def test_storage_limit_eligibility_fails_closed_on_daemon_features(info, expected):
    manager = DockerHostManager()
    manager._context_configs = {"runner-a": "unix:///fake.sock"}
    client = MagicMock()
    client.info.return_value = info
    with patch.object(manager, "_get_client", return_value=client):
        assert manager.check_storage_limit_compatibility("runner-a", "20g") is expected


def test_recovery_derives_auto_cap_before_health_publication():
    host = MagicMock()
    host.ping.return_value = True
    host.check_image.return_value = True
    host.check_storage_limit_compatibility.return_value = True
    orchestrator = Orchestrator(host)
    orchestrator.health = {"runner-a": False}
    orchestrator.context_fences = {
        "runner-a": (1, None, "runner.example", 1, None, 1),
    }
    orchestrator.auto_caps = {"runner-a": 5}
    orchestrator._cap_stale = {"runner-a"}

    def memory_probe(_context_name):
        assert orchestrator.health["runner-a"] is False
        return 4 * 1024**3

    host.get_host_memory.side_effect = memory_probe
    settings = {
        "docker_image": "desktop:latest",
        "storage_limit": "",
        "memory_limit": "2g",
        "capacity_ram_fraction": 0.7,
    }
    with patch("models.get_setting", side_effect=lambda key: settings[key]), patch("orchestrator.event_logger"):
        orchestrator.health_check()

    assert orchestrator.auto_caps["runner-a"] == 1
    assert orchestrator.health["runner-a"] is True
    assert "runner-a" not in orchestrator._cap_stale


def test_periodic_health_rechecks_image_contract_for_healthy_host():
    host = MagicMock()
    host.ping.return_value = True
    host.check_image.return_value = False
    host.check_storage_limit_compatibility.return_value = True
    orchestrator = Orchestrator(host)
    orchestrator.health = {"runner-a": True}
    orchestrator.context_fences = {
        "runner-a": (1, None, "runner.example", 1, 2, 1),
    }
    settings = {"docker_image": "desktop:latest", "storage_limit": "20g"}

    with patch("models.get_setting", side_effect=lambda key: settings[key]), patch("orchestrator.event_logger"):
        orchestrator.health_check()

    host.check_image.assert_called_once_with("runner-a", "desktop:latest")
    host.check_storage_limit_compatibility.assert_called_once_with("runner-a", "20g")
    assert orchestrator.health["runner-a"] is False
