import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import models
import settings
from routes import create_routes


def _handler(bp, name):
    for call in reversed(bp.route.return_value.call_args_list):
        candidate = call.args[0]
        if getattr(candidate, "__name__", None) == name:
            return candidate
    raise LookupError(name)


def _put(payload, *, effective=None):
    bp = create_routes(MagicMock(), MagicMock())
    handler = _handler(bp, "admin_update_settings")
    with (
        patch("routes.request") as request,
        patch("routes.jsonify", side_effect=lambda value: value),
        patch("models.get_all_settings", return_value=effective or dict(settings.SETTING_DEFAULTS)),
        patch("models.set_settings") as set_settings,
    ):
        request.json = payload
        return handler(), set_settings


def _stored_rows(*extra_rows):
    revision = SimpleNamespace(key="_settings_revision", value="1")
    rows = [revision, *extra_rows]
    return revision, rows


def _mock_settings_query(rows, revision):
    query = MagicMock()
    query.filter_by.return_value.first.return_value = revision
    query.filter_by.return_value.with_for_update.return_value.first.return_value = revision
    query.order_by.return_value.all.return_value = rows
    query.all.return_value = rows
    return query


def test_registry_is_the_complete_source_of_public_defaults():
    assert set(settings.SETTING_SPECS) == settings.PUBLIC_SETTING_KEYS | settings.INTERNAL_SETTING_KEYS
    assert set(settings.SETTING_DEFAULTS) == settings.PUBLIC_SETTING_KEYS
    assert settings.INTERNAL_SETTING_KEYS == {"image_cache", "_settings_revision"}
    assert len(settings.PUBLIC_SETTING_KEYS) == 33


def test_default_profile_fully_validates():
    assert settings.validate_effective_settings(dict(settings.SETTING_DEFAULTS)) == settings.SETTING_DEFAULTS


def test_fresh_defaults_require_only_local_docker_primitives():
    assert settings.SETTING_DEFAULTS["rd_network_name"] == "bridge"
    assert settings.SETTING_DEFAULTS["storage_limit"] == ""
    assert settings.SETTING_DEFAULTS["cgroup_parent"] == ""


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "empty"),
        ({"unknown": 1}, "unknown"),
        ({"image_cache": "{}"}, "internal"),
        ({"pids_limit": None}, "null"),
        ({"pids_limit": True}, "integer"),
        ({"pids_limit": 2.0}, "integer"),
        ({"require_verified": 1}, "boolean"),
        ({"require_verified": "true"}, "boolean"),
        ({"cpu_limit": True}, "finite number"),
        ({"cpu_limit": math.nan}, "finite number"),
        ({"cpu_limit": math.inf}, "finite number"),
        ({"docker_image": ["image"]}, "string"),
        ({"docker_image": "bad\nimage"}, "control"),
        ({"storage_limit": " 20g"}, "whitespace"),
        ({"docker_image": "x" * 513}, "character limit"),
    ],
)
def test_api_rejects_unknown_internal_null_and_wrong_json_types(payload, message):
    result, set_settings = _put(payload)
    body, status = result
    assert status == 400
    assert message in body["error"]
    set_settings.assert_not_called()


def test_api_accepts_fractional_cpu_as_a_json_number():
    result, set_settings = _put({"cpu_limit": 0.5})
    assert result == {"success": True}
    set_settings.assert_called_once_with({"cpu_limit": 0.5})


def test_canonical_persisted_encodings_round_trip():
    assert settings.decode_stored_setting("require_verified", "true") is True
    assert settings.decode_stored_setting("require_verified", "false") is False
    assert settings.decode_stored_setting("pids_limit", "4096") == 4096
    assert settings.serialize_setting("require_verified", True) == "true"
    assert settings.serialize_setting("pids_limit", 4096) == "4096"
    assert settings.serialize_setting("cpu_limit", 2) == "2.0"
    assert settings.serialize_setting("storage_limit", "20G") == "20g"


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("require_verified", "yes"),
        ("require_verified", "1"),
        ("require_verified", "True"),
        ("pids_limit", "4096.0"),
        ("pids_limit", "2.5"),
        ("pids_limit", "nan"),
        ("cpu_limit", "nan"),
        ("cpu_limit", "inf"),
        ("cpu_limit", "2e0"),
    ],
)
def test_corrupt_persisted_scalars_fail_closed(key, raw):
    with pytest.raises(settings.SettingsValidationError):
        settings.decode_stored_setting(key, raw)


