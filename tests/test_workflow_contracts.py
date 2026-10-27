"""Behavior oracles for the separated workflow and persistence boundaries.

Expected states come from the approved retry/cancel contract. Fault injection
checks transaction effects instead of only checking the pure policy helper.
"""

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from v2c.engine import DomainError, Engine
from v2c.models import Command, Entity, Event, RuntimeLock
from v2c.workflow.states import (
    ATTEMPT_STATES,
    COMMAND_STATES,
    COMMAND_TERMINAL,
    RUN_STATES,
    STEP_STATES,
)


@pytest.fixture
def workflow(tmp_path):
    engine = Engine(f"sqlite:///{tmp_path / 'workflow.db'}", clock=lambda: 1000.0)
    engine.init_schema()
    project = engine.create_project("maintainer", "Workflow")
    snapshot = engine.create_snapshot(project["id"], {"repositories": []})
    run = engine.create_run(project["id"], snapshot["id"], "workflow-contract")
    yield engine, run
    engine.db.dispose()


@pytest.mark.parametrize(
    "reason,retryable,attempts,expected_step,expected_run,expected_reason",
    [
        ("REGRESSION", True, 1, "PENDING", "QUEUED", "REGRESSION"),
        ("REGRESSION", True, 4, "PENDING", "QUEUED", "REGRESSION"),
        ("REGRESSION", True, 5, "FAILED", "FAILED", "RETRY_LIMIT_EXHAUSTED"),
        ("AUTH", True, 1, "WAITING", "NEEDS_INPUT", "AUTH"),
        ("INVALID_CONFIG", True, 1, "WAITING", "NEEDS_INPUT", "INVALID_CONFIG"),
        ("ACCESS_DENIED", True, 1, "WAITING", "NEEDS_INPUT", "ACCESS_DENIED"),
        ("MISSING_INPUT", False, 1, "WAITING", "NEEDS_INPUT", "MISSING_INPUT"),
        ("UNSUPPORTED", True, 1, "FAILED", "FAILED", "UNSUPPORTED"),
        ("UNSUPPORTED_CAPABILITY", True, 1, "FAILED", "FAILED", "UNSUPPORTED_CAPABILITY"),
        ("PLAN_REJECTED", True, 1, "FAILED", "FAILED", "PLAN_REJECTED"),
        ("CANCELLED", True, 1, "FAILED", "FAILED", "CANCELLED"),
        ("REGRESSION", False, 1, "FAILED", "FAILED", "REGRESSION"),
        (None, False, 1, "FAILED", "FAILED", "STEP_FAILED"),
    ],
)
def test_completion_transitions_preserve_retry_budget(
    workflow, reason, retryable, attempts, expected_step, expected_run, expected_reason
):
    engine, run = workflow
    for number in range(1, attempts + 1):
        claim = engine.claim_next("worker")
        assert claim["attempt"]["number"] == number
        final = number == attempts
        result = engine.finish_attempt(
            claim["attempt"]["id"],
            claim["attempt"]["generation"],
            outcome="FAILED",
            reason=reason if final else "REGRESSION",
            retryable=retryable if final else True,
        )
    step = engine.list_steps(run["id"])[0]
    assert (step["status"], result["status"], result["reason"]) == (
        expected_step,
        expected_run,
        expected_reason,
    )
    assert step["attempts_used"] == attempts
    assert len([e for e in engine.events(run["id"]) if e["kind"] == "ATTEMPT_FINISHED"]) == attempts
    # Repeating the same final observation cannot spend another attempt or event.
    before = engine.events(run["id"])
    repeated = engine.finish_attempt(
        claim["attempt"]["id"],
        claim["attempt"]["generation"],
        outcome="FAILED",
        reason=reason,
        retryable=retryable,
    )
    assert repeated == result
    assert engine.events(run["id"]) == before


@pytest.mark.parametrize("pending", [False, True])
def test_success_queues_only_real_pending_work(workflow, pending):
    engine, run = workflow
    claim = engine.claim_next("worker")
    if pending:
        engine.add_step(run["id"], "baseline", "baseline")
    result = engine.finish_attempt(claim["attempt"]["id"], claim["attempt"]["generation"])
    assert result["status"] == ("QUEUED" if pending else "AWAITING_HANDOFF")
    assert result["status"] != "READY"


