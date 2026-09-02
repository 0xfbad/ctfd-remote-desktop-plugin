from types import SimpleNamespace
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from container_manager import ContainerManager
from docker_host_manager import DockerHostManager
from models import OP_FAILED, OP_RESERVED, OP_SELECTING
from orchestrator import ReservationClaim
from settings import SETTING_SPECS


def _manager_with_context(limit: int = 1) -> DockerHostManager:
    manager = DockerHostManager()
    manager._context_configs = {"alpha": "unix:///fake.sock"}
    manager._init_semaphores(limit)
    return manager


def test_matching_reload_preserves_inflight_semaphore_generation():
    manager = _manager_with_context(limit=1)
    token = manager.acquire_semaphore("alpha", timeout=0)
    assert token is not None

    manager._init_semaphores(1)

    assert manager._semaphores["alpha"] is token
    with pytest.raises(Exception, match="server busy"):
        manager.acquire_semaphore("alpha", timeout=0)

    manager.release_semaphore(token)
    replacement_token = manager.acquire_semaphore("alpha", timeout=0)
    assert replacement_token is token
    manager.release_semaphore(replacement_token)


def test_release_targets_acquired_object_after_limit_reload():
    manager = _manager_with_context(limit=1)
    old_token = manager.acquire_semaphore("alpha", timeout=0)
    assert old_token is not None

    manager._init_semaphores(2)
    first_new_token = manager.acquire_semaphore("alpha", timeout=0)
    assert first_new_token is not None
    assert first_new_token is not old_token

    # releasing an in flight token from the old generation must not add capacity to the new semaphore
    manager.release_semaphore(old_token)
    second_new_token = manager.acquire_semaphore("alpha", timeout=0)
    assert second_new_token is first_new_token
    with pytest.raises(Exception, match="server busy"):
        manager.acquire_semaphore("alpha", timeout=0)

    manager.release_semaphore(first_new_token)
    manager.release_semaphore(second_new_token)


