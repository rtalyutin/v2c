"""Independent state oracles for V2C-01..08, typed EAV and handoff gates."""
import concurrent.futures
import os

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from v2c.engine import DomainError, Engine, REQUIRED_VERIFICATIONS
from v2c.models import Attempt, EntityParameterValue


class Clock:
    def __init__(self):
        self.now = 1000.0
    def __call__(self):
        return self.now


@pytest.fixture
def state(tmp_path):
    clock = Clock()
    engine = Engine(f"sqlite:///{tmp_path}/state.db", clock=clock)
    engine.init_schema()
    p = engine.create_project("owner", "Project")
    sn = engine.create_snapshot(p["id"], {"repositories": [{"name": "code", "revision": "abc"}]})
    run = engine.create_run(p["id"], sn["id"], "request-1")
    return engine, clock, p, sn, run


def claim(state):
    return state[0].claim_next("worker", lease_seconds=10)


def finish(engine, c, **kw):
    return engine.finish_attempt(c["attempt"]["id"], c["attempt"]["generation"], **kw)


def test_v2c_01_fifth_success_persisted_budget(state):
    e, _, _, _, run = state
    observed = []
    for n in range(1, 6):
        c = claim(state)
        observed.append(c["attempt"]["number"])
        finish(e, c, outcome="FAILED" if n < 5 else "SUCCEEDED", reason="REGRESSION" if n < 5 else None, retryable=True, result={"candidate": n})
    assert observed == [1, 2, 3, 4, 5]
    assert e.claim_next("another") is None
    step = e.list_steps(run["id"])[0]
    assert step["attempts_used"] == 5 and step["result"] == {"candidate": 5}
    with Session(e.db) as s:
        assert len(list(s.scalars(select(Attempt)))) == 5


def test_v2c_02_exhaustion_keeps_original_failure(state):
    e, _, _, _, run = state
    for _ in range(5):
        finish(e, claim(state), outcome="FAILED", reason="REGRESSION", retryable=True)
    final = e.get_run(run["id"])
    assert (final["status"], final["reason"], final["original_reason"]) == ("FAILED", "RETRY_LIMIT_EXHAUSTED", "REGRESSION")
    assert e.claim_next("worker") is None
    assert len([x for x in e.events(run["id"]) if x["kind"] == "ATTEMPT_STARTED"]) == 5


def test_v2c_03_command_saved_before_dispatch_and_replayed(state):
    e, clock, _, _, run = state
    c = claim(state)
    aid, gen = c["attempt"]["id"], c["attempt"]["generation"]
    request = {"argv": ["pytest"], "vm": "vm-1"}
    first = e.prepare_command(aid, gen, "cmd-1", request)
    assert first["status"] == "PREPARED"
    assert e.prepare_command(aid, gen, "cmd-1", request)["id"] == "cmd-1"
    with pytest.raises(DomainError, match="COMMAND_ID_CONFLICT"):
        e.prepare_command(aid, gen, "cmd-1", {"argv": ["rm"]})
    # Runner accepted before lost response. After restart inspect SAME saved ID.
    clock.now += 11
    recovered = e.recover_expired(run["id"], "new-worker")
    assert recovered["attempt"]["id"] == aid
    assert recovered["step"]["attempts_used"] == 1
    assert e.commands_for_attempt(aid)[0]["id"] == "cmd-1"
    e.record_command_result("cmd-1", recovered["attempt"]["generation"], "SUCCEEDED", {"exit_code": 0})
    e.confirm_reconciliation(aid, recovered["attempt"]["generation"])
    finish(e, recovered)
    assert e.list_steps(run["id"])[0]["attempts_used"] == 1


def test_v2c_04_lease_expiry_is_not_command_stop(state):
    e, clock, _, _, run = state
    c = claim(state)
    e.prepare_command(c["attempt"]["id"], 1, "cmd", {"vm": "old"})
    e.record_command_result("cmd", 1, "RUNNING")
    clock.now += 11
    assert e.claim_next("new-worker") is None
    recovered = e.recover_expired(run["id"], "new-worker")
    gen = recovered["attempt"]["generation"]
    with pytest.raises(DomainError, match="RECONCILIATION_REQUIRED"):
        e.prepare_command(c["attempt"]["id"], gen, "cmd-new", {"vm": "new"})
    with pytest.raises(DomainError, match="EXECUTION_OUTCOME_UNKNOWN"):
        finish(e, recovered, outcome="FAILED", retryable=True, reason="TRANSPORT_OR_TIMEOUT")
    e.confirm_reconciliation(c["attempt"]["id"], gen, stop_confirmed=True)
    finish(e, recovered, outcome="FAILED", retryable=True, reason="TRANSPORT_OR_TIMEOUT")
    assert claim(state)["attempt"]["number"] == 2


