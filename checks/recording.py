from __future__ import annotations

import ast
import base64
import json
import logging
import math
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock


source = Path(__file__).resolve().parents[1] / "src" / "command_logs.py"
tree = ast.parse(source.read_text())
selected: list[ast.stmt] = [
    node
    for node in tree.body
    if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_READ_JOURNAL" for t in node.targets))
    or (isinstance(node, ast.FunctionDef) and node.name in {"_tool", "_parse", "collect_session"})
]
namespace: dict[str, Any] = {
    "base64": base64,
    "json": json,
    "logging": logging,
    "math": math,
    "shlex": shlex,
    "time": time,
}
exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])), str(source), "exec"), namespace)
checks = []


def check(condition, label):
    assert condition, label
    checks.append(label)


def main():
    with tempfile.TemporaryDirectory(prefix="desktop-recording-check-") as directory:
        root = Path(directory)
        journal = root / "journal.jsonl"
        socket_path = root / "capture.sock"
        cursor = SimpleNamespace(
            user_id=7,
            container_id="fixture",
            session_uuid="fixture-session",
            byte_offset=0,
            journal_id=None,
            last_read_at=None,
            status="unknown",
        )
        records = []
        db = SimpleNamespace(session=MagicMock())
        db.session.add_all.side_effect = lambda rows: records.extend(rows)
        query = MagicMock()
        query.filter_by.return_value.with_for_update.return_value.first.return_value = cursor
        namespace.update(
            db=db,
            logger=logging.getLogger("recording-check"),
            JOURNAL_PATH=str(journal),
            COLLECTOR_SOCKET_PATH=str(socket_path),
            _cursor=lambda *_args: cursor,
            DesktopCommandCursorModel=SimpleNamespace(query=query),
            DesktopRecordedCommandModel=lambda **row: row,
        )

        class Host:
            def exec_in_container(self, context, container_id, command):
                assert (context, container_id) == ("local-fixture", "fixture")
                result = subprocess.run([sys.executable, *command[1:]], capture_output=True, text=True, check=False)
                return result.returncode, result.stdout

        def collect():
            namespace["collect_session"](Host(), 7, "fixture-session", "local-fixture", "fixture")

        def event(index):
            return (
                json.dumps(
                    {
                        "ts": 1700000000 + index,
                        "cmd": f"printf fixture-{index}",
                        "exit": 0,
                        "duration_ms": 1,
                        "cwd": "/fixture",
                        "tty": "/dev/pts/0",
                    }
                ).encode()
                + b"\n"
            )

        journal.write_bytes(event(0))
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            server.bind(str(socket_path))
            collect()
            check(
                cursor.status == "recording" and len(records) == 1, "bound collector permits live status and ingestion"
            )
            offset, identity, contents = cursor.byte_offset, cursor.journal_id, journal.read_bytes()
            for _ in range(20):
                collect()
            check(
                journal.read_bytes() == contents and len(records) == 1, "connect-only probes create no journal events"
            )
            socket_path.unlink()
            journal.write_bytes(contents + event(1))
            collect()
            check(
                cursor.status == "unavailable" and len(records) == 2 and cursor.byte_offset > offset,
                "capture loss retains and completes readable history",
            )
            check(cursor.journal_id == identity, "capture loss preserves journal identity")
            server.close()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(str(socket_path))
            collect()
            check(cursor.status == "recording" and len(records) == 2, "collector recovery does not replay history")
            server.close()
            check(socket_path.exists(), "closed collector leaves a stale socket inode")
            collect()
            check(cursor.status == "unavailable" and len(records) == 2, "stale socket is unavailable")
            offset = cursor.byte_offset
            journal.write_bytes(b"")
            collect()
            check(
                cursor.status == "discontinuous" and cursor.byte_offset == offset and len(records) == 2,
                "truncation preserves exact historical cursor",
            )
            collect()
            check(cursor.status == "discontinuous", "known history loss remains sticky")
            journal.unlink()
            cursor.status = "unknown"
            collect()
            check(
                cursor.status == "unavailable" and cursor.byte_offset == offset, "missing journal remains unavailable"
            )
        finally:
            server.close()
    print(json.dumps({"pass": True, "checks": checks, "source": str(source)}))


if __name__ == "__main__":
    main()
