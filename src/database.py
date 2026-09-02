from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import unquote, urlsplit


_SCHEMA_BOOTSTRAP_LOCK_PREFIX = "ctfd_rd_schema_"


def _schema_bootstrap_lock_name(database_url: str) -> str:
    identity = unquote(urlsplit(database_url).path.lstrip("/"))
    if not identity:
        raise RuntimeError("remote desktop MariaDB URL must select a database")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]  # mariadb caps lock names at 64 chars
    return f"{_SCHEMA_BOOTSTRAP_LOCK_PREFIX}{digest}"


def prepare_database(app: Any) -> None:
    """creates the schema for a fresh deployment, existing tables are never mutated
    a mismatch fails closed in validate_database_schema"""
    database_url = app.config.get("SQLALCHEMY_DATABASE_URI")
    if not isinstance(database_url, str):
        app.db.create_all()  # test app doubles set no SQLALCHEMY_DATABASE_URI, production always does
        return

    if not database_url.startswith(("mysql", "mariadb")):
        app.db.create_all()
        validate_database_schema(app)
        return

    from sqlalchemy import text

    lock_name = _schema_bootstrap_lock_name(database_url)
    with app.db.engine.connect() as lock_connection:
        # workers without preload build the app at once, the lock keeps them off one check then create
        acquired = lock_connection.execute(
            text("SELECT GET_LOCK(:lock_name, 60)"),
            {"lock_name": lock_name},
        ).scalar()
        if acquired != 1:
            raise RuntimeError("timed out waiting for remote desktop schema bootstrap lock")

        try:
            app.db.create_all()
            validate_database_schema(app)
        finally:
            try:
                released = lock_connection.execute(
                    text("SELECT RELEASE_LOCK(:lock_name)"),
                    {"lock_name": lock_name},
                ).scalar()
            except BaseException:
                # a failed RELEASE_LOCK may still hold the lock, invalidate discards the connection
                lock_connection.invalidate()
                raise

            if released != 1:
                lock_connection.invalidate()
                raise RuntimeError("failed to release remote desktop schema bootstrap lock")