def test_v2c_05_stale_generation_cannot_publish_or_issue(state):
    e, clock, _, _, run = state
    c = claim(state)
    clock.now += 11
    recovered = e.recover_expired(run["id"], "new-worker", stop_confirmed=True)
    assert recovered["attempt"]["generation"] == 2
    for operation in [lambda: finish(e, c), lambda: e.prepare_command(c["attempt"]["id"], 1, "old-command", {}), lambda: e.record_artifact(run["id"], "bundle", "x", "a" * 64, 1, verified=True, attempt_id=c["attempt"]["id"], generation=1)]:
        with pytest.raises(DomainError, match="STALE_GENERATION"):
            operation()
    finish(e, recovered, result={"accepted": "new-worker"})
    assert e.list_steps(run["id"])[0]["result"] == {"accepted": "new-worker"}


def test_v2c_06_renamed_plan_step_keeps_four_used_attempts(state):
    e, _, p, sn, run = state
    initial = claim(state)
    e.publish_plan(run["id"], {"steps": [{"id": "old-name", "title": "Refactor"}]})
    e.set_waiting(initial["attempt"]["id"], 1, "AWAITING_PLAN_APPROVAL", "PLAN")
    e.decide_plan(run["id"], p["id"], sn["id"], 1, True, "operator")
    for _ in range(4):
        finish(e, claim(state), outcome="FAILED", reason="REGRESSION", retryable=True)
    step = e.list_steps(run["id"])[1]
    assert step["attempts_used"] == 4
    e.publish_plan(run["id"], {"steps": [{"id": "old-name", "title": "Same work renamed"}]})
    # Put controller at approval wait without creating another execution attempt.
    from v2c.db import transaction
    from v2c.models import Run
    with transaction(e.db) as s:
        s.get(Run, run["id"]).status = "AWAITING_PLAN_APPROVAL"
    e.decide_plan(run["id"], p["id"], sn["id"], 2, True, "operator")
    revised = e.list_steps(run["id"])[1]
    assert revised["id"] == step["id"] and revised["attempts_used"] == 4
    assert claim(state)["attempt"]["number"] == 5


@pytest.mark.parametrize("reason", ["MISSING_INPUT", "AUTH", "ACCESS_DENIED", "INVALID_CONFIG"])
def test_v2c_07_nonretryable_stops_without_spending_remaining(state, reason):
    e, _, _, _, run = state
    finish(e, claim(state), outcome="FAILED", reason=reason, retryable=True)
    assert e.get_run(run["id"])["status"] == "NEEDS_INPUT"
    assert e.claim_next("w") is None
    assert e.list_steps(run["id"])[0]["attempts_used"] == 1


def test_v2c_08_cancel_waits_for_stop_and_retains_usage(state):
    e, _, _, _, run = state
    c = claim(state)
    aid = c["attempt"]["id"]
    e.prepare_command(aid, 1, "cmd", {"vm": "vm"})
    e.request_cancel(run["id"])
    assert e.get_run(run["id"])["status"] == "RUNNING"
    with pytest.raises(DomainError, match="RUN_NOT_EXECUTABLE"):
        e.prepare_command(aid, 1, "next", {})
    with pytest.raises(DomainError, match="STOP_NOT_CONFIRMED"):
        e.confirm_cancel(run["id"])
    e.record_usage(run["id"], "provider-req", "provider", {"input_tokens": None, "cost": None, "delivery": "UNKNOWN"}, aid)
    e.confirm_cancel(run["id"], stop_confirmed=True)
    assert e.get_run(run["id"])["status"] == "CANCELLED"
    assert e.usage(run["id"])[0]["usage"]["cost"] is None
    assert e.claim_next("w") is None


def test_idempotent_intent_and_snapshot_project(state):
    e, _, p, sn, run = state
    assert e.create_run(p["id"], sn["id"], "request-1")["id"] == run["id"]
    with pytest.raises(DomainError, match="IDEMPOTENCY_CONFLICT"):
        e.create_run(p["id"], sn["id"], "request-1", limits={"time": 1})
    foreign = e.create_project("other", "Other")
    with pytest.raises(DomainError, match="SNAPSHOT_PROJECT_MISMATCH"):
        e.create_run(foreign["id"], sn["id"], "key")