def test_cancelled_reserved_fence_releases_slot_before_semaphore_acquire():
    host_manager = MagicMock()
    orchestrator = MagicMock()
    manager = ContainerManager(host_manager, orchestrator)
    host_manager.get_connection_hostnames.return_value = (
        "alpha.example.com",
        "alpha.example.com",
    )
    host_manager.ping.return_value = True
    orchestrator.select_and_reserve.return_value = "alpha"

    states: list[str] = []

    def update_operation(_user_id, _session_uuid, _worker_uuid, state, **_kwargs):
        states.append(state)
        return state != OP_RESERVED

    with (
        patch("container_manager._display_name", return_value=(None, "alice")),
        patch.object(manager, "_resolve_username", return_value="alice"),
        patch.object(manager, "_update_operation", side_effect=update_operation),
        patch("container_manager.event_logger"),
    ):
        manager._create_container_background(
            user_id=7,
            container_url="http://ctfd",
            extra_hosts=None,
            session_uuid="01234567-89ab-4def-8123-456789abcdef",
            worker_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    assert OP_RESERVED in states
    assert OP_FAILED in states
    host_manager.acquire_semaphore.assert_not_called()
    host_manager.release_semaphore.assert_not_called()
    host_manager.run_container.assert_not_called()
    host_manager.stop_container.assert_not_called()
    host_manager.force_remove_container.assert_not_called()
    orchestrator.select_and_reserve.assert_called_once_with(
        ReservationClaim(
            user_id=7,
            session_uuid="01234567-89ab-4def-8123-456789abcdef",
            worker_lease_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            container_name="rd-session-7-01234567-89a",
        )
    )
    # the terminal operation update decrements the exact reservation, a post commit release by name would be aba prone
    orchestrator.release_slot.assert_not_called()


def test_semaphore_failure_releases_reserved_slot_without_docker_cleanup():
    host_manager = MagicMock()
    orchestrator = MagicMock()
    manager = ContainerManager(host_manager, orchestrator)
    host_manager.get_connection_hostnames.return_value = (
        "alpha.example.com",
        "alpha.example.com",
    )
    host_manager.acquire_semaphore.side_effect = RuntimeError("server busy")
    host_manager.ping.return_value = True
    orchestrator.select_and_reserve.return_value = "alpha"

    states: list[str] = []

    def update_operation(_user_id, _session_uuid, _worker_uuid, state, **_kwargs):
        states.append(state)
        return True

    with (
        patch("container_manager._display_name", return_value=(None, "alice")),
        patch.object(manager, "_resolve_username", return_value="alice"),
        patch.object(manager, "_update_operation", side_effect=update_operation),
        patch("container_manager.event_logger"),
    ):
        manager._create_container_background(
            user_id=7,
            container_url="http://ctfd",
            extra_hosts=None,
            session_uuid="01234567-89ab-4def-8123-456789abcdef",
            worker_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    assert states[:2] == [OP_SELECTING, OP_RESERVED]
    assert states[-1] == OP_FAILED
    host_manager.acquire_semaphore.assert_called_once_with("alpha")
    host_manager.release_semaphore.assert_not_called()
    host_manager.run_container.assert_not_called()
    host_manager.stop_container.assert_not_called()
    host_manager.force_remove_container.assert_not_called()
    orchestrator.select_and_reserve.assert_called_once_with(
        ReservationClaim(
            user_id=7,
            session_uuid="01234567-89ab-4def-8123-456789abcdef",
            worker_lease_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            container_name="rd-session-7-01234567-89a",
        )
    )
    orchestrator.release_slot.assert_not_called()


def test_session_created_telemetry_failure_does_not_cleanup_committed_session():
    host_manager = MagicMock()
    orchestrator = MagicMock()
    manager = ContainerManager(host_manager, orchestrator)
    semaphore_token = object()
    host_manager.acquire_semaphore.return_value = semaphore_token
    host_manager.get_connection_hostnames.return_value = (
        "alpha.example.com",
        "alpha.example.com",
    )
    host_manager.run_container.return_value = {
        "container_id": "container-id",
        "ports": {"5900/tcp": 40001, "6080/tcp": 40002, "22/tcp": 40003, "7682/tcp": 40004},
    }
    # context names are admin labels not valid hostnames, so the escaped form must never reach the container hostname
    orchestrator.select_and_reserve.return_value = "alpha & west_context.example"
    settings = {
        "docker_image": "desktop:latest",
        "resolution": "1920x1080",
        "shm_size": "256m",
        "memory_limit": "2g",
        "cpu_limit": 1,
        "initial_duration": 3600,
        "extension_duration": 1800,
        "max_extensions": 2,
        "rd_network_name": "bridge",
        "ssh_enabled": True,
        "web_terminal_enabled": True,
    }
    user = MagicMock(id=7, name="alice", email="alice@example.com")
    events = MagicMock()
    events.log_event.side_effect = RuntimeError("telemetry backend unavailable")
    mock_db = MagicMock()

    with (
        patch("container_manager._display_name", return_value=(user, "alice")),
        patch("container_manager._mint_session_cookie", return_value=None),
        patch.object(manager, "_resolve_username", return_value="alice"),
        patch.object(manager, "_get_setting", side_effect=lambda key: settings[key]),
        patch.object(manager, "wait_for_vnc_ready", return_value=True),
        patch.object(manager, "_read_resolved_username", return_value="alice"),
        patch("container_manager.DesktopContainerInfoModel") as row_model,
        patch("container_manager.db", mock_db),
        patch("container_manager.event_logger", events),
    ):
        manager._create_container_background(user_id=7, container_url="http://ctfd", extra_hosts=None)

    row_model.assert_called_once()
    mock_db.session.commit.assert_called_once()
    assert manager.creation_status[7]["status"] == "ready"
    host_manager.stop_container.assert_not_called()
    host_manager.force_remove_container.assert_not_called()
    orchestrator.release_slot.assert_not_called()
    host_manager.release_semaphore.assert_called_once_with(semaphore_token)
    orchestrator.select_and_reserve.assert_called_once_with()
    run_kwargs = host_manager.run_container.call_args.kwargs
    assert run_kwargs["context_name"] == "alpha & west_context.example"
    assert run_kwargs["hostname"] == run_kwargs["name"]
    assert run_kwargs["hostname"].startswith("rd-session-7-")
    assert "&amp;" not in run_kwargs["hostname"]


def test_default_readiness_budget_covers_delayed_image_startup():
    manager = ContainerManager(MagicMock(), MagicMock())
    settings = {
        "vnc_ready_attempts": SETTING_SPECS["vnc_ready_attempts"].default,
        "http_request_timeout": SETTING_SPECS["http_request_timeout"].default,
    }
    response = MagicMock()
    response.__enter__.return_value.status = 200

    # 360 refusals at the production 0.5s interval is three minutes, the budget for serial image startup gates
    delayed_start = [urllib.error.URLError("not listening yet") for _ in range(360)]
    with (
        patch.object(manager, "_get_setting", side_effect=lambda key: settings[key]),
        patch("urllib.request.build_opener") as build_opener,
        patch("container_manager.time.sleep") as sleep,
    ):
        opener = build_opener.return_value
        opener.open.side_effect = [*delayed_start, response]
        assert manager.wait_for_vnc_ready("runner.example", 32000) is True

    assert opener.open.call_count == 361
    assert sleep.call_count == 360


def test_destroy_acquires_local_status_lock_before_database_row_locks():
    """destroy takes the local lock before the row lock, the reverse order deadlocks against create"""
    host_manager = MagicMock()
    orchestrator = MagicMock()
    manager = ContainerManager(host_manager, orchestrator)
    manager.creation_status[7] = {"status": "queued", "message": "Queued..."}
    events: list[str] = []
    row_lock_held = False

    class OrderingGuard:
        def __enter__(self):
            events.append("local_lock")
            assert not row_lock_held, "process-local lock acquired after database row lock"

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

    operation = SimpleNamespace(
        user_id=7,
        state="queued",
        cancel_requested=False,
        updated_at=0.0,
    )

    def lock_operation(_user_id, create=False):
        nonlocal row_lock_held
        assert create is True
        events.append("operation_row_lock")
        row_lock_held = True
        return operation

    def commit():
        nonlocal row_lock_held
        row_lock_held = False

    manager.lock = OrderingGuard()  # type: ignore[assignment]
    mock_db = MagicMock()
    mock_db.session.commit.side_effect = commit

    with (
        patch("container_manager._display_name", return_value=(None, "alice")),
        patch.object(manager, "_locked_operation", side_effect=lock_operation),
        patch.object(manager, "_locked_active_row", return_value=None),
        patch("container_manager.db", mock_db),
    ):
        result = manager.destroy_container(7)

    assert result == {"success": True, "status": "cancelling"}
    assert events == ["local_lock", "operation_row_lock"]
    assert manager.creation_status[7] == {"status": "cancelled"}
    assert operation.cancel_requested is True
    assert row_lock_held is False
