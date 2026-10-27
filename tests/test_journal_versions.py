"""Migration fixtures are the released v0 schema, not current schema builders."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from v2c.adapters.runner import CommandRequest, RunnerError
from v2c.runner_service import Journal
from v2c.timeweb_gateway import AllocateRequest, GatewayError, GatewayJournal

RUNNER_V0 = """
CREATE TABLE workspace (
    id TEXT PRIMARY KEY, archive_hash TEXT NOT NULL, manifest TEXT NOT NULL,
    generation INTEGER NOT NULL, attempt_id TEXT NOT NULL, state TEXT NOT NULL
);
CREATE TABLE runtime (
    workspace_id TEXT PRIMARY KEY, definition_json TEXT NOT NULL, state TEXT NOT NULL
);
CREATE TABLE command (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL, result_json TEXT NOT NULL
);
"""

GATEWAY_V0 = """
CREATE TABLE allocation (
    id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, purpose TEXT NOT NULL,
    name TEXT NOT NULL, marker TEXT NOT NULL, cloud_id INTEGER UNIQUE,
    state TEXT NOT NULL, generation INTEGER NOT NULL,
    delete_requested INTEGER NOT NULL DEFAULT 0,
    delete_sent INTEGER NOT NULL DEFAULT 0,
    endpoint TEXT NOT NULL, reason TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE firewall_intent (
    allocation_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, rules_sha256 TEXT NOT NULL,
    generation INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE event (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, allocation_id TEXT NOT NULL,
    action TEXT NOT NULL, generation INTEGER NOT NULL, reason TEXT, created_at REAL NOT NULL
);
"""


def version(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def contents(path: Path) -> list[str]:
    with sqlite3.connect(path) as db:
        return list(db.iterdump())


def runner_v0(path: Path, states: tuple[str, ...] = ("ACCEPTED",)) -> list[CommandRequest]:
    requests = []
    with sqlite3.connect(path) as db:
        db.executescript(RUNNER_V0)
        db.execute(
            "INSERT INTO workspace VALUES (?,?,?,?,?,?)",
            ("work", "snapshot-hash", "{}", 4, "attempt", "READY"),
        )
        for i, state in enumerate(states):
            request = CommandRequest(
                command_id=f"command{i}",
                workspace_id="work",
                attempt_id="attempt",
                generation=4,
                argv=["python3", "check.py"],
                timeout_seconds=12,
            )
            # Released wire form captured independently of CommandResult serialization.
            result = {
                "command_id": request.command_id,
                "request_hash": request.content_hash(),
                "state": state,
                "exit_code": 0 if state == "SUCCEEDED" else None,
                "output": "",
                "output_truncated": False,
                "stop_confirmed": state in {"SUCCEEDED", "FAILED", "CANCELLED"},
                "reason": None,
            }
            db.execute(
                "INSERT INTO command VALUES (?,?,?,?,?)",
                (
                    request.command_id,
                    "work",
                    request.content_hash(),
                    request.model_dump_json(),
                    json.dumps(result),
                ),
            )
            requests.append(request)
    return requests


def gateway_v0(directory: Path) -> Path:
    directory.mkdir(exist_ok=True)
    path = directory / "timeweb-gateway.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(GATEWAY_V0)
        db.execute(
            "INSERT INTO allocation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "vm1",
                "immutable-hash",
                "baseline",
                "owned-name",
                "owned-marker",
                123,
                "DESTROYING",
                2,
                1,
                1,
                "https://example.invalid",
                "DELETE_SENT",
                10.0,
                11.0,
            ),
        )
        db.execute(
            "INSERT INTO firewall_intent VALUES (?,?,?,?,?)",
            ("vm1", "group1", "rules-hash", 1, 10.0),
        )
        db.execute(
            "INSERT INTO event (allocation_id,action,generation,reason,created_at) VALUES (?,?,?,?,?)",
            ("vm1", "DELETE_INTENT_BEFORE_CLOUD_CALL", 2, None, 11.0),
        )
    return path


def test_runner_adopts_legacy_without_replay_or_identity_changes(tmp_path):
    path = tmp_path / "runner.sqlite3"
    states = ("ACCEPTED", "RUNNING", "UNKNOWN", "SUCCEEDED", "FAILED", "CANCELLED")
    requests = runner_v0(path, states)
    journal = Journal(path)
    assert version(path) == 1
    for request, original in zip(requests, states, strict=True):
        result = journal.get(request.command_id)
        assert result.state == ("UNKNOWN" if original in states[:3] else original)
        assert result.request_hash == request.content_hash()
        assert journal.request(request.command_id) == request
        repeated, fresh = journal.admit(request)
        assert not fresh and repeated == result
    with pytest.raises(RunnerError) as blocked:
        journal.admit(requests[0].model_copy(update={"command_id": "new-command"}))
    assert blocked.value.code == "CONFLICT"


def test_malformed_runner_record_rolls_back_version_and_earlier_demotions(tmp_path):
    path = tmp_path / "runner.sqlite3"
    runner_v0(path, ("ACCEPTED", "RUNNING"))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE command SET result_json='invalid-json' WHERE id='command1'")
    before = contents(path)
    with pytest.raises(ValidationError):
        Journal(path)
    assert version(path) == 0
    assert contents(path) == before


@pytest.mark.parametrize("kind", ["runner", "gateway"])
@pytest.mark.parametrize(
    "problem", ["future", "extra-column", "trigger", "missing-table", "extra-sqlite-like-table"]
)
def test_unknown_schema_is_rejected_without_recreating_or_changing_data(tmp_path, kind, problem):
    if kind == "runner":
        path = tmp_path / "runner.sqlite3"
        runner_v0(path)

        def open_journal():
            return Journal(path)

        failure_type = RunnerError
        table = "workspace"
    else:
        path = gateway_v0(tmp_path)

        def open_journal():
            return GatewayJournal(tmp_path)

        failure_type = GatewayError
        table = "firewall_intent"
    with sqlite3.connect(path) as db:
        if problem == "future":
            db.execute("PRAGMA user_version = 2")
        elif problem == "extra-column":
            db.execute(f"ALTER TABLE {table} ADD COLUMN unexpected TEXT")
        elif problem == "trigger":
            db.execute(f"CREATE TRIGGER unexpected AFTER INSERT ON {table} BEGIN SELECT 1; END")
        elif problem == "extra-sqlite-like-table":
            db.execute("CREATE TABLE sqlitex_custom (id TEXT)")
        else:
            db.execute(f"DROP TABLE {table}")
    before = contents(path)
    with pytest.raises(failure_type) as failure:
        open_journal()
    assert failure.value.code == (
        "JOURNAL_VERSION_UNSUPPORTED" if problem == "future" else "JOURNAL_SCHEMA_UNSUPPORTED"
    )
    assert version(path) == (2 if problem == "future" else 0)
    assert contents(path) == before
    assert not list(tmp_path.glob("*.backup"))


def test_gateway_backups_legacy_data_and_keeps_pending_delete_claim(tmp_path):
    path = gateway_v0(tmp_path)
    before = contents(path)
    gateway = GatewayJournal(tmp_path)
    assert version(path) == 1
    assert contents(path) == before  # user_version is a header, not changed rows/schema.
    assert gateway.backup_path is not None
    assert gateway.backup_path.stat().st_mode & 0o777 == 0o600
    assert version(gateway.backup_path) == 0
    assert contents(gateway.backup_path) == before
    assert not gateway.claim_delete("vm1", 2)
    row, fresh = gateway.intent(
        AllocateRequest(allocation_id="vm1", purpose="baseline"),
        "immutable-hash",
        "owned-name",
        "owned-marker",
    )
    assert not fresh and row["cloud_id"] == 123 and row["delete_sent"] == 1
    assert row["generation"] == 2 and row["state"] == "DESTROYING"
    reopened = GatewayJournal(tmp_path)
    assert reopened.backup_path is None
    assert len(list(tmp_path.glob("*.backup"))) == 1


def test_gateway_backup_failure_leaves_legacy_journal_untouched(tmp_path, monkeypatch):
    path = gateway_v0(tmp_path)
    before = contents(path)

    def fail_backup(self):
        raise OSError("synthetic backup destination failure")

    monkeypatch.setattr(GatewayJournal, "_backup_legacy", fail_backup)
    with pytest.raises(OSError, match="synthetic backup"):
        GatewayJournal(tmp_path)
    assert version(path) == 0 and contents(path) == before


def test_concurrent_gateway_openers_adopt_legacy_once(tmp_path):
    path = gateway_v0(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        gateways = list(pool.map(lambda _: GatewayJournal(tmp_path), range(2)))
    assert version(path) == 1
    assert sum(g.backup_path is not None for g in gateways) == 1
    assert len(list(tmp_path.glob("*.backup"))) == 1


def test_new_journals_are_versioned_and_empty_gateway_needs_no_backup(tmp_path):
    runner = Journal(tmp_path / "runner.sqlite3")
    gateway = GatewayJournal(tmp_path)
    assert version(runner.path) == version(gateway.path) == 1
    assert gateway.backup_path is None
    assert not list(tmp_path.glob("*.backup"))


@pytest.mark.parametrize("kind", ["runner", "gateway"])
def test_sql_whitespace_normalization_cannot_hide_missing_constraint(tmp_path, kind):
    # SQLite accepts arbitrary declared type names. Merely removing whitespace
    # from SQL would conflate `TEXT NOT NULL` with the type `TEXTNOTNULL`.
    if kind == "runner":
        path = tmp_path / "runner.sqlite3"
        schema = RUNNER_V0.replace("archive_hash TEXT NOT NULL", "archive_hash TEXTNOTNULL")
        failure_type = RunnerError
    else:
        path = tmp_path / "timeweb-gateway.sqlite3"
        schema = GATEWAY_V0.replace("request_hash TEXT NOT NULL", "request_hash TEXTNOTNULL")
        failure_type = GatewayError
    with sqlite3.connect(path) as db:
        db.executescript(schema)
    before = contents(path)
    with pytest.raises(failure_type) as failure:
        if kind == "runner":
            Journal(path)
        else:
            GatewayJournal(tmp_path)
    assert failure.value.code == "JOURNAL_SCHEMA_UNSUPPORTED"
    assert version(path) == 0 and contents(path) == before


def test_gateway_backup_includes_committed_wal_pages(tmp_path):
    path = gateway_v0(tmp_path)
    # Keep a connection open so the committed update remains in the WAL while
    # the migration copies a consistent database through SQLite's backup API.
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE allocation SET reason='COMMITTED_IN_WAL' WHERE id='vm1'")
        db.commit()
        gateway = GatewayJournal(tmp_path)
        assert gateway.backup_path is not None
        with sqlite3.connect(gateway.backup_path) as backup:
            row = backup.execute(
                "SELECT reason,delete_sent FROM allocation WHERE id='vm1'"
            ).fetchone()
            assert row == ("COMMITTED_IN_WAL", 1)
    finally:
        db.close()
