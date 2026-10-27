"""Independent black-box wire probe, runnable against baseline and candidate."""

from __future__ import annotations

import base64
import itertools
import json
import tempfile
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from v2c.api import create_app
from v2c.config import Settings
from v2c.engine import Engine
from v2c.models import Run, Step
from v2c.storage import LocalArtifactStore

counter = itertools.count(1)
uuid.uuid4 = lambda: uuid.UUID(int=next(counter))
responses = {}
token = "qa-only-" + "a" * 32
other = "qa-only-" + "b" * 32

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    engine = Engine("sqlite:///" + str(root / "state.sqlite3"), clock=lambda: 1700000000.0)
    engine.init_schema()
    settings = Settings(
        database_url="sqlite:///" + str(root / "state.sqlite3"),
        api_token=token,
        operator_tokens={"other": other},
        work_root=root / "work",
        artifact_root=root / "objects",
    )
    store = LocalArtifactStore(settings.artifact_root)
    with TestClient(create_app(settings, engine=engine, store=store)) as client:

        def record(name, method, path, *, authenticated=True, **kwargs):
            headers = kwargs.pop("headers", {})
            if authenticated:
                headers.setdefault("Authorization", "Bearer " + token)
            response = client.request(method, path, headers=headers, **kwargs)
            try:
                body = response.json()
            except ValueError:
                body = {"base64": base64.b64encode(response.content).decode()}
            responses[name] = {
                "status": response.status_code,
                "body": body,
                "headers": {
                    key: response.headers.get(key)
                    for key in (
                        "content-type",
                        "cache-control",
                        "www-authenticate",
                        "content-disposition",
                        "x-content-type-options",
                    )
                },
            }
            return body

        record("health", "GET", "/healthz", authenticated=False)
        record("ready", "GET", "/readyz", authenticated=False)
        record("unauthorized", "GET", "/projects", authenticated=False)
        project = record("new_project", "POST", "/projects", json={"name": "QA project"})
        pid = project["id"]
        record("projects", "GET", "/projects")
        record("project", "GET", "/projects/" + pid)
        record(
            "other_owner", "GET", "/projects/" + pid, headers={"Authorization": "Bearer " + other}
        )
        snap = engine.create_snapshot(
            pid,
            {
                "repositories": [{"name": "main", "revision": "a" * 40}],
                "workspace_root": "/must/not/leak",
                "optional": None,
                "facts": {"token": "must-not-leak", "secret": "must-not-leak", "value": "kept"},
            },
        )
        record("snapshots", "GET", f"/projects/{pid}/snapshots")
        payload = {"snapshot_id": snap["id"], "idempotency_key": "qa-idempotency"}
        run = record("new_run", "POST", f"/projects/{pid}/runs", json=payload)
        rid = run["id"]
        record("same_run", "POST", f"/projects/{pid}/runs", json=payload)
        record("runs", "GET", f"/projects/{pid}/runs")
        record("run", "GET", "/runs/" + rid)
        record("plan_absent", "GET", f"/runs/{rid}/plan")
        record("events_initial", "GET", f"/runs/{rid}/events")
        record("resume_rejected", "POST", f"/runs/{rid}/resume")
        record(
            "acceptance_rejected",
            "POST",
            f"/runs/{rid}/acceptance",
            json={"task": "QA change", "checks": [{"argv": ["python3", "-V"]}]},
        )
        record(
            "decision_rejected",
            "POST",
            f"/runs/{rid}/decisions",
            json={"snapshot_id": snap["id"], "plan_revision": 1, "approved": True},
        )
        claim = engine.claim_next("qa-worker")
        attempt = claim["attempt"]
        plan = {
            "summary": "Existing code preserved",
            "findings": [],
            "steps": [],
            "limitations": [],
        }
        published = engine.publish_plan(
            rid, plan, attempt_id=attempt["id"], generation=attempt["generation"]
        )
        engine.set_waiting(
            attempt["id"],
            attempt["generation"],
            "AWAITING_PLAN_APPROVAL",
            "PLAN_DECISION_REQUIRED",
            {"plan_revision": published["revision"]},
        )
        record("plan", "GET", f"/runs/{rid}/plan")
        record(
            "approve",
            "POST",
            f"/runs/{rid}/decisions",
            json={
                "snapshot_id": snap["id"],
                "plan_revision": published["revision"],
                "approved": True,
            },
        )
        record(
            "approve_duplicate",
            "POST",
            f"/runs/{rid}/decisions",
            json={
                "snapshot_id": snap["id"],
                "plan_revision": published["revision"],
                "approved": True,
            },
        )
        record("cancel", "POST", f"/runs/{rid}/cancel")
        engine.confirm_cancel(rid, stop_confirmed=True)
        record("cancelled_run", "GET", "/runs/" + rid)
        data = b"PK\x03\x04 synthetic QA artifact"
        ref = store.put_bytes("qa/package.zip", data)
        artifact = engine.record_artifact(
            rid,
            "diagnostic",
            ref.key,
            ref.sha256,
            ref.size,
            {"optional": None, "nested": {"api_key": "must-not-leak", "value": "kept"}},
            True,
        )
        record("artifacts", "GET", f"/runs/{rid}/artifacts")
        record("download", "GET", f"/runs/{rid}/artifacts/{artifact['id']}")
        record(
            "artifact_foreign",
            "GET",
            f"/runs/{rid}/artifacts/{artifact['id']}",
            headers={"Authorization": "Bearer " + other},
        )

        # Persisted legacy payloads can contain explicit null and nested metadata.
        second = engine.create_run(pid, snap["id"], "acceptance-fixture")
        heldout = engine.add_step(second["id"], "ai_handoff", "ai_handoff")
        with Session(engine.db) as session:
            state = session.get(Run, second["id"])
            state.status = "NEEDS_INPUT"
            state.reason = "HELD_OUT_TASK_REQUIRED"
            step = session.get(Step, heldout["id"])
            step.status = "WAITING"
            session.commit()
        record(
            "acceptance",
            "POST",
            f"/runs/{second['id']}/acceptance",
            json={
                "task": "Add a simple helper",
                "checks": [{"argv": ["python3", "-V"]}],
                "allowed_new_files": [],
            },
        )
        with Session(engine.db) as session:
            state = session.get(Run, second["id"])
            state.status = "AWAITING_HANDOFF"
            state.final_digest = "b" * 64
            session.commit()
        record(
            "human_failed",
            "POST",
            f"/runs/{second['id']}/handoff",
            json={
                "final_digest": "b" * 64,
                "passed": False,
                "evidence": "Independent QA synthetic negative receipt",
            },
        )
        record(
            "human_digest_rejected",
            "POST",
            f"/runs/{second['id']}/handoff",
            json={
                "final_digest": "c" * 64,
                "passed": True,
                "evidence": "Independent QA wrong version receipt",
            },
        )
        record("events_final", "GET", f"/runs/{rid}/events")
    engine.db.dispose()

print(json.dumps(responses, sort_keys=True, indent=2))
