"""Focused regressions for cross-worker/session operations stability."""

from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from docker_host_manager import DockerHostManager
from models import LIFECYCLE_HELD


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
        # A crash/retry at the same instant uses the updated file mtime and
        # therefore does not credit the same 500 seconds twice.
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
