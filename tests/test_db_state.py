import threading
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

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    with patch("container_manager.DesktopContainerInfoModel", mock_model):
        info = cm.get_container_info(1)

    assert info["container_id"] == "abc123"
    assert info["container_name"] == "kali-desktop-1-1234"
    assert info["vnc_url"] == "http://host1.example.com:6080/vnc.html"
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

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="alice")

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.DesktopSessionHistoryModel", MagicMock()),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("models.get_setting", return_value=False),
    ):
        result = cm.destroy_container(1)

    assert result["success"]
    mock_db.session.delete.assert_called_once_with(row)
    cm.host_manager.stop_container.assert_called_once_with("ctx1", "kali-desktop-1-1234")
    cm.orchestrator.release_slot.assert_called_once_with("ctx1")


def test_destroy_no_container(container_manager):
    cm = container_manager

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = None

    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="alice")

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
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="alice")

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

    row2 = MagicMock()
    row2.user_id = 2
    row2.docker_context = "ctx2"
    row2.container_name = "kali-desktop-2-1234"
    row2.created_at = 1100.0
    row2.extensions_used = 1

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
        patch("container_manager.DesktopSessionHistoryModel", mock_history_cls),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 2000.0
        killed = cm.destroy_all_containers_admin(admin_user)

    assert killed == 2
    assert cm.host_manager.stop_container.call_count == 2
    assert cm.orchestrator.release_slot.call_count == 2


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
        killed = cm.destroy_all_containers_admin(admin_user)

    assert killed == 0
    mock_event_logger.log_event.assert_called_once()
    args, kwargs = mock_event_logger.log_event.call_args
    assert args[0] == "admin_action"
    assert kwargs["metadata"] == {"killed_count": 0}
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
    row.extensions_used = 0

    mock_model = MagicMock()
    # first call: periodic_cleanup queries timer_started=True
    mock_model.query.filter_by.return_value.all.return_value = [row]
    # subsequent calls from destroy_container query by user_id
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="bob")

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.DesktopSessionHistoryModel", MagicMock()),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 1700.0  # 700s elapsed, 600s duration, expired
        cm.periodic_cleanup()

    cm.host_manager.stop_container.assert_called_once()


def test_log_offsets_concurrent_get_and_pop(container_manager):
    cm = container_manager

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.count.return_value = 0

    errors: list[BaseException] = []
    iterations = 200
    container_ids = [f"c{i}" for i in range(50)]

    with patch("container_manager.CommandLogModel", mock_model):

        def hammer_get():
            try:
                for _ in range(iterations):
                    for cid in container_ids:
                        cm._get_log_offset(cid)
            except BaseException as e:
                errors.append(e)

        def hammer_pop():
            try:
                for _ in range(iterations):
                    for cid in container_ids:
                        with cm._log_offsets_lock:
                            cm._log_offsets.pop(cid, None)
            except BaseException as e:
                errors.append(e)

        def hammer_advance():
            try:
                for _ in range(iterations):
                    for cid in container_ids:
                        with cm._log_offsets_lock:
                            cm._log_offsets[cid] = cm._log_offsets.get(cid, 0) + 1
            except BaseException as e:
                errors.append(e)

        threads = [
            threading.Thread(target=hammer_get),
            threading.Thread(target=hammer_get),
            threading.Thread(target=hammer_pop),
            threading.Thread(target=hammer_advance),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert errors == []
    # final state must only contain non-negative ints, no half-written objects
    for k, v in cm._log_offsets.items():
        assert isinstance(k, str)
        assert isinstance(v, int)
        assert v >= 0