def test_single_active_run_and_wait_releases_worker(state):
    e, _, p, sn, _ = state
    other = e.create_run(p["id"], sn["id"], "request-2")
    first = claim(state)
    assert e.claim_next("another") is None
    e.set_waiting(first["attempt"]["id"], 1, "NEEDS_INPUT", "MISSING_INPUT")
    assert claim(state)["run"]["id"] != first["run"]["id"]


def test_concurrent_claims_only_one_active_file_sqlite(state):
    e = state[0]
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        claims = list(pool.map(lambda i: e.claim_next(f"worker-{i}"), range(4)))
    assert len([c for c in claims if c is not None]) == 1


def test_approval_is_bound_to_exact_snapshot_and_plan(state):
    e, _, p, sn, run = state
    c = claim(state)
    e.publish_plan(run["id"], {"steps": []})
    e.set_waiting(c["attempt"]["id"], 1, "AWAITING_PLAN_APPROVAL", "PLAN")
    with pytest.raises(DomainError, match="PLAN_VERSION_MISMATCH"):
        e.decide_plan(run["id"], p["id"], "another-snapshot", 1, True, "operator")
    e.publish_plan(run["id"], {"steps": [{"id": "new"}]})
    with pytest.raises(DomainError, match="PLAN_VERSION_MISMATCH"):
        e.decide_plan(run["id"], p["id"], sn["id"], 1, True, "operator")


def test_eav_type_tenant_and_missing_null_constraints(state):
    e, _, p, _, _ = state
    with pytest.raises(DomainError, match="PARAMETER_TYPE_MISMATCH"):
        e.create_entity("owner", "component", {"name": 42})
    foreign = e.create_project("foreign", "Other")
    with pytest.raises(DomainError, match="REFERENCE_OWNER_MISMATCH"):
        e.create_entity("owner", "component", {"project": foreign["id"]})
    entity = e.create_entity("owner", "component", {"name": None, "project": p["id"]})
    with Session(e.db) as s:
        explicit_null = s.get(EntityParameterValue, (entity["id"], "component.name"))
        assert explicit_null.is_null is True
        assert s.get(EntityParameterValue, (entity["id"], "component.facts")) is None
    with pytest.raises(IntegrityError):
        with Session(e.db) as s:
            s.add(EntityParameterValue(entity_id=entity["id"], parameter_id="component.facts", owner_id="owner", entity_type="component", value_type="text", is_null=False, value_text="wrong metadata", provenance={}))
            s.commit()


def test_v2c_12_ready_needs_independent_all_kinds_and_same_bundle(state):
    e, _, _, _, run = state
    finish(e, claim(state))
    d = "f" * 64
    e.record_artifact(run["id"], "bundle", "bundle.zip", "a" * 64, 10, {"final_digest": d}, verified=True)
    e.record_verification(run["id"], "project_PASS_file", True, {"text": "PASS"}, d)
    with pytest.raises(DomainError, match="HANDOFF_NOT_VERIFIED"):
        e.complete_handoff(run["id"], d)
    for kind in REQUIRED_VERIFICATIONS - {"human_handoff"}:
        e.record_verification(run["id"], kind, True, {"trusted": True}, d)
    e.record_verification(run["id"], "human_handoff", True, {"human": "reviewer"}, "different-digest")
    with pytest.raises(DomainError, match="HANDOFF_NOT_VERIFIED"):
        e.complete_handoff(run["id"], d)
    e.record_verification(run["id"], "human_handoff", True, {"human": "reviewer"}, d)
    with pytest.raises(DomainError, match="HANDOFF_BUNDLE_NOT_SEALED"):
        e.complete_handoff(run["id"], d)
    receipts = {v["kind"]: v["id"] for v in e.verifications(run["id"]) if v["kind"] in REQUIRED_VERIFICATIONS and v["final_digest"] == d}
    e.record_artifact(run["id"], "bundle", "sealed-bundle.zip", "b" * 64, 10,
                      {"final_digest": d, "status": "VERIFIED", "sealed_receipts": receipts}, verified=True)
    assert e.complete_handoff(run["id"], d)["status"] == "READY"


