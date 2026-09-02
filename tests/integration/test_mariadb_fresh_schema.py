from __future__ import annotations

import importlib.util
import multiprocessing
import os
from pathlib import Path
import queue
import sys
import threading
import time
import types

import pytest

flask_sqlalchemy = pytest.importorskip("flask_sqlalchemy")

from flask import Flask  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402

SQLAlchemy = flask_sqlalchemy.SQLAlchemy


DATABASE_URL = os.environ.get("REMOTE_DESKTOP_TEST_MARIADB_URL")
DESTRUCTIVE_GUARD = os.environ.get("REMOTE_DESKTOP_TEST_ALLOW_DESTRUCTIVE")
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="disposable MariaDB URL not configured")


db = SQLAlchemy()


class Users(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(512), nullable=False)
    hidden = db.Column(db.Boolean, nullable=False, default=False)


def _load_source_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType("_rd_fresh_schema")
package.__path__ = [str(SRC_ROOT)]
sys.modules[package.__name__] = package

ctfd_package = types.ModuleType("CTFd")
ctfd_models = types.ModuleType("CTFd.models")
ctfd_models.db = db
ctfd_models.Users = Users
ctfd_package.models = ctfd_models
sys.modules["CTFd"] = ctfd_package
sys.modules["CTFd.models"] = ctfd_models

_load_source_module("_rd_fresh_schema.settings", SRC_ROOT / "settings.py")
models = _load_source_module("_rd_fresh_schema.models", SRC_ROOT / "models.py")
database = _load_source_module("_rd_fresh_schema.database", SRC_ROOT / "database.py")


def _new_app(name: str) -> Flask:
    app = Flask(name)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=DATABASE_URL,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    app.db = db  # type: ignore[attr-defined]
    return app


def _bootstrap_worker(start, results, worker_number: int) -> None:
    app = _new_app(f"fresh-bootstrap-{worker_number}")
    try:
        if not start.wait(timeout=10):
            raise RuntimeError("bootstrap start barrier timed out")
        with app.app_context():
            database.prepare_database(app)
        results.put(None)
    except BaseException as exc:
        results.put(f"{type(exc).__name__}: {exc}")


def _reset_to_fresh_ctfd_schema() -> None:
    assert DESTRUCTIVE_GUARD == "ctfd_plugin_schema_test", (
        "refusing destructive schema test without the disposable-database guard"
    )
    engine = create_engine(DATABASE_URL)
    try:
        db.metadata.drop_all(engine)
        Users.__table__.create(engine)  # ctfd owns the users table and creates it before plugins load
    finally:
        engine.dispose()


def _bootstrap_once() -> None:
    app = _new_app(f"fresh-bootstrap-{time.time_ns()}")
    with app.app_context():
        database.prepare_database(app)


def test_two_workers_bootstrap_the_fresh_schema_and_constraints() -> None:
    _reset_to_fresh_ctfd_schema()
    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()
    workers = [context.Process(target=_bootstrap_worker, args=(start, results, number)) for number in (1, 2)]

    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(timeout=30)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
            pytest.fail("fresh schema bootstrap worker hung")
        assert worker.exitcode == 0

    failures = []
    for _worker in workers:
        try:
            result = results.get(timeout=5)
        except queue.Empty:
            pytest.fail("fresh schema bootstrap worker returned no result")
        if result is not None:
            failures.append(result)
    assert failures == []

    engine = create_engine(DATABASE_URL)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {
            "desktop_container_info",
            "desktop_session_history",
            "desktop_session_operations",
            "desktop_settings",
            "desktop_plugin_metadata",
            "desktop_event_log",
        } <= tables

        active_uniques = {
            tuple(constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints("desktop_container_info")
        }
        assert ("user_id",) in active_uniques
        assert ("session_uuid",) in active_uniques
        assert not [
            foreign_key
            for foreign_key in inspector.get_foreign_keys("desktop_container_info")
            if "user_id" in (foreign_key.get("constrained_columns") or ())
        ]
    finally:
        engine.dispose()


def test_bootstrap_waits_for_the_database_scoped_advisory_lock() -> None:
    _reset_to_fresh_ctfd_schema()
    engine = create_engine(DATABASE_URL)
    lock_name = database._schema_bootstrap_lock_name(str(DATABASE_URL))
    finished = threading.Event()
    errors: list[BaseException] = []

    def bootstrap() -> None:
        try:
            _bootstrap_once()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    try:
        with engine.connect() as blocker:
            assert blocker.execute(text("SELECT GET_LOCK(:name, 5)"), {"name": lock_name}).scalar_one() == 1
            worker = threading.Thread(target=bootstrap, daemon=True)
            worker.start()
            time.sleep(0.25)
            assert not finished.is_set(), "bootstrap bypassed the database-scoped advisory lock"
            assert blocker.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name}).scalar_one() == 1
            worker.join(timeout=15)
            assert not worker.is_alive()
        assert errors == []
    finally:
        engine.dispose()


