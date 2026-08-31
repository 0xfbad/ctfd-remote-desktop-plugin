import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _load_entrypoint():
    path = Path(__file__).resolve().parent.parent / "src" / "__init__.py"
    name = "_rd_plugin.plugin_entrypoint_for_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_reload_bus_callback_discards_long_lived_scoped_session():
    module = _load_entrypoint()
    orchestrator = MagicMock()
    callback = module._make_bus_callback(orchestrator)

    with patch("CTFd.models.db") as db:
        callback({"_control": "reload_contexts"})

    orchestrator.load_from_db.assert_called_once()
    db.session.remove.assert_called_once()


def test_bus_callback_discards_session_when_delivery_raises():
    module = _load_entrypoint()
    callback = module._make_bus_callback(MagicMock())

    with patch("CTFd.models.db") as db, patch.object(module.event_logger, "_deliver_local", side_effect=RuntimeError):
        try:
            callback({"type": "test"})
        except RuntimeError:
            pass

    db.session.remove.assert_called_once()


def test_bus_callback_marks_remote_delivery_non_persistent():
    module = _load_entrypoint()
    callback = module._make_bus_callback(MagicMock())

    with patch("CTFd.models.db") as db, patch.object(module.event_logger, "_deliver_local") as deliver:
        callback({"type": "test"})

    deliver.assert_called_once_with({"type": "test"}, persist=False)
    db.session.remove.assert_called_once()


def test_gunicorn_preload_is_rejected(monkeypatch):
    module = _load_entrypoint()
    monkeypatch.setenv("GUNICORN_CMD_ARGS", "--workers 5 --preload")

    try:
        module._reject_gunicorn_preload()
    except RuntimeError as exc:
        assert "preload" in str(exc).lower()
    else:
        raise AssertionError("Gunicorn preload was accepted")


def test_gunicorn_preload_check_matches_complete_option_only(monkeypatch):
    module = _load_entrypoint()
    monkeypatch.setattr(sys, "argv", ["gunicorn", "example:app", "--preloaded-assets=/static"])
    monkeypatch.setenv("GUNICORN_CMD_ARGS", '--name "worker --preload notes"')

    module._reject_gunicorn_preload()


def test_gunicorn_preload_check_rejects_quoted_environment_option(monkeypatch):
    module = _load_entrypoint()
    monkeypatch.setattr(sys, "argv", ["gunicorn", "example:app"])
    monkeypatch.setenv("GUNICORN_CMD_ARGS", '--workers 5 "--preload"')

    with pytest.raises(RuntimeError, match="preload"):
        module._reject_gunicorn_preload()


def test_gunicorn_config_file_preload_is_rejected_from_master_load_path(monkeypatch):
    module = _load_entrypoint()
    monkeypatch.setattr(sys, "argv", ["gunicorn", "-c", "gunicorn.conf.py", "example:app"])
    monkeypatch.delenv("GUNICORN_CMD_ARGS", raising=False)
    monkeypatch.setattr(module, "_gunicorn_master_preload_active", lambda: True)

    with pytest.raises(RuntimeError, match="preload"):
        module._reject_gunicorn_preload()


def test_gunicorn_master_stack_detector_reads_resolved_preload_setting():
    module = _load_entrypoint()
    frame = SimpleNamespace(
        f_globals={"__name__": "gunicorn.arbiter"},
        f_code=SimpleNamespace(co_name="setup"),
        f_locals={"self": SimpleNamespace(cfg=SimpleNamespace(preload_app=True))},
        f_back=None,
    )

    assert module._gunicorn_master_preload_active(frame) is True


def test_gunicorn_nonpreload_worker_load_path_is_not_rejected(monkeypatch):
    module = _load_entrypoint()
    monkeypatch.setattr(sys, "argv", ["gunicorn", "-c", "gunicorn.conf.py", "example:app"])
    monkeypatch.delenv("GUNICORN_CMD_ARGS", raising=False)
    monkeypatch.setattr(module, "_gunicorn_master_preload_active", lambda: False)

    module._reject_gunicorn_preload()


def test_preload_rejection_precedes_database_and_event_bus_initialization(monkeypatch):
    module = _load_entrypoint()
    app = MagicMock()
    monkeypatch.setenv("GUNICORN_CMD_ARGS", "--preload")
    module._prepare_database = MagicMock()
    module.event_bus = MagicMock()

    with pytest.raises(RuntimeError, match="preload"):
        module.load(app)

    module._prepare_database.assert_not_called()
    module.event_bus.init.assert_not_called()


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class _SchedulerLockConnection:
    def __init__(self, get_lock=1, owns_lock=1, execute_error_at=None):
        self.get_lock = get_lock
        self.owns_lock = owns_lock
        self.execute_error_at = execute_error_at
        self.closed = False
        self.invalidated = False
        self.invalidation_error = None
        self.statements = []

    def execute(self, statement, _params=None):
        sql = str(statement)
        self.statements.append(sql)
        if self.execute_error_at and self.execute_error_at in sql:
            raise RuntimeError("uncertain database result")
        if "SELECT DATABASE()" in sql:
            return _ScalarResult("ctfd")
        if "GET_LOCK" in sql:
            return _ScalarResult(self.get_lock)
        if "IS_USED_LOCK" in sql:
            return _ScalarResult(self.owns_lock)
        if "RELEASE_LOCK" in sql:
            return _ScalarResult(1)
        raise AssertionError(sql)

    def close(self):
        self.closed = True

    def invalidate(self, error=None):
        self.invalidated = True
        self.invalidation_error = error


