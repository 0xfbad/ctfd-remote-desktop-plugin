from unittest.mock import patch, MagicMock


def make_container_row(user_id=1, created_at=1000.0, extensions_used=2):
    row = MagicMock()
    row.user_id = user_id
    row.docker_context = "ctx1"
    row.container_name = f"kali-desktop-{user_id}-1234"
    row.created_at = created_at
    row.extensions_used = extensions_used
    return row


def test_destroy_records_history(container_manager):
    cm = container_manager
    row = make_container_row(user_id=1, created_at=1000.0, extensions_used=2)

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="alice")

    mock_history_cls = MagicMock()

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.DesktopSessionHistoryModel", mock_history_cls),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("container_manager.time") as mock_time,
        patch("models.get_setting", return_value=False),
    ):
        mock_time.time.return_value = 2000.0
        result = cm.destroy_container(1, reason="user_destroyed")

    assert result["success"]
    mock_history_cls.assert_called_once()
    history_kwargs = mock_history_cls.call_args[1]
    assert history_kwargs["user_id"] == 1
    assert history_kwargs["docker_context"] == "ctx1"
    assert history_kwargs["started_at"] == 1000.0
    assert history_kwargs["ended_at"] == 2000.0
    assert history_kwargs["duration"] == 1000.0
    assert history_kwargs["end_reason"] == "user_destroyed"
    assert history_kwargs["extensions_used"] == 2
    mock_db.session.add.assert_any_call(mock_history_cls.return_value)


def test_destroy_expired_records_history(container_manager):
    cm = container_manager
    row = make_container_row(user_id=42)
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 600

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.all.return_value = [row]
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="bob")
    mock_history_cls = MagicMock()

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.DesktopSessionHistoryModel", mock_history_cls),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 1700.0
        cm.periodic_cleanup()

    mock_history_cls.assert_called_once()
    assert mock_history_cls.call_args[1]["end_reason"] == "expired"


def test_destroy_all_records_history(container_manager):
    cm = container_manager
    row1 = make_container_row(user_id=1, created_at=1000.0)
    row2 = make_container_row(user_id=2, created_at=1100.0)

    mock_model = MagicMock()
    # destroy_all queries all rows, then destroy_container queries by user_id
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
    assert mock_history_cls.call_count == 2
    for c in mock_history_cls.call_args_list:
        assert c[1]["end_reason"] == "admin_killed"


def test_history_duration_calculation(container_manager):
    cm = container_manager
    row = make_container_row(user_id=5, created_at=1500.0)

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="eve")
    mock_history_cls = MagicMock()

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.DesktopSessionHistoryModel", mock_history_cls),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("container_manager.time") as mock_time,
        patch("models.get_setting", return_value=False),
    ):
        mock_time.time.return_value = 5000.0
        cm.destroy_container(5)

    kwargs = mock_history_cls.call_args[1]
    assert kwargs["duration"] == 3500.0
    assert kwargs["ended_at"] - kwargs["started_at"] == kwargs["duration"]
