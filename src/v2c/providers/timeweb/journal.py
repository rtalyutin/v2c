"""Durable Timeweb intents; no network operations occur in journal transactions."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import cast

from .contracts import _PLACEHOLDER_ENDPOINT, AllocateRequest, GatewayError

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS allocation (
    id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, purpose TEXT NOT NULL,
    name TEXT NOT NULL, marker TEXT NOT NULL, cloud_id INTEGER UNIQUE,
    state TEXT NOT NULL, generation INTEGER NOT NULL,
    delete_requested INTEGER NOT NULL DEFAULT 0,
    delete_sent INTEGER NOT NULL DEFAULT 0,
    endpoint TEXT NOT NULL, reason TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS firewall_intent (
    allocation_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, rules_sha256 TEXT NOT NULL,
    generation INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, allocation_id TEXT NOT NULL,
    action TEXT NOT NULL, generation INTEGER NOT NULL, reason TEXT, created_at REAL NOT NULL
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


class GatewayJournal:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "timeweb-gateway.sqlite3"
        self.backup_path: Path | None = None
        # A writer lock covers recognition, backup and version adoption. Legacy
        # readers can still produce a consistent SQLite backup through a second
        # read-only connection, including committed WAL pages.
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_schema(db)
        self.path.chmod(0o600)

    def _ensure_schema(self, db: sqlite3.Connection) -> None:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise GatewayError(
                "JOURNAL_VERSION_UNSUPPORTED",
                "gateway journal version is not supported; preserve it for a compatible gateway",
            )
        actual = _schema_signature(db)
        if version == 0 and not actual[0]:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
        elif actual != _EXPECTED_SCHEMA:
            raise GatewayError(
                "JOURNAL_SCHEMA_UNSUPPORTED",
                "gateway journal schema is not recognized; do not recreate it",
            )
        elif version == 0:
            self.backup_path = self._backup_legacy()
        if version == 0:
            db.execute("PRAGMA user_version = 1")

    def _backup_legacy(self) -> Path:
        descriptor, filename = tempfile.mkstemp(
            prefix=self.path.name + ".v0-", suffix=".backup", dir=self.path.parent
        )
        os.close(descriptor)
        destination = Path(filename)
        try:
            with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as source:
                with sqlite3.connect(destination) as target:
                    source.backup(target)
                    if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise GatewayError(
                            "JOURNAL_BACKUP_FAILED", "gateway journal backup validation failed"
                        )
            return destination
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def get(self, allocation_id: str) -> sqlite3.Row:
        with self.connect() as db:
            row = db.execute("SELECT * FROM allocation WHERE id=?", (allocation_id,)).fetchone()
        if row is None:
            raise GatewayError("NOT_FOUND", "allocation is not owned by this gateway")
        return cast(sqlite3.Row, row)

    def intent(
        self, request: AllocateRequest, request_hash: str, name: str, marker: str
    ) -> tuple[sqlite3.Row, bool]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM allocation WHERE id=?", (request.allocation_id,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise GatewayError(
                        "CONFLICT", "allocation ID already has a different immutable request"
                    )
                return existing, False
            if (
                db.execute("SELECT COUNT(*) FROM allocation WHERE state!='DESTROYED'").fetchone()[0]
                >= 1
            ):
                raise GatewayError(
                    "CAPACITY", "one active or unresolved VM already occupies the gateway"
                )
            now = time.time()
            db.execute(
                "INSERT INTO allocation (id,request_hash,purpose,name,marker,state,generation,endpoint,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    request.allocation_id,
                    request_hash,
                    request.purpose,
                    name,
                    marker,
                    "CREATING",
                    1,
                    _PLACEHOLDER_ENDPOINT,
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO event (allocation_id,action,generation,created_at) VALUES (?,?,?,?)",
                (request.allocation_id, "CREATE_INTENT_BEFORE_CLOUD_CALL", 1, now),
            )
        return self.get(request.allocation_id), True

    def update(
        self,
        allocation_id: str,
        *,
        state: str | None = None,
        reason: str | None = None,
        endpoint: str | None = None,
        cloud_id: int | None = None,
        generation: int | None = None,
    ) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM allocation WHERE id=?", (allocation_id,)).fetchone()
            if row is None:
                raise GatewayError("NOT_FOUND", "unknown allocation")
            if generation is not None and row["generation"] != generation:
                return False
            if cloud_id is not None and row["cloud_id"] is not None and row["cloud_id"] != cloud_id:
                raise GatewayError("OWNERSHIP", "allocation has a different cloud VM identity")
            if row["state"] == "DESTROYED":
                return False
            if cloud_id is not None:
                prior = db.execute(
                    "SELECT id FROM allocation WHERE cloud_id=? AND id!=?",
                    (cloud_id, allocation_id),
                ).fetchone()
                if prior:
                    raise GatewayError(
                        "OWNERSHIP", "cloud VM identity was already used by another allocation"
                    )
            if row["delete_requested"] and state in {"CREATING", "READY"}:
                state = "DESTROYING"
            db.execute(
                "UPDATE allocation SET state=?,reason=?,endpoint=?,cloud_id=?,updated_at=? WHERE id=?",
                (
                    state or row["state"],
                    reason,
                    endpoint or row["endpoint"],
                    cloud_id or row["cloud_id"],
                    time.time(),
                    allocation_id,
                ),
            )
            db.execute(
                "INSERT INTO event (allocation_id,action,generation,reason,created_at) VALUES (?,?,?,?,?)",
                (allocation_id, state or row["state"], row["generation"], reason, time.time()),
            )
        return True

    def claim_firewall(
        self, allocation_id: str, group_id: str, rules_sha256: str, generation: int
    ) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM allocation WHERE id=?", (allocation_id,)).fetchone()
            if row is None or row["generation"] != generation or row["delete_requested"]:
                raise GatewayError("STALE_GENERATION", "firewall attachment owner is stale")
            prior = db.execute(
                "SELECT * FROM firewall_intent WHERE allocation_id=?", (allocation_id,)
            ).fetchone()
            if prior:
                if prior["group_id"] != group_id or prior["rules_sha256"] != rules_sha256:
                    raise GatewayError("CONFLICT", "allocation already has another firewall intent")
                return False
            db.execute(
                "INSERT INTO firewall_intent VALUES (?,?,?,?,?)",
                (allocation_id, group_id, rules_sha256, generation, time.time()),
            )
            db.execute(
                "INSERT INTO event (allocation_id,action,generation,created_at) VALUES (?,?,?,?)",
                (
                    allocation_id,
                    "FIREWALL_ATTACH_INTENT_BEFORE_CLOUD_CALL",
                    generation,
                    time.time(),
                ),
            )
            return True

    def delete_intent(self, allocation_id: str) -> sqlite3.Row:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM allocation WHERE id=?", (allocation_id,)).fetchone()
            if row is None:
                raise GatewayError("NOT_FOUND", "allocation is not owned by this gateway")
            if row["state"] == "DESTROYED":
                return cast(sqlite3.Row, row)
            if not row["delete_requested"]:
                db.execute(
                    "UPDATE allocation SET delete_requested=1,generation=generation+1,state='DESTROYING',updated_at=? WHERE id=?",
                    (time.time(), allocation_id),
                )
                db.execute(
                    "INSERT INTO event (allocation_id,action,generation,created_at) VALUES (?,?,?,?)",
                    (allocation_id, "DESTROY_INTENT", row["generation"] + 1, time.time()),
                )
        return self.get(allocation_id)

    def claim_delete(self, allocation_id: str, generation: int) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE allocation SET delete_sent=1 WHERE id=? AND generation=? AND delete_sent=0 AND delete_requested=1",
                (allocation_id, generation),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO event (allocation_id,action,generation,created_at) VALUES (?,?,?,?)",
                    (allocation_id, "DELETE_INTENT_BEFORE_CLOUD_CALL", generation, time.time()),
                )
        return bool(changed)