def test_two_connections_serialize_settings_revision_for_update() -> None:
    _reset_to_fresh_ctfd_schema()
    _bootstrap_once()
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM desktop_settings WHERE `key` = '_settings_revision'"))
        connection.execute(text("INSERT INTO desktop_settings (`key`, value) VALUES ('_settings_revision', '1')"))

    first_locked = threading.Event()
    release_first = threading.Event()
    second_locked = threading.Event()
    errors: list[BaseException] = []

    def first_connection() -> None:
        try:
            with engine.connect() as connection, connection.begin():
                value = connection.execute(
                    text("SELECT value FROM desktop_settings WHERE `key` = '_settings_revision' FOR UPDATE")
                ).scalar_one()
                assert value == "1"
                first_locked.set()
                assert release_first.wait(timeout=10)
                connection.execute(text("UPDATE desktop_settings SET value = '2' WHERE `key` = '_settings_revision'"))
        except BaseException as exc:
            errors.append(exc)
            first_locked.set()
            release_first.set()

    def second_connection() -> None:
        try:
            assert first_locked.wait(timeout=10)
            with engine.connect() as connection:
                connection.execute(text("SET SESSION innodb_lock_wait_timeout = 10"))
                connection.commit()
                with connection.begin():
                    value = connection.execute(
                        text("SELECT value FROM desktop_settings WHERE `key` = '_settings_revision' FOR UPDATE")
                    ).scalar_one()
                    assert value == "2"
                    second_locked.set()
                    connection.execute(
                        text("UPDATE desktop_settings SET value = '3' WHERE `key` = '_settings_revision'")
                    )
        except BaseException as exc:
            errors.append(exc)
            second_locked.set()

    first = threading.Thread(target=first_connection, daemon=True)
    second = threading.Thread(target=second_connection, daemon=True)
    first.start()
    second.start()
    assert first_locked.wait(timeout=10)
    time.sleep(0.25)
    assert not second_locked.is_set(), "the second connection bypassed the row lock"
    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT value FROM desktop_settings WHERE `key` = '_settings_revision'")
            ).scalar_one()
            == "3"
        )
    engine.dispose()


def test_schema_bootstrap_is_restart_idempotent() -> None:
    _reset_to_fresh_ctfd_schema()
    _bootstrap_once()
    engine = create_engine(DATABASE_URL)
    try:
        before = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    _bootstrap_once()

    engine = create_engine(DATABASE_URL)
    try:
        assert set(inspect(engine).get_table_names()) == before
    finally:
        engine.dispose()


def test_existing_mismatched_schema_is_rejected_without_repair() -> None:
    _reset_to_fresh_ctfd_schema()
    _bootstrap_once()
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE desktop_container_info MODIFY lifecycle_state VARCHAR(32) NULL"))
    finally:
        engine.dispose()

    app = _new_app(f"fresh-mismatch-{time.time_ns()}")
    with app.app_context(), pytest.raises(RuntimeError, match="lifecycle_state has incorrect nullability"):
        database.prepare_database(app)


def test_complete_model_contract_rejects_type_length_and_unique_mismatches() -> None:
    _reset_to_fresh_ctfd_schema()
    _bootstrap_once()
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE desktop_reports MODIFY content INT NOT NULL"))
            connection.execute(text("ALTER TABLE desktop_docker_contexts MODIFY hostname VARCHAR(64) NULL"))
            connection.execute(
                text("ALTER TABLE desktop_session_operations DROP INDEX uq_desktop_session_operations_operation_uuid")
            )
    finally:
        engine.dispose()

    app = _new_app(f"fresh-contract-mismatch-{time.time_ns()}")
    with app.app_context(), pytest.raises(RuntimeError) as exc_info:
        database.prepare_database(app)
    error = str(exc_info.value)
    assert "desktop_reports.content has incorrect type" in error
    assert "desktop_docker_contexts.hostname must have length 512" in error
    assert "desktop_session_operations missing UNIQUE('operation_uuid',)" in error


def test_missing_operation_user_primary_key_is_rejected_without_repair() -> None:
    _reset_to_fresh_ctfd_schema()
    _bootstrap_once()
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            # mariadb needs auto_increment removed in the same statement before the primary key can be dropped
            connection.execute(
                text("ALTER TABLE desktop_session_operations MODIFY user_id INTEGER NOT NULL, DROP PRIMARY KEY")
            )
    finally:
        engine.dispose()

    app = _new_app(f"fresh-operation-mismatch-{time.time_ns()}")
    with app.app_context(), pytest.raises(RuntimeError, match="desktop_session_operations must have PRIMARY KEY"):
        database.prepare_database(app)
