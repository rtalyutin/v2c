"""Independent contract checks at API/workflow and late model response boundaries."""

import io
import time
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from v2c.adapters.model import ModelResult, ModelUsage
from v2c.api import create_app
from v2c.config import Settings
from v2c.engine import Engine
from v2c.pipeline import Pipeline, RunCancelled, StepFailure
from v2c.storage import LocalArtifactStore
from v2c.worker import run_once


@pytest.fixture
def state(tmp_path):
    e = Engine(f"sqlite:///{tmp_path}/qa.db")
    e.init_schema()
    p = e.create_project("alice", "QA project")
    sn = e.create_snapshot(p["id"], {"synthetic": True})
    r = e.create_run(p["id"], sn["id"], "qa-run")
    return e, p, sn, r


class ReachedMaintainer(Exception):
    pass


@pytest.mark.asyncio
async def test_valid_operator_acceptance_check_reaches_maintainer(state, tmp_path):
    """A payload accepted by the public API must be consumable by its worker."""
    e, p, sn, r = state
    body = {
        "task": "Add one field to the report.",
        "checks": [{"argv": ["python3", "-c", "assert True"]}],
    }
    e.add_step(r["id"], "ai_handoff", "ai_handoff")
    initial = e.claim_next("qa-worker")
    e.set_waiting(
        initial["attempt"]["id"],
        initial["attempt"]["generation"],
        "NEEDS_INPUT",
        "HELD_OUT_TASK_REQUIRED",
    )
    captured = {}

    def supply(run_id, step_id, payload):
        captured.update(payload)
        return e.get_run(run_id)

    e.supply_input = supply
    settings = Settings(
        database_url="sqlite://",
        operator_tokens={"alice": "a" * 32},
        work_root=tmp_path / "work",
        artifact_root=tmp_path / "artifacts",
    )
    with TestClient(
        create_app(settings, engine=e, store=LocalArtifactStore(settings.artifact_root))
    ) as client:
        response = client.post(
            f"/runs/{r['id']}/acceptance",
            json=body,
            headers={"Authorization": "Bearer " + "a" * 32},
        )
        assert response.status_code == 200
    payload = captured
    pipeline = Pipeline(e, None)
    pipeline._required_result = lambda *args: {"checkpoint": {}, "final_digest": "f" * 64}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w"):
        pass
    pipeline._artifact_bytes = lambda _: stream.getvalue()
    pipeline._source = lambda _: SimpleNamespace(repositories=[])
    pipeline._delivery_instructions = lambda _: {}

    async def maintainer(*args, **kwargs):
        raise ReachedMaintainer()

    pipeline._model = maintainer
    with pytest.raises(ReachedMaintainer):
        await pipeline._stage_ai_handoff({"run": r, "step": {"payload": payload}})


class SimpleOutput(BaseModel):
    answer: str


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["invalid", "cancel_after_response"])
async def test_paid_model_response_keeps_usage_before_rejection(state, response_kind):
    """Invalid output/cancellation cannot erase an already observed usage receipt."""
    e, _, _, r = state
    claim = e.claim_next("qa-worker", lease_seconds=120)

    class Model:
        is_test = True

        async def generate(self, request):
            if response_kind == "cancel_after_response":
                e.request_cancel(r["id"])
            return ModelResult(
                text="fixture",
                structured={} if response_kind == "invalid" else {"answer": "done"},
                finish_reason="stop",
                provider_request_id="qa-paid-request",
                usage=ModelUsage(input_tokens=12, output_tokens=7, total_tokens=19),
            )

    pipeline = Pipeline(e, None, model=Model())
    with pytest.raises((RunCancelled, StepFailure)):
        await pipeline._model(claim, "probe", "Return a result", {}, SimpleOutput)
    receipts = e.usage(r["id"])
    assert len(receipts) == 1
    assert receipts[0]["usage"]["provider_request_id"] == "qa-paid-request"
    assert receipts[0]["usage"]["usage"]["total_tokens"] == 19


@pytest.mark.asyncio
async def test_model_only_worker_crash_pauses_unknown_without_redispatch(state):
    """Crash after durable request intent cannot deadlock or resend an unknown paid call."""
    e, _, _, r = state
    now = time.time()
    e.clock = lambda: now
    claim = e.claim_next("vanished-worker", lease_seconds=10)

    class CrashedModel:
        is_test = True
        calls = 0

        async def generate(self, request):
            self.calls += 1
            raise RuntimeError("synthetic worker process loss after provider acceptance")

    model = CrashedModel()
    pipeline = Pipeline(e, None, model=model)
    with pytest.raises(RuntimeError, match="synthetic worker"):
        await pipeline._model(claim, "recipe", "Return a recipe", {}, SimpleOutput)
    assert model.calls == 1
    e.clock = lambda: now + 11
    await run_once(e, pipeline, "new-worker", lease_seconds=10)
    current = e.get_run(r["id"])
    assert current["status"] == "NEEDS_INPUT"
    assert current["reason"] == "MODEL_OUTCOME_UNKNOWN"
    assert model.calls == 1
    assert e.list_steps(r["id"])[0]["attempts_used"] == 1
    receipts = e.usage(r["id"])
    assert len(receipts) == 1 and receipts[0]["usage"]["usage_unknown"] is True