def test_checkpoint_requires_complete_state_recipe_and_separate_restore_flag(state):
    e, _, _, _, run = state
    incomplete = e.record_artifact(run["id"], "checkpoint", "c1", "c" * 64, 10, {"repositories": []}, verified=True)
    with pytest.raises(DomainError, match="INCOMPLETE_CHECKPOINT"):
        e.confirm_checkpoint(run["id"], incomplete["id"])
    artifact = e.record_artifact(run["id"], "checkpoint", "c2", "d" * 64, 10, {"repositories": ["abc"], "plan_revision": 0, "environment": {}, "files": {}, "services": {"restore": "migrations_and_fixtures"}}, verified=True)
    confirmed = e.confirm_checkpoint(run["id"], artifact["id"])
    assert confirmed["verified"] and not confirmed["restore_verified"]
    assert e.confirm_checkpoint(run["id"], artifact["id"], restore_verified=True)["restore_verified"]


def test_same_attempt_result_duplicate_is_readback_not_new_attempt(state):
    e, _, _, _, run = state
    c = claim(state)
    first = finish(e, c, result={"accepted": True})
    duplicate = finish(e, c, result={"accepted": True})
    assert first["status"] == duplicate["status"] == "AWAITING_HANDOFF"
    assert e.list_steps(run["id"])[0]["attempts_used"] == 1
    with pytest.raises(DomainError, match="ATTEMPT_RESULT_CONFLICT"):
        finish(e, c, result={"accepted": False})


def test_latest_verification_failure_overrides_prior_pass_at_same_clock(state):
    e, _, _, _, run = state
    finish(e, claim(state))
    d = "f" * 64
    e.record_artifact(run["id"], "bundle", "bundle", "a" * 64, 1, {"final_digest": d}, verified=True)
    for kind in REQUIRED_VERIFICATIONS:
        e.record_verification(run["id"], kind, True, {"reviewer": "external"}, d)
    e.record_verification(run["id"], "final_regression", False, {"failure": "real regression"}, d)
    with pytest.raises(DomainError, match="HANDOFF_NOT_VERIFIED"):
        e.complete_handoff(run["id"], d)
    assert e.get_run(run["id"])["status"] == "AWAITING_HANDOFF"


def test_new_project_property_uses_metadata_definition_not_schema_change(state):
    e, _, p, _, _ = state
    e.define_parameter("project", "requires_postgres", "boolean")
    e.set_entity_value(p["id"], "requires_postgres", True, {"source": "pyproject.toml", "revision": "abc"})
    readback = e.get_entity(p["id"])
    assert readback["values"]["requires_postgres"] is True
    assert readback["provenance"]["requires_postgres"]["revision"] == "abc"
    with pytest.raises(DomainError, match="PARAMETER_DEFINITION_CONFLICT"):
        e.define_parameter("project", "requires_postgres", "text")


@pytest.mark.parametrize("revision", [
    [],
    [{"id": "B", "title": "New work"}, {"id": "A", "title": "Original"}],
    [{"id": "renamed-id", "title": "Original"}],
])
def test_revised_plan_cannot_shift_or_rename_machine_work_identity(state, revision):
    e, _, p, sn, run = state
    initial = claim(state)
    e.publish_plan(run["id"], {"steps": [{"id": "A", "title": "Original"}]})
    e.set_waiting(initial["attempt"]["id"], 1, "AWAITING_PLAN_APPROVAL", "PLAN")
    e.decide_plan(run["id"], p["id"], sn["id"], 1, True, "operator")
    for _ in range(3):
        finish(e, claim(state), outcome="FAILED", reason="REGRESSION", retryable=True)
    fourth = claim(state)
    assert fourth["attempt"]["number"] == 4
    e.publish_plan(run["id"], {"steps": revision})
    e.set_waiting(fourth["attempt"]["id"], fourth["attempt"]["generation"], "AWAITING_PLAN_APPROVAL", "REPLAN")
    with pytest.raises(DomainError, match="PLAN_WORK_IDENTITY_CONFLICT"):
        e.decide_plan(run["id"], p["id"], sn["id"], 2, True, "operator")
    transforms = [step for step in e.list_steps(run["id"]) if step["kind"] == "transform"]
    assert len(transforms) == 1
    assert transforms[0]["attempts_used"] == 4
    assert transforms[0]["payload"]["work_id"] == "A"
    assert e.get_run(run["id"])["status"] == "AWAITING_PLAN_APPROVAL"


