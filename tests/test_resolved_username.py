"""Authoritative container username handoff."""

from unittest.mock import MagicMock, patch

import pytest

from container_manager import ContainerManager


def _manager(result):
    host_manager = MagicMock()
    host_manager.exec_in_container.return_value = result
    return ContainerManager(host_manager, MagicMock()), host_manager


def test_reads_valid_collision_safe_username():
    manager, host_manager = _manager((0, "student_tcpdump\n"))

    assert manager._read_resolved_username("alpha", "rd-session-1") == "student_tcpdump"
    host_manager.exec_in_container.assert_called_once_with(
        "alpha",
        "rd-session-1",
        [
            "/bin/bash",
            "-c",
            "/usr/local/bin/remote-desktop-healthcheck && cat -- /var/lib/remote-desktop/resolved-username",
        ],
    )


@pytest.mark.parametrize(
    "result",
    [
        (1, ""),
        (0, ""),
        (0, "Root\n"),
        (0, "1student\n"),
        (0, "student-name\n"),
        (0, "a" * 33 + "\n"),
        (0, "alice\r\n"),
        (0, "alice\n\n"),
        (0, b"alice\xff\n"),
    ],
)
def test_rejects_missing_or_invalid_username(result):
    manager, _host_manager = _manager(result)

    with pytest.raises(RuntimeError, match="Linux username"):
        manager._read_resolved_username("alpha", "rd-session-1")


def test_exec_failure_is_a_clear_image_contract_error():
    manager, host_manager = _manager((0, "alice\n"))
    host_manager.exec_in_container.side_effect = OSError("transport reset")

    with (
        patch("container_manager.time.sleep") as sleep,
        pytest.raises(RuntimeError, match="did not publish"),
    ):
        manager._read_resolved_username("alpha", "rd-session-1")

    assert host_manager.exec_in_container.call_count == 3
    assert sleep.call_count == 2


def test_transient_exec_results_retry_then_succeed():
    manager, host_manager = _manager((0, "unused\n"))
    host_manager.exec_in_container.side_effect = [
        (-1, ""),
        OSError("transport reset"),
        (0, "student_tcpdump\n"),
    ]

    with patch("container_manager.time.sleep") as sleep:
        assert manager._read_resolved_username("alpha", "rd-session-1") == "student_tcpdump"

    assert host_manager.exec_in_container.call_count == 3
    assert sleep.call_count == 2


@pytest.mark.parametrize("first_result", [(2, "health failed"), (0, "Not-A-User\n")])
def test_nontransient_or_structurally_invalid_results_fail_without_retry(first_result):
    manager, host_manager = _manager((0, "unused\n"))
    host_manager.exec_in_container.side_effect = [first_result, (0, "alice\n")]

    with (
        patch("container_manager.time.sleep") as sleep,
        pytest.raises(RuntimeError, match="Linux username"),
    ):
        manager._read_resolved_username("alpha", "rd-session-1")

    host_manager.exec_in_container.assert_called_once()
    sleep.assert_not_called()