def test_object_authorization_covers_every_run_read_and_write(state, tmp_path):
    e, p, sn, r = state
    foreign = e.create_project("bob", "Foreign project")
    settings = Settings(
        database_url="sqlite://",
        operator_tokens={"alice": "a" * 32, "bob": "b" * 32},
        work_root=tmp_path / "work",
        artifact_root=tmp_path / "artifacts",
    )
    artifact = e.record_artifact(r["id"], "bundle", "synthetic.zip", "a" * 64, 1, verified=True)
    headers = {"Authorization": "Bearer " + "b" * 32}
    with TestClient(
        create_app(settings, engine=e, store=LocalArtifactStore(settings.artifact_root))
    ) as client:
        for path in (
            f"/projects/{p['id']}",
            f"/projects/{p['id']}/snapshots",
            f"/projects/{p['id']}/runs",
            f"/runs/{r['id']}",
            f"/runs/{r['id']}/events",
            f"/runs/{r['id']}/plan",
            f"/runs/{r['id']}/artifacts",
            f"/runs/{r['id']}/artifacts/{artifact['id']}",
        ):
            assert client.get(path, headers=headers).status_code == 404, path
        for path, payload in (
            (f"/runs/{r['id']}/cancel", {}),
            (f"/runs/{r['id']}/resume", {}),
            (
                f"/runs/{r['id']}/decisions",
                {"snapshot_id": sn["id"], "plan_revision": 1, "approved": True},
            ),
            (f"/runs/{r['id']}/acceptance", {"task": "held out", "checks": [{"argv": ["true"]}]}),
            (
                f"/runs/{r['id']}/handoff",
                {"final_digest": "f" * 64, "passed": True, "evidence": "independent QA evidence"},
            ),
        ):
            assert client.post(path, json=payload, headers=headers).status_code == 404, path
        assert (
            client.post(
                f"/projects/{foreign['id']}/runs",
                headers=headers,
                json={"snapshot_id": sn["id"], "idempotency_key": "foreign-attempt"},
            ).status_code
            == 409
        )
    assert not e.get_run(r["id"])["cancel_requested"]


def test_invalid_archive_never_publishes_snapshot_or_escapes_workspace(state, tmp_path):
    e, p, _, _ = state
    settings = Settings(
        database_url="sqlite://",
        operator_tokens={"alice": "a" * 32},
        work_root=tmp_path / "work",
        artifact_root=tmp_path / "artifacts",
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../../escaped.txt", "fixture")
    headers = {"Authorization": "Bearer " + "a" * 32, "Content-Type": "application/zip"}
    before = e.list_snapshots(p["id"])
    with TestClient(
        create_app(settings, engine=e, store=LocalArtifactStore(settings.artifact_root))
    ) as client:
        assert (
            client.post(
                f"/projects/{p['id']}/imports", content=stream.getvalue(), headers=headers
            ).status_code
            == 422
        )
    assert e.list_snapshots(p["id"]) == before
    assert not list(tmp_path.rglob("escaped.txt"))


def test_revised_plan_insertion_cannot_reset_existing_work_budget(state):
    """New accepted work may precede old work without giving the old work five fresh attempts."""
    e, p, sn, r = state
    first = e.claim_next("qa-worker")
    old_work = {
        "id": "old-work",
        "title": "Original transformation",
        "files": ["src.py"],
        "reason": "same obstacle",
    }
    initial_plan = e.publish_plan(r["id"], {"steps": [old_work]})
    e.set_waiting(
        first["attempt"]["id"], first["attempt"]["generation"], "AWAITING_PLAN_APPROVAL", "PLAN"
    )
    e.decide_plan(r["id"], p["id"], sn["id"], initial_plan["revision"], True, "alice")
    for _ in range(3):
        claim = e.claim_next("qa-worker")
        e.finish_attempt(
            claim["attempt"]["id"],
            claim["attempt"]["generation"],
            outcome="FAILED",
            reason="REGRESSION",
            retryable=True,
        )
    fourth = e.claim_next("qa-worker")
    assert fourth["attempt"]["number"] == 4
    revised = e.publish_plan(
        r["id"],
        {
            "steps": [
                {
                    "id": "new-work",
                    "title": "New prerequisite",
                    "files": ["helper.py"],
                    "reason": "new obstacle",
                },
                old_work,
            ]
        },
    )
    e.set_waiting(
        fourth["attempt"]["id"], fourth["attempt"]["generation"], "AWAITING_PLAN_APPROVAL", "REPLAN"
    )
    try:
        e.decide_plan(r["id"], p["id"], sn["id"], revised["revision"], True, "alice")
    except Exception as exc:
        # A documented safe refusal to reorder work also preserves the contract.
        from v2c.engine import DomainError

        assert isinstance(exc, DomainError)
        assert exc.code == "PLAN_WORK_IDENTITY_CONFLICT"
        assert e.get_step(fourth["step"]["id"])["attempts_used"] == 4
        assert e.get_step(fourth["step"]["id"])["payload"]["work_id"] == "old-work"
        assert len([s for s in e.list_steps(r["id"]) if s["kind"] == "transform"]) == 1
        return
    previous = [
        s
        for s in e.list_steps(r["id"])
        if s["kind"] == "transform" and s["payload"].get("work_id") == "old-work"
    ]
    assert len(previous) == 1
    assert previous[0]["id"] == fourth["step"]["id"]
    assert previous[0]["attempts_used"] == 4