def test_revised_plan_can_append_approved_new_work_preserving_old_identity(state):
    e, _, p, sn, run = state
    first = claim(state)
    e.publish_plan(run["id"], {"steps": [{"id": "A", "title": "Original"}]})
    e.set_waiting(first["attempt"]["id"], 1, "AWAITING_PLAN_APPROVAL", "PLAN")
    e.decide_plan(run["id"], p["id"], sn["id"], 1, True, "operator")
    old = claim(state)
    e.publish_plan(run["id"], {"steps": [{"id": "A", "title": "Renamed title"}, {"id": "B", "title": "New approved work"}]})
    e.set_waiting(old["attempt"]["id"], 1, "AWAITING_PLAN_APPROVAL", "REPLAN")
    e.decide_plan(run["id"], p["id"], sn["id"], 2, True, "operator")
    steps = [step for step in e.list_steps(run["id"]) if step["kind"] == "transform"]
    assert [step["payload"]["work_id"] for step in steps] == ["A", "B"]
    assert [step["attempts_used"] for step in steps] == [1, 0]
    assert steps[0]["id"] == old["step"]["id"]


def test_late_usage_after_recovery_and_cancel_refines_unknown_without_double_count(state):
    e, _, _, _, run = state
    active = claim(state)
    aid = active["attempt"]["id"]
    e.record_usage(run["id"], "late-response", "provider-profile-v1", {"usage_unknown": True, "delivery_unknown": True}, aid)
    e.request_cancel(run["id"])
    e.confirm_cancel(run["id"], stop_confirmed=True)
    refined = e.record_usage(run["id"], "late-response", "provider-profile-v1", {"usage": {"input_tokens": 12, "output_tokens": 4, "cost": None}, "provider_request_id": "provider-123"}, aid)
    assert refined["usage"]["usage"]["input_tokens"] == 12
    assert refined["usage"]["usage"]["cost"] is None
    assert refined["usage"]["delivery_unknown"] is False
    assert len(e.usage(run["id"])) == 1
    history = [event for event in e.events(run["id"]) if event["kind"] == "USAGE_REFINED"]
    assert history[0]["payload"]["before"] == {"usage_unknown": True, "delivery_unknown": True}
    # A later vague observation must not erase the actual counts or delivery.
    e.record_usage(run["id"], "late-response", "provider-profile-v1", {"usage_unknown": True, "delivery_unknown": True}, aid)
    final = e.usage(run["id"])[0]["usage"]
    assert final["usage"]["input_tokens"] == 12 and final["delivery_unknown"] is False
    with pytest.raises(DomainError, match="USAGE_ID_CONFLICT"):
        e.record_usage(run["id"], "late-response", "provider-profile-v1", {"usage": {"input_tokens": 99}}, aid)


def test_sealed_bundle_receipts_must_match_latest_verdicts_not_only_code_digest(state):
    e, _, _, _, run = state
    finish(e, claim(state))
    code_digest = "a" * 64
    receipts = {kind: e.record_verification(run["id"], kind, True, {"independent": True}, code_digest)["id"] for kind in REQUIRED_VERIFICATIONS}
    e.record_artifact(run["id"], "bundle", "sealed-v1.zip", "b" * 64, 1,
                      {"final_digest": code_digest, "status": "VERIFIED", "sealed_receipts": receipts}, verified=True)
    # A repeated human acceptance is a new receipt. The old archive can no
    # longer attest which latest acceptance decision it contains.
    new_human = e.record_verification(run["id"], "human_handoff", True, {"reviewer": "new-check"}, code_digest)
    with pytest.raises(DomainError, match="HANDOFF_BUNDLE_NOT_SEALED"):
        e.complete_handoff(run["id"], code_digest)
    receipts["human_handoff"] = new_human["id"]
    e.record_artifact(run["id"], "bundle", "sealed-v2.zip", "c" * 64, 1,
                      {"final_digest": code_digest, "status": "VERIFIED", "sealed_receipts": receipts}, verified=True)
    assert e.complete_handoff(run["id"], code_digest)["status"] == "READY"
    with pytest.raises(DomainError, match="FINAL_VERSION_MISMATCH"):
        e.record_artifact(run["id"], "bundle", "wrong-version.zip", "d" * 64, 1,
                          {"final_digest": "e" * 64, "status": "VERIFIED", "sealed_receipts": receipts}, verified=True)


