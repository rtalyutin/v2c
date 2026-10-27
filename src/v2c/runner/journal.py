"""Durable command admission and restart reconciliation."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import cast

from ..adapters.runner import CommandRequest, CommandResult, RunnerError

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace (
    id TEXT PRIMARY KEY, archive_hash TEXT NOT NULL, manifest TEXT NOT NULL,
    generation INTEGER NOT NULL, attempt_id TEXT NOT NULL, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime (
    workspace_id TEXT PRIMARY KEY, definition_json TEXT NOT NULL, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS command (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL, result_json TEXT NOT NULL
);
"""


SchemaSignature = tuple[
    list[tuple[str, str, str, str]],
    dict[str, tuple[tuple[object, ...], ...]],
]


def _schema_signature(db: sqlite3.Connection) -> SchemaSignature:
    """Check SQL objects and column semantics, including hidden columns.

    SQL whitespace alone is not a safe signature: ``TEXT NOT NULL`` and a
    custom SQLite type ``TEXTNOTNULL`` would otherwise look identical.
    """
    objects = [
        (
            kind,
            name,
            table,
            "".join(sql.split()).casefold().replace("createtableifnotexists", "createtable"),
        )
        for kind, name, table, sql in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE substr(name, 1, 7) != 'sqlite_' ORDER BY type,name"
        )
    ]
    columns = {
        name: tuple(
            tuple(row) for row in db.execute("SELECT * FROM pragma_table_xinfo(?)", (name,))
        )
        for kind, name, _, _ in objects
        if kind == "table"
    }
    return objects, columns


def _expected_schema() -> SchemaSignature:
    with sqlite3.connect(":memory:") as db:
        db.executescript(_SCHEMA)
        return _schema_signature(db)


_EXPECTED_SCHEMA = _expected_schema()


class Journal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        # Validate before enabling WAL or altering existing data. Versioning and
        # restart demotion are one transaction: malformed records cannot leave a
        # partially adopted legacy journal behind.
        with sqlite3.connect(self.path, timeout=30) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            self._ensure_schema(db)
            for row in db.execute("SELECT id,result_json FROM command").fetchall():
                result = CommandResult.model_validate_json(row["result_json"])
                if result.state not in TERMINAL:
                    result = result.model_copy(
                        update={"state": "UNKNOWN", "reason": "RUNNER_RESTART_REQUIRES_READBACK"}
                    )
                    db.execute(
                        "UPDATE command SET result_json=? WHERE id=?",
                        (result.model_dump_json(), row["id"]),
                    )

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise RunnerError(
                "JOURNAL_VERSION_UNSUPPORTED",
                "runner journal version is not supported; preserve the VM and reconcile its effects",
            )
        actual = _schema_signature(db)
        if version == 0 and not actual[0]:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
        elif actual != _EXPECTED_SCHEMA:
            raise RunnerError(
                "JOURNAL_SCHEMA_UNSUPPORTED",
                "runner journal schema is not recognized; do not recreate it",
            )
        if version == 0:
            db.execute("PRAGMA user_version = 1")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def get(self, command_id: str) -> CommandResult:
        with self.connect() as db:
            row = db.execute("SELECT result_json FROM command WHERE id=?", (command_id,)).fetchone()
        if not row:
            raise RunnerError(
                "NOT_FOUND", "command journal has no record; absence is not proof of nonexecution"
            )
        return CommandResult.model_validate_json(row["result_json"])

    def save(self, result: CommandResult) -> None:
        with self.lock, self.connect() as db:
            # Cancel and runner-readback terminal results cannot be overwritten by
            # an older attachment thread that finishes later.
            row = db.execute(
                "SELECT result_json FROM command WHERE id=?", (result.command_id,)
            ).fetchone()
            previous = CommandResult.model_validate_json(row["result_json"]) if row else None
            if previous and previous.state in TERMINAL:
                return
            db.execute(
                "UPDATE command SET result_json=? WHERE id=?",
                (result.model_dump_json(), result.command_id),
            )

    def request(self, command_id: str) -> CommandRequest:
        with self.connect() as db:
            row = db.execute(
                "SELECT request_json FROM command WHERE id=?", (command_id,)
            ).fetchone()
        if not row:
            raise RunnerError("NOT_FOUND", "unknown command")
        return CommandRequest.model_validate_json(row["request_json"])

    def workspace(self, workspace_id: str) -> sqlite3.Row:
        with self.connect() as db:
            row = db.execute("SELECT * FROM workspace WHERE id=?", (workspace_id,)).fetchone()
        if not row or row["state"] != "READY":
            raise RunnerError("NOT_FOUND", "workspace is not ready")
        return cast(sqlite3.Row, row)

    def admit(self, request: CommandRequest) -> tuple[CommandResult, bool]:
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT request_hash,result_json FROM command WHERE id=?", (request.command_id,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != request.content_hash():
                    raise RunnerError("CONFLICT", "command ID has a different request hash")
                return CommandResult.model_validate_json(existing["result_json"]), False
            row = db.execute(
                "SELECT * FROM workspace WHERE id=?", (request.workspace_id,)
            ).fetchone()
            if not row or row["state"] != "READY":
                raise RunnerError("NOT_FOUND", "workspace is not ready")
            if row["attempt_id"] != request.attempt_id or row["generation"] != request.generation:
                raise RunnerError("STALE_GENERATION", "attempt authorization is stale")
            for other in db.execute(
                "SELECT result_json FROM command WHERE workspace_id=?", (request.workspace_id,)
            ):
                if CommandResult.model_validate_json(other["result_json"]).state not in TERMINAL:
                    raise RunnerError("CONFLICT", "previous command has not confirmed termination")
            result = CommandResult(
                command_id=request.command_id, request_hash=request.content_hash(), state="ACCEPTED"
            )
            db.execute(
                "INSERT INTO command VALUES (?,?,?,?,?)",
                (
                    request.command_id,
                    request.workspace_id,
                    request.content_hash(),
                    request.model_dump_json(),
                    result.model_dump_json(),
                ),
            )
            return result, True
