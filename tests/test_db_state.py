from types import SimpleNamespace
from unittest.mock import patch, MagicMock


def test_get_container_info_from_db(container_manager):
    cm = container_manager

    row = MagicMock()
    row.container_id = "abc123"
    row.user_id = 1
    row.container_name = "kali-desktop-1-1234"
    row.vnc_port = 5900
    row.novnc_port = 6080
    row.docker_context = "ctx1"
    row.pub_hostname = "host1.example.com"
    row.vnc_password = "secret"
    row.vnc_url = "http://host1.example.com:6080/vnc.html"
    row.created_at = 1700000000.0
    row.timer_started = False
    row.paused_at = None

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    with patch("container_manager.DesktopContainerInfoModel", mock_model):
        info = cm.get_container_info(1)

    assert info["container_id"] == "abc123"
    assert info["container_name"] == "kali-desktop-1-1234"
    assert (
        info["vnc_url"]
        == "/remote-desktop/vnc/1/vnc.html?autoconnect=true&resize=remote&reconnect=true#password=secret"
    )
    assert info["docker_context"] == "ctx1"


def test_get_container_info_none(container_manager):
    cm = container_manager

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = None

    with patch("container_manager.DesktopContainerInfoModel", mock_model):
        info = cm.get_container_info(1)

    assert info is None


def test_destroy_deletes_db_row(container_manager):
    cm = container_manager

    row = MagicMock()
    row.docker_context = "ctx1"
    row.container_name = "kali-desktop-1-1234"
    row.user_id = 1
    row.created_at = 1000.0
    row.extensions_used = 0
    row.paused_at = None

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=1)

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("models.DesktopSessionHistoryModel", MagicMock()),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("models.get_setting", return_value=False),
    ):
        result = cm.destroy_container(1)

    assert result["success"]
    mock_db.session.delete.assert_called_once_with(row)
    cm.host_manager.stop_container.assert_called_once_with("ctx1", str(row.container_id))
    cm.orchestrator.release_active_slot_in_transaction.assert_called_once()
    cm.orchestrator.release_slot.assert_not_called()


def test_destroy_no_container(container_manager):
    cm = container_manager

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = None

    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=1)

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
    ):
        result = cm.destroy_container(1)

    assert not result["success"]
    assert "No active container" in result["error"]


def test_create_rejects_existing_session(container_manager):
    cm = container_manager

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = MagicMock()

    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=1)

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
    ):
        result = cm.create_container(1, "http://test/", None)

    assert not result["success"]
    assert "already exists" in result["error"]


def test_destroy_all_containers_admin(container_manager):
    cm = container_manager

    row1 = MagicMock()
    row1.user_id = 1
    row1.docker_context = "ctx1"
    row1.container_name = "kali-desktop-1-1234"
    row1.created_at = 1000.0
    row1.extensions_used = 0
    row1.paused_at = None

    row2 = MagicMock()
    row2.user_id = 2
    row2.docker_context = "ctx2"
    row2.container_name = "kali-desktop-2-1234"
    row2.created_at = 1100.0
    row2.extensions_used = 1
    row2.paused_at = None

    mock_model = MagicMock()
    mock_model.query.all.return_value = [row1, row2]
    mock_model.query.filter_by.return_value.first.side_effect = [row1, row2]

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="admin")
    mock_history_cls = MagicMock()

    admin_user = MagicMock()
    admin_user.name = "admin"
    admin_user.id = 99

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("models.DesktopSessionHistoryModel", mock_history_cls),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 2000.0
        summary = cm.destroy_all_containers_admin(admin_user)

    assert summary == {"requested": 2, "completed": 2, "cancelling": 0, "stopping": 0, "failed": 0}
    assert cm.host_manager.stop_container.call_count == 2
    assert cm.orchestrator.release_active_slot_in_transaction.call_count == 2
    cm.orchestrator.release_slot.assert_not_called()


def test_destroy_all_containers_admin_empty_fleet_still_logs(container_manager):
    cm = container_manager

    mock_model = MagicMock()
    mock_model.query.all.return_value = []

    admin_user = MagicMock()
    admin_user.name = "admin"
    admin_user.id = 99

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.event_logger") as mock_event_logger,
    ):
        summary = cm.destroy_all_containers_admin(admin_user)

    assert summary == {"requested": 0, "completed": 0, "cancelling": 0, "stopping": 0, "failed": 0}
    mock_event_logger.log_event.assert_called_once()
    args, kwargs = mock_event_logger.log_event.call_args
    assert args[0] == "admin_action"
    assert kwargs["metadata"] == {"killed_count": 0, **summary}
    assert kwargs["user_id"] == 99


def test_periodic_cleanup_destroys_expired(container_manager):
    cm = container_manager

    row = MagicMock()
    row.user_id = 42
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 600
    row.docker_context = "ctx1"
    row.container_name = "kali-desktop-42-1234"
    row.created_at = 1000.0
    row.paused_at = None
    row.extensions_used = 0

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.all.return_value = [row]  # periodic_cleanup queries by timer_started
    mock_model.query.filter_by.return_value.first.return_value = row  # destroy_container then queries by user_id

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="bob")

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("models.DesktopSessionHistoryModel", MagicMock()),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 1700.0  # 700s elapsed, 600s duration, expired
        cm.periodic_cleanup()

    cm.host_manager.stop_container.assert_called_once()
