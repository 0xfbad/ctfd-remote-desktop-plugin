from unittest.mock import patch, MagicMock
from container_manager import ContainerManager


def make_manager():
    cm = ContainerManager(MagicMock(), MagicMock())
    return cm


def test_get_container_info_from_db():
    cm = make_manager()

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

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    with patch("container_manager.DesktopContainerInfoModel", mock_model):
        info = cm.get_container_info(1)

    assert info["container_id"] == "abc123"
    assert info["container_name"] == "kali-desktop-1-1234"
    assert info["vnc_url"] == "http://host1.example.com:6080/vnc.html"
    assert info["docker_context"] == "ctx1"


def test_get_container_info_none():
    cm = make_manager()

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = None

    with patch("container_manager.DesktopContainerInfoModel", mock_model):
        info = cm.get_container_info(1)

    assert info is None


def test_destroy_deletes_db_row():
    cm = make_manager()

    row = MagicMock()
    row.docker_context = "ctx1"
    row.container_name = "kali-desktop-1-1234"
    row.user_id = 1

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="alice")

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
    ):
        result = cm.destroy_container(1)

    assert result["success"]
    mock_db.session.delete.assert_called_once_with(row)
    mock_db.session.commit.assert_called_once()
    cm.host_manager.stop_container.assert_called_once_with("ctx1", "kali-desktop-1-1234")
    cm.orchestrator.release_slot.assert_called_once_with("ctx1")


def test_destroy_no_container():
    cm = make_manager()

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


def test_create_rejects_existing_session():
    cm = make_manager()

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = MagicMock()

    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="alice")

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
    ):
        result = cm.create_container(1)

    assert not result["success"]
    assert "already exists" in result["error"]


def test_periodic_cleanup_destroys_expired():
    cm = make_manager()

    row = MagicMock()
    row.user_id = 42
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 600
    row.docker_context = "ctx1"
    row.container_name = "kali-desktop-42-1234"

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
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 1700.0  # 700s elapsed, 600s duration -> expired
        cm.periodic_cleanup()

    cm.host_manager.stop_container.assert_called_once()
