"""Negative controls for SOLID boundaries and recovery, not live-cloud qualification."""

import asyncio
import json
import threading
import time
from dataclasses import replace

import httpx
import pytest
from solid_model import ContractModel, as_model
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_adapters import profile
from test_api import create_source, headers
from test_api import system as api_system
from test_pipeline import RECIPE, advance_to_handoff
from test_pipeline import setup as pipeline_setup

from v2c.adapters.model import OpenAICompatibleModel
from v2c.contracts.model import (
    ModelRequest,
)
from v2c.contracts.operator import Acceptance
from v2c.contracts.stages import StageSpec
from v2c.engine import DomainError
from v2c.models import Attempt, Command, Step
from v2c.orchestration.blocking import run_blocking
from v2c.orchestration.registry import StageRegistry
from v2c.pipeline import Pipeline
from v2c.worker import run_once


@pytest.fixture
def setup(tmp_path):
    return pipeline_setup.__wrapped__(tmp_path)


@pytest.fixture
def system(tmp_path):
    yield from api_system.__wrapped__(tmp_path)


@pytest.mark.asyncio
async def test_independent_model_works_through_real_consumer(setup):
    engine, original, _, vms, run = setup
    model = ContractModel(RECIPE)
    pipeline = Pipeline(
        engine,
        original.store,
        as_model(model),
        settings=original.settings,
        vm_provider=vms,
        runner_factory=vms.runner,
    )
    assert await run_once(engine, pipeline, "contract-worker")
    inspected = engine.list_steps(run["id"])[0]
    assert inspected["status"] == "SUCCEEDED"
    assert inspected["result"]["recipe"] == RECIPE
    assert len(model.calls) == 1
    assert engine.get_run(run["id"])["limits"]["model_profile"] == {"version": "contract-v1"}
    assert not any(hasattr(model, name) for name in ("profile", "request_body", "is_test"))
    assert vms.purposes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("implementation", ["independent", "http"])
async def test_shared_model_contract(implementation, monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "contract-only-secret")
    observed = []

    def response(request):
        observed.append(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(RECIPE)}, "finish_reason": "stop"}]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        model = (
            ContractModel(RECIPE)
            if implementation == "independent"
            else OpenAICompatibleModel(profile(), lambda _: "contract-only-secret", client)
        )
        request = ModelRequest(request_id="contract1", task="describe", context="source")
        assert model.readiness().ready
        description = model.describe()
        assert description.manifest["version"] == description.version
        prepared = model.prepare(request)
        assert prepared == model.prepare(request)
        assert prepared.size_bytes == len(prepared.body)
        assert b"contract-only-secret" not in prepared.body
        result = await model.generate(request)
        assert result.finish_reason == "stop"
        assert result.usage.total_tokens is None
        assert not client.is_closed
        if implementation == "http":
            assert observed == [prepared.body]


@pytest.mark.asyncio
async def test_registered_extension_needs_no_model_or_vm(setup):
    engine, original, _, vms, run = setup
    first = engine.claim_next("bootstrap")
    engine.add_step(
        run["id"],
        "local_probe",
        "local_probe",
        attempt_id=first["attempt"]["id"],
        generation=first["attempt"]["generation"],
    )
    engine.finish_attempt(first["attempt"]["id"], first["attempt"]["generation"], {})
    calls = []

    async def local_stage(claim):
        calls.append(claim["step"]["kind"])
        return {"final_digest": "c" * 64, "acceptance": [], "human_handoff": "probe"}

    pipeline = Pipeline(
        engine, original.store, None, extra_stages=[StageSpec("local_probe", local_stage)]
    )
    assert await run_once(engine, pipeline, "extension-worker")
    assert calls == ["local_probe"]
    assert engine.list_steps(run["id"])[1]["status"] == "SUCCEEDED"
    assert vms.purposes == []


def test_registry_rejects_invalid_composition_before_admission():
    calls = []

    async def handler(claim):
        calls.append(claim)

    stage = StageSpec("local", handler)
    with pytest.raises(DomainError, match="DUPLICATE_STAGE_KIND"):
        StageRegistry([stage, stage], {})
    with pytest.raises(DomainError, match="STAGE_CAPABILITY_UNAVAILABLE"):
        StageRegistry([replace(stage, requirements=("model",))], {})
    with pytest.raises(DomainError, match="INVALID_STAGE_KIND"):
        StageRegistry([replace(stage, kind="../unsafe")], {})
    assert calls == []