@pytest.mark.parametrize("key", sorted(settings.PUBLIC_SETTING_KEYS))
def test_every_registered_default_passes_its_scalar_validator(key):
    assert settings.validate_setting_value(key, settings.SETTING_DEFAULTS[key]) == settings.SETTING_DEFAULTS[key]


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"shm_size": "5g"}, "shm_size"),
        ({"memory_reservation": "5g"}, "memory_reservation"),
        ({"nofile_soft": 2048, "nofile_hard": 1024}, "nofile_soft"),
        ({"extension_duration": 0, "max_extensions": 1}, "extension_duration"),
        ({"initial_duration": 604800, "extension_duration": 604800, "max_extensions": 100}, "30 days"),
    ],
)
def test_cross_field_validation_rejects_unsafe_profiles(updates, message):
    profile = dict(settings.SETTING_DEFAULTS)
    profile.update(updates)
    with pytest.raises(settings.SettingsValidationError, match=message):
        settings.validate_effective_settings(profile)


def test_local_profile_keeps_generic_safety_validation():
    profile = dict(settings.SETTING_DEFAULTS)
    profile.update(
        {
            "storage_limit": "",
            "log_max_size": "",
            "memory_reservation": "",
            "swap_limit": "-1",
            "cgroup_parent": "",
        }
    )
    settings.validate_effective_settings(profile)

    profile["nofile_soft"] = 10000
    profile["nofile_hard"] = 100
    with pytest.raises(settings.SettingsValidationError, match="nofile_soft"):
        settings.validate_effective_settings(profile)


def test_set_settings_locks_revision_validates_merged_profile_and_serializes_atomically():
    cpu_row = SimpleNamespace(key="cpu_limit", value="2.0")
    revision, rows = _stored_rows(cpu_row)
    query = _mock_settings_query(rows, revision)

    with patch("models.DesktopSettingsModel") as model, patch.object(models.db.session, "commit") as commit:
        model.query = query
        models.set_settings({"cpu_limit": 0.5})

    query.filter_by.return_value.with_for_update.assert_called_once()
    assert cpu_row.value == "0.5"
    assert revision.value == "2"
    commit.assert_called_once()


def test_set_settings_rolls_back_entire_batch_before_mutating_on_profile_error():
    memory_row = SimpleNamespace(key="memory_limit", value="4g")
    revision, rows = _stored_rows(memory_row)
    query = _mock_settings_query(rows, revision)

    with (
        patch("models.DesktopSettingsModel") as model,
        patch.object(models.db.session, "commit") as commit,
        patch.object(models.db.session, "rollback") as rollback,
    ):
        model.query = query
        with pytest.raises(settings.SettingsValidationError, match="shm_size"):
            models.set_settings({"memory_limit": "256m"})

    assert memory_row.value == "4g"
    assert revision.value == "1"
    commit.assert_not_called()
    rollback.assert_called_once()


def test_initialize_settings_seeds_missing_defaults_and_canonicalizes_numbers():
    bool_row = SimpleNamespace(key="require_verified", value="true")
    cpu_row = SimpleNamespace(key="cpu_limit", value="2")
    revision, rows = _stored_rows(bool_row, cpu_row)
    query = _mock_settings_query(rows, revision)

    with (
        patch("models.DesktopSettingsModel") as model,
        patch("models.DesktopPluginMetadataModel") as metadata_model,
        patch.object(models.db.session, "add") as add,
        patch.object(models.db.session, "commit") as commit,
    ):
        model.query = query
        metadata_model.query.filter_by.return_value.first.return_value = None
        models.initialize_settings()

    assert bool_row.value == "true"
    assert cpu_row.value == "2.0"
    assert add.call_count >= len(settings.PUBLIC_SETTING_KEYS) - 2
    commit.assert_called_once()