def validate_database_schema(app: Any) -> None:
    from sqlalchemy import inspect

    inspector = inspect(app.db.engine)
    tables = set(inspector.get_table_names())
    required_columns = {
        "desktop_docker_contexts": {
            "id",
            "context_name",
            "hostname",
            "pub_hostname",
            "weight",
            "enabled",
            "max_containers",
            "active_sessions",
        },
        "desktop_container_info": {
            "container_id",
            "user_id",
            "container_name",
            "vnc_port",
            "novnc_port",
            "ssh_port",
            "ttyd_port",
            "vnc_password",
            "vnc_url",
            "docker_context",
            "pub_hostname",
            "container_username",
            "created_at",
            "timer_started",
            "timer_start_time",
            "timer_duration",
            "extensions_used",
            "max_extensions",
            "cookie_sid",
            "paused_at",
            "session_uuid",
            "lifecycle_state",
            "lifecycle_reason",
        },
        "desktop_session_history": {
            "id",
            "user_id",
            "username",
            "docker_context",
            "started_at",
            "ended_at",
            "duration",
            "end_reason",
            "extensions_used",
            "container_name",
            "session_uuid",
        },
        "desktop_session_operations": {
            "user_id",
            "operation_uuid",
            "worker_lease_uuid",
            "session_uuid",
            "state",
            "cancel_requested",
            "docker_context",
            "container_name",
            "capacity_reserved",
            "requested_reason",
            "created_at",
            "updated_at",
            "heartbeat_at",
            "error",
        },
        "desktop_reports": {
            "id",
            "user_id",
            "username",
            "timestamp",
            "content",
        },
        "desktop_settings": {"key", "value"},
        "desktop_plugin_metadata": {"key", "value"},
        "desktop_event_log": {
            "id",
            "event_id",
            "timestamp",
            "event_type",
            "level",
            "user_id",
            "username",
            "message",
            "metadata_json",
        },
    }
    problems: list[str] = []
    for table, expected in required_columns.items():
        if table not in tables:
            problems.append(f"missing table {table}")
            continue
        actual = {column["name"] for column in inspector.get_columns(table)}
        missing = sorted(expected - actual)
        if missing:
            problems.append(f"{table} missing columns {', '.join(missing)}")
        unexpected = sorted(actual - expected)
        if unexpected:
            problems.append(f"{table} has unexpected columns {', '.join(unexpected)}")

    # fresh install bootstrap has no repair path, a same named incompatible table must fail here not mid session
    column_contracts: dict[str, dict[str, tuple[bool, tuple[str, ...], int | None]]] = {
        "desktop_docker_contexts": {
            "id": (False, ("int",), None),
            "context_name": (False, ("char", "string"), 512),
            "hostname": (True, ("char", "string"), 512),
            "pub_hostname": (False, ("char", "string"), 512),
            "weight": (True, ("int",), None),
            "enabled": (True, ("bool", "tinyint"), None),
            "max_containers": (True, ("int",), None),
            "active_sessions": (False, ("int",), None),
        },
        "desktop_container_info": {
            "container_id": (False, ("char", "string"), 512),
            "user_id": (False, ("int",), None),
            "container_name": (False, ("char", "string"), 512),
            "vnc_port": (False, ("int",), None),
            "novnc_port": (False, ("int",), None),
            "ssh_port": (True, ("int",), None),
            "ttyd_port": (True, ("int",), None),
            "vnc_password": (False, ("char", "string"), 256),
            "vnc_url": (False, ("text",), None),
            "docker_context": (False, ("char", "string"), 512),
            "pub_hostname": (False, ("char", "string"), 512),
            "container_username": (True, ("char", "string"), 64),
            "created_at": (False, ("float", "double", "real"), None),
            "timer_started": (True, ("bool", "tinyint"), None),
            "timer_start_time": (True, ("float", "double", "real"), None),
            "timer_duration": (True, ("float", "double", "real"), None),
            "extensions_used": (True, ("int",), None),
            "max_extensions": (True, ("int",), None),
            "cookie_sid": (True, ("char", "string"), 128),
            "paused_at": (True, ("float", "double", "real"), None),
            "session_uuid": (False, ("char", "string"), 36),
            "lifecycle_state": (False, ("char", "string"), 32),
            "lifecycle_reason": (True, ("char", "string"), 128),
        },
        "desktop_session_history": {
            "id": (False, ("int",), None),
            "user_id": (False, ("int",), None),
            "username": (False, ("char", "string"), 512),
            "docker_context": (False, ("char", "string"), 512),
            "started_at": (False, ("float", "double", "real"), None),
            "ended_at": (False, ("float", "double", "real"), None),
            "duration": (False, ("float", "double", "real"), None),
            "end_reason": (False, ("char", "string"), 128),
            "extensions_used": (True, ("int",), None),
            "container_name": (True, ("char", "string"), 512),
            "session_uuid": (False, ("char", "string"), 36),
        },
        "desktop_session_operations": {
            "user_id": (False, ("int",), None),
            "operation_uuid": (False, ("char", "string"), 36),
            "worker_lease_uuid": (True, ("char", "string"), 36),
            "session_uuid": (True, ("char", "string"), 36),
            "state": (False, ("char", "string"), 32),
            "cancel_requested": (False, ("bool", "tinyint"), None),
            "docker_context": (True, ("char", "string"), 512),
            "container_name": (True, ("char", "string"), 512),
            "capacity_reserved": (False, ("bool", "tinyint"), None),
            "requested_reason": (True, ("char", "string"), 128),
            "created_at": (False, ("float", "double", "real"), None),
            "updated_at": (False, ("float", "double", "real"), None),
            "heartbeat_at": (True, ("float", "double", "real"), None),
            "error": (True, ("text",), None),
        },
        "desktop_reports": {
            "id": (False, ("int",), None),
            "user_id": (False, ("int",), None),
            "username": (False, ("char", "string"), 512),
            "timestamp": (False, ("float", "double", "real"), None),
            "content": (False, ("text",), None),
        },
        "desktop_settings": {
            "key": (False, ("char", "string"), 512),
            "value": (True, ("text",), None),
        },
        "desktop_plugin_metadata": {
            "key": (False, ("char", "string"), 128),
            "value": (False, ("char", "string"), 512),
        },
        "desktop_event_log": {
            "id": (False, ("int",), None),
            "event_id": (False, ("char", "string"), 128),
            "timestamp": (False, ("float", "double", "real"), None),
            "event_type": (False, ("char", "string"), 128),
            "level": (False, ("char", "string"), 16),
            "user_id": (True, ("int",), None),
            "username": (True, ("char", "string"), 512),
            "message": (False, ("text",), None),
            "metadata_json": (True, ("text",), None),
        },
    }
    for table, expectations in column_contracts.items():
        if table not in tables:
            continue
        by_name = {column["name"]: column for column in inspector.get_columns(table)}
        for column_name, (nullable, type_tokens, length) in expectations.items():
            column = by_name.get(column_name)
            if column is None:
                continue
            if column.get("nullable") is not nullable:
                problems.append(f"{table}.{column_name} has incorrect nullability")
            reflected_type = column.get("type")
            type_label = f"{type(reflected_type).__name__} {reflected_type}".lower()
            if not any(token in type_label for token in type_tokens):
                problems.append(f"{table}.{column_name} has incorrect type")
            if length is not None and getattr(reflected_type, "length", None) != length:
                problems.append(f"{table}.{column_name} must have length {length}")

    expected_uniques = {
        "desktop_docker_contexts": (("context_name",),),
        "desktop_container_info": (("user_id",), ("session_uuid",)),
        "desktop_session_history": (("session_uuid",),),
        "desktop_session_operations": (("operation_uuid",), ("session_uuid",)),
        "desktop_event_log": (("event_id",),),
    }
    for table, expected_unique_sets in expected_uniques.items():
        if table not in tables:
            continue
        unique_column_sets = {
            tuple(constraint.get("column_names") or ()) for constraint in inspector.get_unique_constraints(table)
        }
        unique_column_sets.update(
            tuple(index.get("column_names") or ()) for index in inspector.get_indexes(table) if index.get("unique")
        )
        for columns in expected_unique_sets:
            if columns not in unique_column_sets:
                problems.append(f"{table} missing UNIQUE{columns}")

    expected_primary_keys = {
        "desktop_docker_contexts": ("id",),
        "desktop_container_info": ("container_id",),
        "desktop_session_history": ("id",),
        "desktop_session_operations": ("user_id",),
        "desktop_reports": ("id",),
        "desktop_settings": ("key",),
        "desktop_plugin_metadata": ("key",),
        "desktop_event_log": ("id",),
    }
    for table, expected_columns in expected_primary_keys.items():
        if table not in tables:
            continue
        actual_columns = tuple(inspector.get_pk_constraint(table).get("constrained_columns") or ())
        if actual_columns != expected_columns:
            problems.append(f"{table} must have PRIMARY KEY{expected_columns}")

    for table in (
        "desktop_container_info",
        "desktop_session_operations",
        "desktop_session_history",
        "desktop_event_log",
    ):
        if table not in tables:
            continue
        user_foreign_keys = [
            foreign_key
            for foreign_key in inspector.get_foreign_keys(table)
            if "user_id" in (foreign_key.get("constrained_columns") or ())
        ]
        if user_foreign_keys:
            problems.append(f"{table}.user_id must not have a foreign key")

    if problems:
        raise RuntimeError("remote desktop schema does not match the fresh-install model: " + "; ".join(problems))