@pytest.mark.parametrize("state", ["NEEDS_INPUT", "AWAITING_PLAN_APPROVAL", "AWAITING_HANDOFF"])
def test_waiting_states_close_attempt_without_becoming_ready(workflow, state):
    engine, run = workflow
    claim = engine.claim_next("worker")
    result = engine.set_waiting(
        claim["attempt"]["id"], claim["attempt"]["generation"], state, "INPUT_REQUIRED"
    )
    assert result["status"] == state
    assert engine.list_steps(run["id"])[0]["status"] == "WAITING"
    with pytest.raises(DomainError, match="STALE_GENERATION"):
        engine.prepare_command(claim["attempt"]["id"], claim["attempt"]["generation"], "late", {})
    if state == "NEEDS_INPUT":
        assert engine.resume_run(run["id"])["status"] == "QUEUED"
    else:
        with pytest.raises(DomainError, match="RUN_NOT_RESUMABLE"):
            engine.resume_run(run["id"])


def test_invalid_wait_leaves_claim_and_lock_unchanged(workflow):
    engine, run = workflow
    claim = engine.claim_next("worker")
    with pytest.raises(DomainError, match="INVALID_WAIT_STATUS"):
        engine.set_waiting(claim["attempt"]["id"], claim["attempt"]["generation"], "READY", "BAD")
    assert engine.assert_current(claim["attempt"]["id"], claim["attempt"]["generation"]) == claim
    with Session(engine.db) as session:
        assert session.get(RuntimeLock, 1).active_run_id == run["id"]


def test_command_intent_and_event_roll_back_together(workflow):
    engine, run = workflow
    claim = engine.claim_next("worker")
    before = engine.events(run["id"])

    def fail_event(session, _flush_context, _instances):
        if any(isinstance(row, Event) and row.kind == "COMMAND_PREPARED" for row in session.new):
            raise RuntimeError("synthetic event persistence failure")

    event.listen(Session, "before_flush", fail_event)
    try:
        with pytest.raises(RuntimeError, match="synthetic event"):
            engine.prepare_command(
                claim["attempt"]["id"], claim["attempt"]["generation"], "atomic", {"argv": ["true"]}
            )
    finally:
        event.remove(Session, "before_flush", fail_event)
    with Session(engine.db) as session:
        assert session.get(Command, "atomic") is None
    assert engine.events(run["id"]) == before
    assert engine.assert_current(claim["attempt"]["id"], claim["attempt"]["generation"]) == claim


def test_metadata_validation_failure_rolls_back_entity_and_previous_values(workflow):
    engine, _ = workflow
    with pytest.raises(DomainError, match="UNKNOWN_PARAMETER"):
        engine.create_entity("maintainer", "component", {"name": "temporary", "not-defined": "bad"})
    with Session(engine.db) as session:
        assert list(session.scalars(select(Entity).where(Entity.entity_type == "component"))) == []


def test_cancel_requires_stop_then_atomically_fences_and_stops_commands(workflow):
    engine, run = workflow
    claim = engine.claim_next("worker")
    aid, gen = claim["attempt"]["id"], claim["attempt"]["generation"]
    engine.prepare_command(aid, gen, "remote", {"operation": "synthetic"})
    engine.request_cancel(run["id"])
    with pytest.raises(DomainError, match="STOP_NOT_CONFIRMED"):
        engine.confirm_cancel(run["id"])
    assert engine.get_run(run["id"])["status"] == "RUNNING"
    assert engine.get_command("remote")["status"] == "PREPARED"
    assert engine.confirm_cancel(run["id"], stop_confirmed=True)["status"] == "CANCELLED"
    assert engine.get_command("remote")["status"] == "STOPPED"
    assert engine.list_steps(run["id"])[0]["generation"] == gen + 1
    with pytest.raises(DomainError, match="STALE_GENERATION"):
        engine.record_command_result("remote", gen, "SUCCEEDED", {"exit_code": 0})
    before = engine.events(run["id"])
    engine.confirm_cancel(run["id"], stop_confirmed=True)
    assert engine.events(run["id"]) == before
    with Session(engine.db) as session:
        assert session.get(RuntimeLock, 1).active_run_id is None


def test_state_vocabularies_do_not_conflate_wire_and_control_states():
    assert "READY" in RUN_STATES and "READY" not in STEP_STATES
    assert "ACTIVE" in ATTEMPT_STATES and "ACTIVE" not in RUN_STATES
    assert "STOPPED" in COMMAND_STATES and "STOPPED" in COMMAND_TERMINAL
    assert "STOPPED" not in RUN_STATES | STEP_STATES | ATTEMPT_STATES
