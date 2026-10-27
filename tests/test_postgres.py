"""Actual PostgreSQL races; opt-in only on a dedicated disposable test database.

CI migrates V2C_TEST_POSTGRES_URL first. No schema creation, dropping or shared
row cleanup is performed here. Only uniquely owned fixture rows are removed.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import delete, event, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from v2c.db import transaction
from v2c.engine import DomainError, Engine
from v2c.models import (
    Approval,
    Artifact,
    Attempt,
    Base,
    Command,
    Entity,
    EntityParameterValue,
    Event,
    Plan,
    Run,
    RuntimeLock,
    Snapshot,
    Step,
    UsageEvent,
    VerificationResult,
)

URL = os.environ.get("V2C_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="V2C_TEST_POSTGRES_URL not supplied; PostgreSQL concurrency not exercised"
)


def cleanup_owned(engine, owner_id):
    with engine._write() as (s, lock):
        projects = list(s.scalars(select(Entity.id).where(Entity.owner_id == owner_id)))
        runs = list(s.scalars(select(Run.id).where(Run.project_id.in_(projects))))
        steps = list(s.scalars(select(Step.id).where(Step.run_id.in_(runs))))
        attempts = list(s.scalars(select(Attempt.id).where(Attempt.step_id.in_(steps))))
        if lock.active_run_id in runs:
            lock.active_run_id = None
            s.flush()
        for model, predicate in [
            (Command, Command.attempt_id.in_(attempts)),
            (UsageEvent, UsageEvent.run_id.in_(runs)),
            (Attempt, Attempt.id.in_(attempts)),
            (Step, Step.id.in_(steps)),
            (Approval, Approval.run_id.in_(runs)),
            (Plan, Plan.run_id.in_(runs)),
            (Artifact, Artifact.run_id.in_(runs)),
            (VerificationResult, VerificationResult.run_id.in_(runs)),
            (Event, Event.run_id.in_(runs)),
            (Run, Run.id.in_(runs)),
            (Snapshot, Snapshot.project_id.in_(projects)),
            (EntityParameterValue, EntityParameterValue.owner_id == owner_id),
            (Entity, Entity.owner_id == owner_id),
        ]:
            s.execute(delete(model).where(predicate))


def test_postgres_migration_schema_global_claim_and_atomic_budget():
    e1, e2 = Engine(URL), Engine(URL)
    assert e1.db.dialect.name == "postgresql", "Use a PostgreSQL test URL"
    # Different pools/connections exercise the DB protocol rather than one
    # process mutex. This is intentionally not substituted by SQLite.
    with e1.db.connect() as c1, e2.db.connect() as c2:
        assert c1.scalar(text("select pg_backend_pid()")) != c2.scalar(
            text("select pg_backend_pid()")
        )
    inspector = inspect(e1.db)
    assert set(Base.metadata.tables) <= set(inspector.get_table_names())
    for table in ("attempts", "commands", "entity_parameter_values", "verification_results"):
        assert set(Base.metadata.tables[table].columns.keys()) <= {
            c["name"] for c in inspector.get_columns(table)
        }
    with Session(e1.db) as s:
        assert s.get(RuntimeLock, 1) is not None
        # Never race with an unrelated live task if someone supplied a shared DB.
        if s.scalar(
            select(func.count()).select_from(Run).where(Run.status.in_(["QUEUED", "RUNNING"]))
        ):
            pytest.skip("Test database contains unrelated executable runs")
    owner = "pg-test-" + uuid.uuid4().hex
    try:
        project = e1.create_project(owner, "PostgreSQL race")
        snapshot = e1.create_snapshot(
            project["id"], {"repositories": [{"name": "fixture", "revision": "abc"}]}
        )
        run = e1.create_run(project["id"], snapshot["id"], "race")
        numbers = []
        for expected in range(1, 6):
            barrier = Barrier(2)

            def concurrent_claim(pair):
                engine, name = pair
                barrier.wait(timeout=10)
                return engine.claim_next(name)

            with ThreadPoolExecutor(2) as pool:
                claims = list(
                    pool.map(concurrent_claim, [(e1, "pg-worker-a"), (e2, "pg-worker-b")])
                )
            winners = [claim for claim in claims if claim is not None]
            assert len(winners) == 1
            winner = winners[0]
            assert winner["run"]["id"] == run["id"]
            numbers.append(winner["attempt"]["number"])
            assert winner["attempt"]["number"] == expected
            e1.finish_attempt(
                winner["attempt"]["id"],
                winner["attempt"]["generation"],
                outcome="FAILED",
                reason="REGRESSION",
                retryable=True,
            )
        assert numbers == [1, 2, 3, 4, 5]
        assert e2.claim_next("sixth") is None
        assert e1.get_run(run["id"])["reason"] == "RETRY_LIMIT_EXHAUSTED"
        with Session(e1.db) as s:
            step = s.execute(select(Step).where(Step.run_id == run["id"])).scalar_one()
            assert step.attempts_used == 5
            assert (
                s.scalar(
                    select(func.count()).select_from(Attempt).where(Attempt.step_id == step.id)
                )
                == 5
            )
        # A raw invalid value bypasses Python validators and still fails at the
        # metadata/type composite FK in PostgreSQL.
        with pytest.raises(IntegrityError):
            with transaction(e1.db) as s:
                s.add(
                    EntityParameterValue(
                        entity_id=project["id"],
                        owner_id=owner,
                        entity_type="project",
                        parameter_id="project.facts",
                        value_type="text",
                        value_text="wrong type",
                        is_null=False,
                        provenance={},
                    )
                )
    finally:
        cleanup_owned(e1, owner)
        e1.db.dispose()
        e2.db.dispose()


def test_postgres_recovery_fencing_and_atomic_command_journal():
    """Independent pools observe persisted fencing and rollback after an INSERT.

    No cloud dispatch takes place. The injected exception occurs after PostgreSQL
    executes an intent INSERT but before its transaction commits, so the second
    Engine must observe neither that intent nor its COMMAND_PREPARED event.
    """
    now = [1000.0]
    e1 = Engine(URL, clock=lambda: now[0])
    e2 = Engine(URL, clock=lambda: now[0])
    owner = "pg-recovery-" + uuid.uuid4().hex
    try:
        assert e1.db.dialect.name == "postgresql", "Use a PostgreSQL test URL"
        with e1.db.connect() as c1, e2.db.connect() as c2:
            assert c1.scalar(text("select pg_backend_pid()")) != c2.scalar(
                text("select pg_backend_pid()")
            )
        with Session(e1.db) as session:
            assert session.get(RuntimeLock, 1) is not None
            if session.scalar(
                select(func.count()).select_from(Run).where(Run.status.in_(["QUEUED", "RUNNING"]))
            ):
                pytest.skip("Test database contains unrelated executable runs")

        project = e1.create_project(owner, "PostgreSQL recovery")
        snapshot = e1.create_snapshot(project["id"], {"repositories": []})
        run = e1.create_run(project["id"], snapshot["id"], "recovery")
        claim = e1.claim_next("old-worker", lease_seconds=10)
        assert claim is not None and claim["run"]["id"] == run["id"]
        attempt_id, old_generation = claim["attempt"]["id"], claim["attempt"]["generation"]
        existing_id = "pg-command-" + uuid.uuid4().hex
        e1.prepare_command(attempt_id, old_generation, existing_id, {"operation": "synthetic"})
        e1.record_command_result(existing_id, old_generation, "RUNNING")

        now[0] += 11
        recovered = e2.recover_expired(run["id"], "replacement-worker", lease_seconds=60)
        generation = recovered["attempt"]["generation"]
        assert generation == old_generation + 1
        assert recovered["attempt"]["id"] == attempt_id
        assert recovered["step"]["attempts_used"] == 1
        with pytest.raises(DomainError, match="STALE_GENERATION"):
            e1.record_command_result(existing_id, old_generation, "SUCCEEDED", {"exit_code": 0})
        with pytest.raises(DomainError, match="STALE_GENERATION"):
            e1.prepare_command(attempt_id, old_generation, "stale-" + owner, {})
        assert e2.get_command(existing_id)["status"] == "RUNNING"
        with pytest.raises(DomainError, match="RECONCILIATION_REQUIRED"):
            e2.prepare_command(attempt_id, generation, "premature-" + owner, {})
        with pytest.raises(DomainError, match="EXECUTION_OUTCOME_UNKNOWN"):
            e2.finish_attempt(attempt_id, generation)

        # A current-generation readback is allowed; only reconciliation then
        # grants permission for a new intent under the recovered generation.
        e2.record_command_result(existing_id, generation, "SUCCEEDED", {"exit_code": 0})
        e2.confirm_reconciliation(attempt_id, generation)
        before_events = e2.events(run["id"])
        before_claim = e2.assert_current(attempt_id, generation)
        atomic_id = "pg-atomic-" + uuid.uuid4().hex
        inserted = []

        def interrupt_after_insert(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("INSERT INTO COMMANDS "):
                inserted.append(True)
                raise RuntimeError("synthetic failure after command INSERT before commit")

        event.listen(e1.db, "after_cursor_execute", interrupt_after_insert)
        try:
            with pytest.raises(RuntimeError, match="after command INSERT"):
                e1.prepare_command(attempt_id, generation, atomic_id, {"operation": "atomic-probe"})
        finally:
            event.remove(e1.db, "after_cursor_execute", interrupt_after_insert)
        assert inserted == [True], "The test must exercise a real PostgreSQL INSERT"
        with pytest.raises(DomainError, match="NOT_FOUND"):
            e2.get_command(atomic_id)
        assert e2.events(run["id"]) == before_events
        assert e2.assert_current(attempt_id, generation) == before_claim

        # The rolled-back identity can now be admitted once, proving that no
        # partial intent, fencing update or event survived on the first pool.
        accepted = e2.prepare_command(
            attempt_id, generation, atomic_id, {"operation": "atomic-probe"}
        )
        assert accepted["status"] == "PREPARED"
        assert e1.get_command(atomic_id)["generation"] == generation
        e2.record_command_result(atomic_id, generation, "SUCCEEDED", {"exit_code": 0})
        assert e2.finish_attempt(attempt_id, generation)["status"] == "AWAITING_HANDOFF"
        assert e1.list_steps(run["id"])[0]["attempts_used"] == 1
    finally:
        cleanup_owned(e1, owner)
        e1.db.dispose()
        e2.db.dispose()
