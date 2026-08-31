import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest


def _database_module():
    path = Path(__file__).resolve().parent.parent / "src" / "database.py"
    spec = importlib.util.spec_from_file_location("_rd_database_unit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app(database_url: str):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.__exit__.return_value = False
    db = SimpleNamespace(create_all=MagicMock(), engine=SimpleNamespace(connect=MagicMock(return_value=connection)))
    return SimpleNamespace(config={"SQLALCHEMY_DATABASE_URI": database_url}, db=db), connection


def test_mariadb_bootstrap_holds_application_lock_through_create_and_validation():
    module = _database_module()
    app, connection = _app("mysql+pymysql://database/plugin")
    acquired = MagicMock()
    acquired.scalar.return_value = 1
    released = MagicMock()
    released.scalar.return_value = 1
    connection.execute.side_effect = [acquired, released]

    with (
        patch.object(sys.modules["sqlalchemy"], "text", side_effect=lambda statement: statement, create=True),
        patch.object(module, "validate_database_schema") as validate,
    ):
        module.prepare_database(app)

    lock_name = module._schema_bootstrap_lock_name("mysql+pymysql://database/plugin")
    assert connection.execute.call_args_list == [
        call(
            "SELECT GET_LOCK(:lock_name, 60)",
            {"lock_name": lock_name},
        ),
        call(
            "SELECT RELEASE_LOCK(:lock_name)",
            {"lock_name": lock_name},
        ),
    ]
    app.db.create_all.assert_called_once_with()
    validate.assert_called_once_with(app)


def test_schema_bootstrap_lock_is_stable_per_database_and_separates_databases():
    module = _database_module()
    first = module._schema_bootstrap_lock_name("mysql+pymysql://one:secret@db-a.example/ctfd")
    same_database = module._schema_bootstrap_lock_name("mariadb+pymysql://two:other@db-b.example/ctfd")
    other_database = module._schema_bootstrap_lock_name("mysql+pymysql://one:secret@db-a.example/ctfd_two")

    assert first == same_database
    assert first != other_database
    assert first.startswith("ctfd_rd_schema_")
    assert len(first) < 64
    assert "secret" not in first


def test_mariadb_bootstrap_timeout_fails_without_creating_tables():
    module = _database_module()
    app, connection = _app("mariadb+pymysql://database/plugin")
    result = MagicMock()
    result.scalar.return_value = 0
    connection.execute.return_value = result

    with patch.object(sys.modules["sqlalchemy"], "text", side_effect=lambda statement: statement, create=True):
        with pytest.raises(RuntimeError, match="schema bootstrap lock"):
            module.prepare_database(app)

    app.db.create_all.assert_not_called()
    assert connection.execute.call_count == 1


def test_mariadb_bootstrap_discards_connection_when_lock_release_fails():
    module = _database_module()
    app, connection = _app("mariadb+pymysql://database/plugin")
    acquired = MagicMock()
    acquired.scalar.return_value = 1
    connection.execute.side_effect = [acquired, RuntimeError("connection lost")]

    with (
        patch.object(sys.modules["sqlalchemy"], "text", side_effect=lambda statement: statement, create=True),
        patch.object(module, "validate_database_schema"),
        pytest.raises(RuntimeError, match="connection lost"),
    ):
        module.prepare_database(app)

    connection.invalidate.assert_called_once_with()


def test_mariadb_bootstrap_discards_connection_when_release_is_not_confirmed():
    module = _database_module()
    app, connection = _app("mariadb+pymysql://database/plugin")
    acquired = MagicMock()
    acquired.scalar.return_value = 1
    not_owned = MagicMock()
    not_owned.scalar.return_value = 0
    connection.execute.side_effect = [acquired, not_owned]

    with (
        patch.object(sys.modules["sqlalchemy"], "text", side_effect=lambda statement: statement, create=True),
        patch.object(module, "validate_database_schema"),
        pytest.raises(RuntimeError, match="failed to release"),
    ):
        module.prepare_database(app)

    connection.invalidate.assert_called_once_with()


def test_sqlite_fresh_bootstrap_creates_then_validates_without_advisory_lock():
    module = _database_module()
    app, _connection = _app("sqlite:///:memory:")

    with patch.object(module, "validate_database_schema") as validate:
        module.prepare_database(app)

    app.db.engine.connect.assert_not_called()
    app.db.create_all.assert_called_once_with()
    validate.assert_called_once_with(app)