@pytest.mark.asyncio
async def test_missing_model_and_persisted_unknown_stage_release_slot(setup):
    engine, original, _, vms, run = setup
    no_model = Pipeline(engine, original.store, None)
    assert await run_once(engine, no_model, "no-model")
    assert engine.get_run(run["id"])["reason"] == "MODEL_PROFILE_REQUIRED"
    assert vms.purposes == []
    engine.resume_run(run["id"])
    with Session(engine.db) as session:
        step = session.scalar(select(Step).where(Step.run_id == run["id"]))
        step.kind = "unsupported"
        session.commit()
    assert await run_once(engine, no_model, "unknown-kind")
    assert engine.get_run(run["id"])["reason"] == "UNKNOWN_STEP_KIND"
    other = engine.create_run(run["project_id"], run["snapshot_id"], "other")
    assert engine.claim_next("next")["run"]["id"] == other["id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("cwd", "../outside"),
        ("cwd", "/outside"),
        ("argv", ["bad\x00arg"]),
        ("argv", ["x" * 100001]),
    ],
)
def test_invalid_acceptance_rejected_before_state_change(system, field, value):
    client, engine, _, _ = system
    project, snapshot = create_source(client)
    run = engine.create_run(project, snapshot["id"], "invalid-input")
    before = engine.get_run(run["id"])
    check = {"argv": ["python", "-V"], "cwd": ".", field: value}
    response = client.post(
        f"/runs/{run['id']}/acceptance",
        headers=headers(),
        json={"task": "owner task", "checks": [check]},
    )
    assert response.status_code == 422
    assert engine.get_run(run["id"]) == before
    with Session(engine.db) as session:
        assert session.scalar(select(Command)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_effect", [False, True])
async def test_validation_error_classification_accounts_for_unknown_effect(setup, unknown_effect):
    engine, original, _, _, run = setup

    async def invalid(claim):
        if unknown_effect:
            engine.prepare_command(
                claim["attempt"]["id"],
                claim["attempt"]["generation"],
                "effect-unknown",
                {"operation": "extension"},
            )
        Acceptance.model_validate({"task": "x", "checks": [{"argv": ["x"], "cwd": "../outside"}]})

    pipeline = Pipeline(
        engine, original.store, None, extra_stages=[StageSpec("validate_probe", invalid)]
    )
    with Session(engine.db) as session:
        step = session.scalar(select(Step).where(Step.run_id == run["id"]))
        step.kind = "validate_probe"
        session.commit()
    assert await run_once(engine, pipeline, "validation-worker")
    state = engine.get_run(run["id"])
    assert state["reason"] == "INVALID_STAGE_INPUT"
    assert state["status"] == ("RUNNING" if unknown_effect else "NEEDS_INPUT")
    with Session(engine.db) as session:
        attempt = session.scalar(select(Attempt))
        assert attempt.status == ("ACTIVE" if unknown_effect else "WAITING")
    if unknown_effect:
        assert engine.claim_next("forbidden-retry") is None
        assert engine.events(run["id"])[-1]["kind"] == "EXECUTION_PENDING"
    else:
        engine.resume_run(run["id"])
        assert engine.claim_next("corrected")["attempt"]["number"] == 2


@pytest.mark.asyncio
async def test_slow_store_does_not_starve_real_worker_heartbeat(setup, monkeypatch):
    engine, pipeline, _, _, run = setup
    pulses = []
    original_heartbeat = engine.heartbeat

    def heartbeat(*args, **kwargs):
        pulses.append(time.monotonic())
        return original_heartbeat(*args, **kwargs)

    monkeypatch.setattr(engine, "heartbeat", heartbeat)
    original_put = pipeline.store.put_bytes

    def slow_put(*args, **kwargs):
        time.sleep(1.2)
        return original_put(*args, **kwargs)

    monkeypatch.setattr(pipeline.store, "put_bytes", slow_put)
    assert await run_once(engine, pipeline, "heartbeat-worker", lease_seconds=1)
    assert len(pulses) >= 2
    assert engine.list_steps(run["id"])[0]["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_cancel_waits_for_local_producer_and_never_continues_publication():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    published = []

    def blocking():
        entered.set()
        assert release.wait(timeout=5)
        finished.set()
        return "stale"

    async def producer():
        result = await run_blocking(blocking)
        published.append(result)

    task = asyncio.create_task(producer())
    while not entered.is_set():
        await asyncio.sleep(0.005)
    try:
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not finished.is_set()
        task.cancel()  # repeated cancellation must not detach the thread
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and published == []


@pytest.mark.asyncio
async def test_legacy_invalid_handoff_input_is_recoverable_without_vm(setup):
    engine, pipeline, _, vms, run = setup
    await advance_to_handoff(engine, pipeline, run)
    before = list(vms.purposes)
    step = next(step for step in engine.list_steps(run["id"]) if step["kind"] == "ai_handoff")
    engine.supply_input(
        run["id"],
        step["id"],
        {"task": "owner task", "checks": [{"id": "legacy", "argv": ["x"], "cwd": "../outside"}]},
    )
    assert await run_once(engine, pipeline, "legacy-validation")
    assert engine.get_run(run["id"])["reason"] == "INVALID_STAGE_INPUT"
    assert engine.get_run(run["id"])["status"] == "NEEDS_INPUT"
    assert vms.purposes == before
    engine.supply_input(
        run["id"],
        step["id"],
        {"task": "owner task", "checks": [{"id": "corrected", "argv": ["x"], "cwd": "."}]},
    )
    assert engine.get_run(run["id"])["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_packaging_uses_only_its_control_and_source_ports(setup, tmp_path):
    from test_pipeline import advance_to_plan

    from v2c.orchestration.packaging import PackagingOperations

    engine, pipeline, _, _, run = setup
    await advance_to_plan(engine, pipeline, run)
    for _ in range(2):  # transform and verify, leaving package queued
        assert await run_once(engine, pipeline, "prepare-package")
    original = pipeline.sources

    class PackagingControl:
        def verifications(self, run_id):
            return engine.verifications(run_id)

        def record_artifact(self, *args, **kwargs):
            return engine.record_artifact(*args, **kwargs)

    class PackagingSources:
        def required_result(self, *args):
            return original.required_result(*args)

        def source(self, claim):
            return original.source(claim)

        def artifact_bytes(self, ref):
            return original.artifact_bytes(ref)

        def completed(self, *args):
            return original.completed(*args)

    packaging = PackagingOperations(
        PackagingControl(), pipeline.store, PackagingSources(), tmp_path / "only-work-root"
    )
    claim = engine.claim_next("minimal-package")
    assert claim["step"]["kind"] == "package"
    result = packaging.build(claim)
    assert result["final_digest"] == original.required_result(run["id"], "verify")["final_digest"]
    assert pipeline.store.get_bytes(result["bundle"]["key"])


@pytest.mark.asyncio
async def test_cancelling_worker_accounts_for_blocking_producer(setup):
    engine, original, _, _, run = setup
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    continued = []

    def block():
        entered.set()
        assert release.wait(timeout=5)
        finished.set()

    async def handler(claim):
        await run_blocking(block)
        continued.append(True)

    with Session(engine.db) as session:
        step = session.scalar(select(Step).where(Step.run_id == run["id"]))
        step.kind = "blocking_probe"
        session.commit()
    pipeline = Pipeline(
        engine, original.store, None, extra_stages=[StageSpec("blocking_probe", handler)]
    )
    supervisor = asyncio.create_task(run_once(engine, pipeline, "cancel-worker"))
    while not entered.is_set():
        await asyncio.sleep(0.005)
    try:
        supervisor.cancel()
        await asyncio.sleep(0.02)
        assert not supervisor.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await supervisor
    assert finished.is_set() and continued == []
    # Cancellation is not proof of a successful stage or a stopped remote effect.
    assert engine.list_steps(run["id"])[0]["status"] != "SUCCEEDED"


@pytest.mark.asyncio
async def test_late_blocking_producer_cannot_publish_after_generation_loss(setup):
    engine, pipeline, _, _, run = setup
    clock = [time.time()]
    engine.clock = lambda: clock[0]
    claim = engine.claim_next("old-worker", lease_seconds=1)
    entered, release = threading.Event(), threading.Event()

    def produce():
        entered.set()
        assert release.wait(timeout=5)
        return engine.record_artifact(
            run["id"],
            "late-probe",
            "objects/stale",
            "a" * 64,
            1,
            {},
            verified=True,
            attempt_id=claim["attempt"]["id"],
            generation=claim["attempt"]["generation"],
        )

    task = asyncio.create_task(run_blocking(produce))
    while not entered.is_set():
        await asyncio.sleep(0.005)
    try:
        clock[0] += 2
        successor = engine.recover_expired(run["id"], "new-worker", stop_confirmed=True)
        assert successor["attempt"]["generation"] > claim["attempt"]["generation"]
    finally:
        release.set()
    with pytest.raises(DomainError, match="STALE_GENERATION"):
        await task
    assert engine.artifacts(run["id"]) == []


def test_released_stage_admission_policies_are_preserved(setup):
    _, pipeline, _, _, _ = setup
    assert {kind: spec.requirements for kind, spec in pipeline.executor.registry.specs.items()} == {
        "inspect": ("model",),
        "baseline": ("model",),
        "plan": ("model",),
        "transform": ("model",),
        "verify": ("model",),
        "package": (),
        "ai_handoff": ("model",),
    }


def test_model_readiness_uses_injected_resolver_without_environment_precondition(monkeypatch):
    monkeypatch.delenv("TEST_MODEL_KEY", raising=False)
    seen = []

    def resolve(reference):
        seen.append(reference)
        return "synthetic-local-credential"

    model = OpenAICompatibleModel(profile(), resolve)
    assert model.readiness().ready
    assert seen == ["env:TEST_MODEL_KEY"]
    assert "synthetic-local-credential" not in json.dumps(model.describe().manifest)
    assert not OpenAICompatibleModel(profile(), lambda _: "").readiness().ready