def test_new_required_receipt_invalidates_previously_ready_sealed_bundle(state):
    e, _, _, _, run = state
    finish(e, claim(state))
    code_digest = "a" * 64
    receipts = {kind: e.record_verification(run["id"], kind, True, {"independent": True}, code_digest)["id"] for kind in REQUIRED_VERIFICATIONS}
    e.record_artifact(run["id"], "bundle", "sealed.zip", "b" * 64, 1,
                      {"final_digest": code_digest, "status": "VERIFIED", "sealed_receipts": receipts}, verified=True)
    e.complete_handoff(run["id"], code_digest)
    e.record_verification(run["id"], "human_handoff", True, {"reviewer": "new-receipt"}, code_digest)
    assert e.get_run(run["id"])["status"] == "AWAITING_HANDOFF"
    with pytest.raises(DomainError, match="HANDOFF_BUNDLE_NOT_SEALED"):
        e.complete_handoff(run["id"], code_digest)


def test_first_empty_approved_plan_goes_to_verification_without_source_patch(state):
    e, _, project, snapshot, run = state
    inspected = claim(state)
    published = e.publish_plan(run["id"], {"steps": [], "summary": "Existing source needs no changes"})
    e.set_waiting(inspected["attempt"]["id"], 1, "AWAITING_PLAN_APPROVAL", "PLAN")
    approved = e.decide_plan(run["id"], project["id"], snapshot["id"], published["revision"], True, "operator")
    assert approved["status"] == "QUEUED"
    assert not [step for step in e.list_steps(run["id"]) if step["kind"] == "transform"]
    next_work = claim(state)
    assert next_work["step"]["kind"] == "verify"
    assert next_work["step"]["payload"]["no_transform"] is True
    assert next_work["step"]["attempts_used"] == 1


def test_full_model_profile_bound_before_dispatch_and_same_version_changes_rejected(state):
    e, _, _, _, run = state
    active = claim(state)
    fence = {"attempt_id": active["attempt"]["id"], "generation": active["attempt"]["generation"]}
    profile = {"version": "1", "adapter_type": "openai-compatible", "endpoint": "https://provider.example/v1",
               "model": "model-a", "secret_ref": "env:MODEL_KEY", "capabilities": ["structured", "text"],
               "max_input_bytes": 1000, "max_output_tokens": 100, "max_response_bytes": 1000, "timeout_seconds": 10}
    first = e.bind_model_profile(run["id"], profile, **fence)
    assert first["capabilities"] == ["structured", "text"]
    assert e.bind_model_profile(run["id"], {**profile, "capabilities": ["text", "structured"]}, **fence) == first
    for replacement in [{"endpoint": "https://different.example/v1"}, {"model": "model-b"}, {"secret_ref": "env:OTHER_KEY"}, {"max_output_tokens": 200}]:
        with pytest.raises(DomainError, match="MODEL_PROFILE_CHANGED"):
            e.bind_model_profile(run["id"], {**profile, **replacement}, **fence)
    assert e.get_run(run["id"])["limits"]["model_profile"] == first
    bound_events = [event for event in e.events(run["id"]) if event["kind"] == "MODEL_PROFILE_BOUND"]
    assert len(bound_events) == 1 and set(bound_events[0]["payload"]) == {"profile_hash", "version"}


def test_profile_binding_respects_selected_version_and_generation(state):
    e, clock, p, sn, run = state
    configured = e.create_run(p["id"], sn["id"], "selected-profile", {"model_profile_version": "chosen-v1"})
    # Queue ordering is immaterial; wait the original fixture run first.
    e.request_cancel(run["id"])
    e.confirm_cancel(run["id"], stop_confirmed=True)
    active = claim(state)
    assert active["run"]["id"] == configured["id"]
    fence = {"attempt_id": active["attempt"]["id"], "generation": 1}
    with pytest.raises(DomainError, match="MODEL_PROFILE_CHANGED"):
        e.bind_model_profile(configured["id"], {"version": "different"}, **fence)
    assert "model_profile" not in e.get_run(configured["id"])["limits"]
    clock.now += 11
    e.recover_expired(configured["id"], "new-worker", stop_confirmed=True)
    with pytest.raises(DomainError, match="STALE_GENERATION"):
        e.bind_model_profile(configured["id"], {"version": "chosen-v1"}, **fence)
