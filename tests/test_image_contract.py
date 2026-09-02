"""The scheduler must reject images that predate the plugin/image contract."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import docker
import pytest

from docker_host_manager import (
    DockerHostManager,
    IMAGE_CONTRACT_LABEL,
    IMAGE_CONTRACT_VERSION,
)


def _get_handler(bp, name):
    for call in reversed(bp.route.return_value.call_args_list):
        fn = call.args[0]
        if getattr(fn, "__name__", None) == name:
            return fn
    raise LookupError(name)


def _image(contract: str | None):
    labels = {} if contract is None else {IMAGE_CONTRACT_LABEL: contract}
    image = MagicMock()
    image.attrs = {
        "Config": {"Labels": labels},
        "Created": "2026-08-31T12:34:56Z",
        "Size": 100 * 1024 * 1024,
    }
    image.short_id = "sha256:0123456789ab"
    image.id = "sha256:0123456789abcdef"
    return image


def _run_container(
    manager: DockerHostManager,
    client: MagicMock,
    *,
    container: MagicMock | None = None,
    ports: list[str] | None = None,
) -> None:
    from settings import SETTING_DEFAULTS

    settings = dict(SETTING_DEFAULTS)
    settings.update(
        {
            "storage_limit": "",
            "log_max_size": "",
            "memory_reservation": "",
            "oom_score_adj": 0,
            "nofile_soft": 0,
            "nofile_hard": 0,
            "cgroup_parent": "",
        }
    )
    if container is None:
        container = MagicMock(id="container-id")
        container.attrs = {
            "NetworkSettings": {"Ports": {"6080/tcp": [{"HostPort": "40001"}]}},
        }
    client.containers.run.return_value = container

    with (
        patch.object(manager, "_get_client", return_value=client),
        patch("models.get_all_settings", return_value=settings),
    ):
        manager.run_container(
            context_name="alpha",
            image="desktop:latest",
            name="rd-session-7-contract",
            env={},
            ports=ports or ["6080/tcp"],
        )


def test_check_image_accepts_matching_runtime_contract():
    manager = DockerHostManager()
    client = MagicMock()
    client.images.get.return_value = _image(IMAGE_CONTRACT_VERSION)

    with patch.object(manager, "_get_client", return_value=client):
        assert manager.check_image("alpha", "desktop:latest") is True


@pytest.mark.parametrize("contract", [None, "1", "2"])
def test_check_image_rejects_missing_or_mismatched_runtime_contract(contract, caplog):
    manager = DockerHostManager()
    client = MagicMock()
    client.images.get.return_value = _image(contract)

    with (
        patch.object(manager, "_get_client", return_value=client),
        caplog.at_level(logging.WARNING, logger="_rd_plugin.docker_host_manager"),
    ):
        assert manager.check_image("alpha", "desktop:latest") is False

    assert IMAGE_CONTRACT_LABEL in caplog.text or "remote-desktop contract" in caplog.text
    assert IMAGE_CONTRACT_VERSION in caplog.text


@pytest.mark.parametrize(
    "contract,expected_status",
    [(IMAGE_CONTRACT_VERSION, "compatible"), (None, "incompatible"), ("1", "incompatible")],
)
def test_get_image_info_reports_contract_compatibility(contract, expected_status):
    manager = DockerHostManager()
    client = MagicMock()
    client.images.get.return_value = _image(contract)

    with patch.object(manager, "_get_client", return_value=client):
        info = manager.get_image_info("alpha", "desktop:latest")

    assert info is not None
    assert info["contract"] == (contract if contract is not None else "missing")
    assert info["contract_status"] == expected_status


def test_run_container_pins_tag_to_resolved_immutable_image_id():
    manager = DockerHostManager()
    client = MagicMock()
    client.images.get.return_value = _image(IMAGE_CONTRACT_VERSION)

    _run_container(manager, client)

    client.images.get.assert_called_once_with("desktop:latest")
    assert client.containers.run.call_args.args == ("sha256:0123456789abcdef",)


@pytest.mark.parametrize("contract", [None, "1", "2"])
def test_run_container_rejects_incompatible_image_before_create(contract):
    manager = DockerHostManager()
    client = MagicMock()
    client.images.get.return_value = _image(contract)

    with pytest.raises(
        docker.errors.DockerException,
        match=rf"expected '{IMAGE_CONTRACT_VERSION}'",
    ):
        _run_container(manager, client)

    client.containers.run.assert_not_called()


@pytest.mark.parametrize("failure_site", ["reload", "attrs"])
def test_run_container_invalidates_client_when_post_create_inspection_fails(failure_site):
    manager = DockerHostManager()
    client = MagicMock()
    client.images.get.return_value = _image(IMAGE_CONTRACT_VERSION)
    container = MagicMock(id="container-id")
    if failure_site == "reload":
        container.reload.side_effect = docker.errors.DockerException("transport lost")
    else:
        container.attrs = MagicMock()
        container.attrs.get.side_effect = docker.errors.DockerException("transport lost")

    with (
        patch.object(manager, "_clear_client") as clear_client,
        pytest.raises(docker.errors.DockerException, match="transport lost"),
    ):
        _run_container(manager, client, container=container)

    clear_client.assert_called_once_with("alpha")


def test_run_container_never_returns_partial_port_mapping():
    manager = DockerHostManager()
    client = MagicMock()
    client.images.get.return_value = _image(IMAGE_CONTRACT_VERSION)
    container = MagicMock(id="container-id")
    container.attrs = {
        "NetworkSettings": {"Ports": {"6080/tcp": [{"HostPort": "40001"}]}},
    }

    with (
        patch("_rd_plugin.docker_host_manager.time.sleep"),
        pytest.raises(docker.errors.DockerException, match="all port mappings"),
    ):
        _run_container(
            manager,
            client,
            container=container,
            ports=["6080/tcp", "5900/tcp"],
        )

    assert container.reload.call_count == 5


def test_admin_image_matrix_retains_incompatible_info_but_marks_it_unavailable():
    from routes import create_routes

    container_manager = MagicMock()
    container_manager.host_manager.get_connected_contexts.return_value = ["compatible", "legacy"]
    image_info = {
        "compatible": {"contract": IMAGE_CONTRACT_VERSION, "contract_status": "compatible"},
        "legacy": {"contract": "1", "contract_status": "incompatible"},
    }
    container_manager.host_manager.get_image_info.side_effect = lambda context, _image: image_info[context]
    handler = _get_handler(create_routes(container_manager, MagicMock()), "admin_images_matrix")

    with (
        patch("models.get_all_settings", return_value={"docker_image": "desktop:latest"}),
        patch("models.set_setting"),
        patch("routes.jsonify", side_effect=lambda **payload: payload),
    ):
        payload = handler()

    matrix = payload["matrix"]["desktop"]
    assert matrix["compatible"] == {"available": True, "info": image_info["compatible"]}
    assert matrix["legacy"] == {"available": False, "info": image_info["legacy"]}


def test_admin_image_matrix_replaces_stale_cache_when_no_context_is_connected():
    from routes import create_routes

    container_manager = MagicMock()
    container_manager.host_manager.get_connected_contexts.return_value = []
    handler = _get_handler(create_routes(container_manager, MagicMock()), "admin_images_matrix")

    with (
        patch("models.get_all_settings", return_value={"docker_image": "desktop:latest"}),
        patch("models.set_setting") as set_setting,
        patch("routes.jsonify", side_effect=lambda **payload: payload),
    ):
        payload = handler()

    assert payload == {"images": ["desktop"], "contexts": [], "matrix": {"desktop": {}}}
    cached = json.loads(set_setting.call_args.args[1])
    assert cached["contexts"] == []
    assert cached["matrix"] == {"desktop": {}}


def test_admin_image_matrix_distinguishes_incompatible_contract_from_missing():
    template = (Path(__file__).resolve().parent.parent / "src" / "templates" / "remote_desktop_config.html").read_text()

    assert "Incompatible image contract" in template
    assert "incompatible contract" in template
    assert "e.info.contract_status === 'compatible'" in template
    assert "entry.info.contract_status !== 'compatible'" in template
    assert "entry.available && !entry.info" in template
    assert "contract_status !== 'incompatible'" not in template


@pytest.mark.parametrize(
    ("image_info", "expected"),
    [
        (None, "image desktop:latest not found on context"),
        (
            {"contract": "1", "contract_status": "incompatible"},
            f"image desktop:latest has incompatible remote-desktop contract '1'; expected {IMAGE_CONTRACT_VERSION!r}",
        ),
    ],
)
def test_context_probe_distinguishes_missing_from_incompatible_image(image_info, expected):
    from routes import create_routes

    container_manager = MagicMock()
    container_manager.host_manager.ping.return_value = True
    container_manager.host_manager.get_image_info.return_value = image_info
    handler = _get_handler(create_routes(container_manager, MagicMock()), "admin_test_context")
    context = MagicMock(context_name="alpha")

    with (
        patch("models.DesktopDockerContextModel") as context_model,
        patch("models.get_setting", return_value="desktop:latest"),
        patch("routes.jsonify", side_effect=lambda payload: payload),
    ):
        context_model.query.get.return_value = context
        result = handler(7)

    assert result == ({"error": expected}, 503)
    container_manager.host_manager.check_image.assert_not_called()