def _scheduler_app(connection, database_url="mysql+pymysql://ctfd@db/ctfd"):
    engine = SimpleNamespace(connect=MagicMock(return_value=connection))
    return SimpleNamespace(config={"SQLALCHEMY_DATABASE_URI": database_url}, db=SimpleNamespace(engine=engine))


def test_mariadb_scheduler_leadership_is_connection_scoped_and_revalidated():
    module = _load_entrypoint()
    connection = _SchedulerLockConnection()
    app = _scheduler_app(connection)

    try:
        assert module._claim_scheduler_leader(app) is True
        assert module._claim_scheduler_leader(app) is True
        assert sum("GET_LOCK" in sql for sql in connection.statements) == 1
        assert sum("IS_USED_LOCK" in sql for sql in connection.statements) == 1
    finally:
        module._release_scheduler_leader()

    assert any("RELEASE_LOCK" in sql for sql in connection.statements)
    assert connection.closed is True


def test_mariadb_scheduler_follower_does_not_hold_a_pool_connection():
    module = _load_entrypoint()
    connection = _SchedulerLockConnection(get_lock=0)

    assert module._claim_scheduler_leader(_scheduler_app(connection)) is False
    assert connection.closed is True
    assert module._scheduler_lock_connection is None


def test_mariadb_uncertain_ownership_invalidates_connection_before_failover():
    module = _load_entrypoint()
    uncertain = _SchedulerLockConnection(execute_error_at="IS_USED_LOCK")
    successor = _SchedulerLockConnection()
    engine = SimpleNamespace(connect=MagicMock(return_value=successor))
    app = SimpleNamespace(
        config={"SQLALCHEMY_DATABASE_URI": "mysql+pymysql://ctfd@db/ctfd"},
        db=SimpleNamespace(engine=engine),
    )
    module._scheduler_lock_connection = uncertain
    module._scheduler_lock_name = "ctfd_remote_desktop.scheduler.test"

    try:
        assert module._claim_scheduler_leader(app) is True
        assert uncertain.invalidated is True
        assert isinstance(uncertain.invalidation_error, RuntimeError)
        assert uncertain.closed is True
        assert module._scheduler_lock_connection is successor
    finally:
        module._release_scheduler_leader()


def test_mariadb_uncertain_acquisition_invalidates_connection():
    module = _load_entrypoint()
    uncertain = _SchedulerLockConnection(execute_error_at="GET_LOCK")

    with pytest.raises(RuntimeError, match="uncertain database result"):
        module._claim_scheduler_leader(_scheduler_app(uncertain))

    assert uncertain.invalidated is True
    assert isinstance(uncertain.invalidation_error, RuntimeError)
    assert uncertain.closed is True
    assert module._scheduler_lock_connection is None


def test_mariadb_uncertain_release_invalidates_connection():
    module = _load_entrypoint()
    uncertain = _SchedulerLockConnection(execute_error_at="RELEASE_LOCK")
    module._scheduler_lock_connection = uncertain
    module._scheduler_lock_name = "ctfd_remote_desktop.scheduler.test"

    module._release_scheduler_leader()

    assert uncertain.invalidated is True
    assert isinstance(uncertain.invalidation_error, RuntimeError)
    assert uncertain.closed is True
    assert module._scheduler_lock_connection is None


def test_sqlite_scheduler_lock_fails_over_between_process_contenders(tmp_path):
    first = _load_entrypoint()
    second = _load_entrypoint()
    database_path = tmp_path / "ctfd.db"

    def app():
        engine = SimpleNamespace(url=SimpleNamespace(database=str(database_path)))
        return SimpleNamespace(
            config={"SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path}"},
            db=SimpleNamespace(engine=engine),
        )

    try:
        assert first._claim_scheduler_leader(app()) is True
        assert second._claim_scheduler_leader(app()) is False
        first._release_scheduler_leader()
        assert second._claim_scheduler_leader(app()) is True
    finally:
        first._release_scheduler_leader()
        second._release_scheduler_leader()


def test_scheduler_rejects_database_without_cross_replica_locking():
    module = _load_entrypoint()

    with pytest.raises(RuntimeError, match="MariaDB/MySQL or SQLite"):
        module._claim_scheduler_leader(_scheduler_app(MagicMock(), "postgresql://ctfd@db/ctfd"))


def test_entrypoint_does_not_override_process_signals_or_teardown_sessions():
    source = (Path(__file__).resolve().parent.parent / "src" / "__init__.py").read_text()
    load_source = source[source.index("def load(app: Flask)") :]

    assert "signal.signal(" not in source
    assert "cleanup_all_containers()" not in source
    assert load_source.index("_reject_gunicorn_preload()") < load_source.index("_prepare_database(app)")
    assert "REMOTE_DESKTOP_SCHEDULER_ROLE" not in source


def test_startup_reconciliation_delegates_capacity_to_reservation_aware_audit():
    source = (Path(__file__).resolve().parent.parent / "src" / "__init__.py").read_text()
    reconcile_source = source[source.index("def _reconcile_containers(") : source.index("def load(app: Flask)")]

    assert "orchestrator.audit_counts()" in reconcile_source
    assert "active_sessions: target" not in reconcile_source