def test_initialize_settings_migrates_only_exact_contract_one_defaults():
    cap_row = SimpleNamespace(
        key="cap_add",
        value="CHOWN,SETUID,SETGID,FOWNER,DAC_OVERRIDE,NET_RAW,NET_BIND_SERVICE,AUDIT_WRITE",
    )
    readiness_row = SimpleNamespace(key="vnc_ready_attempts", value="180")
    revision, rows = _stored_rows(cap_row, readiness_row)
    query = _mock_settings_query(rows, revision)

    with (
        patch("models.DesktopSettingsModel") as model,
        patch("models.DesktopPluginMetadataModel") as metadata_model,
        patch.object(models.db.session, "add") as add,
    ):
        model.query = query
        metadata_model.query.filter_by.return_value.first.return_value = None
        models.initialize_settings()

    assert cap_row.value == settings.SETTING_DEFAULTS["cap_add"]
    assert readiness_row.value == str(settings.SETTING_DEFAULTS["vnc_ready_attempts"])
    assert revision.value == "2"
    assert any(
        call.kwargs == {"key": "settings_schema_version", "value": "2"} for call in metadata_model.call_args_list
    )
    assert add.called


def test_initialize_settings_preserves_custom_legacy_profile_values():
    cap_row = SimpleNamespace(key="cap_add", value="NET_RAW")
    readiness_row = SimpleNamespace(key="vnc_ready_attempts", value="240")
    revision, rows = _stored_rows(cap_row, readiness_row)
    query = _mock_settings_query(rows, revision)

    with (
        patch("models.DesktopSettingsModel") as model,
        patch("models.DesktopPluginMetadataModel") as metadata_model,
    ):
        model.query = query
        metadata_model.query.filter_by.return_value.first.return_value = None
        models.initialize_settings()

    assert cap_row.value == "NET_RAW"
    assert readiness_row.value == "240"
    assert revision.value == "1"


def test_unknown_persisted_row_fails_startup_normalization():
    unknown = SimpleNamespace(key="typo_setting", value="1")
    revision, rows = _stored_rows(unknown)
    query = _mock_settings_query(rows, revision)

    with (
        patch("models.DesktopSettingsModel") as model,
        patch.object(models.db.session, "commit") as commit,
        patch.object(models.db.session, "rollback") as rollback,
    ):
        model.query = query
        with pytest.raises(settings.SettingsValidationError, match="unknown persisted"):
            models.initialize_settings()

    commit.assert_not_called()
    rollback.assert_called_once()


def test_admission_fails_closed_when_stored_settings_are_corrupt():
    container_manager = MagicMock()
    bp = create_routes(container_manager, MagicMock())
    handler = _handler(bp, "create_session")

    with (
        patch("routes.jsonify", side_effect=lambda value: value),
        patch("models.get_all_settings", side_effect=settings.SettingsValidationError("bad pids_limit")),
    ):
        body, status = handler.__wrapped__()

    assert status == 503
    assert "settings are invalid" in body["error"]
    container_manager.create_container.assert_not_called()


def test_restart_required_intervals_are_exact_and_returned_by_api():
    assert settings.RESTART_REQUIRED_SETTINGS == {
        "cleanup_interval",
        "pause_watch_interval",
    }
    updates = {"cleanup_interval": 120}
    result, set_settings = _put(updates)
    assert result == {"success": True, "restart_required": ["cleanup_interval"]}
    set_settings.assert_called_once_with(updates)


def test_frontend_serializes_numeric_settings_and_marks_restart_requirement():
    template = (Path(__file__).resolve().parent.parent / "src" / "templates" / "remote_desktop_config.html").read_text()
    assert "rdIntegerSettingKeys" in template
    assert "rdFloatSettingKeys" in template
    assert "Number.parseInt(el.value, 10)" in template
    assert "Number.parseFloat(el.value)" in template
    assert "Restart required" in template
    for exposed_setting in ("rd_network_name", "storage_limit", "cgroup_parent"):
        assert f"rd-setting-{exposed_setting}" in template
