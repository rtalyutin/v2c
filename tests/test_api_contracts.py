"""Independent JSON Schema checks of the operator's observable wire responses."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, ValidationError

from v2c.api import create_app, public
from v2c.config import Settings
from v2c.engine import Engine
from v2c.storage import LocalArtifactStore

TOKEN = "contract-operator-" + "a" * 32
HEADERS = {"Authorization": "Bearer " + TOKEN}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def contract_system(tmp_path):
    engine = Engine(f"sqlite:///{tmp_path / 'contracts.db'}")
    engine.init_schema()
    settings = Settings(
        api_token=TOKEN, work_root=tmp_path / "work", artifact_root=tmp_path / "objects"
    )
    store = LocalArtifactStore(settings.artifact_root)
    with TestClient(create_app(settings, engine=engine, store=store)) as client:
        yield client, engine, store
    engine.db.dispose()


def response_validator(document, path, method="get", status=200, mime="application/json"):
    schema = document["paths"][path][method]["responses"][str(status)]["content"][mime]["schema"]
    # jsonschema is independent from Pydantic/FastAPI serialization. Keep the
    # root components in scope for document-local OpenAPI JSON references.
    return Draft202012Validator({**schema, "components": document["components"]})


def test_all_success_contracts_are_explicit_and_download_is_binary(contract_system):
    client, _, _ = contract_system
    document = client.get("/openapi.json").json()
    successes = []
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            for code, response in operation["responses"].items():
                if code.startswith("2"):
                    successes.append((path, method))
                    for media in response["content"].values():
                        assert media["schema"], (path, method)
    assert len(successes) == 19
    download = document["paths"]["/runs/{run_id}/artifacts/{artifact_id}"]["get"]["responses"][
        "200"
    ]
    assert download["content"] == {
        "application/zip": {"schema": {"type": "string", "format": "binary"}}
    }
    assert "/web/{filename}" not in document["paths"]


def test_observed_resources_and_explicit_nulls_validate(contract_system):
    client, engine, _ = contract_system
    document = client.get("/openapi.json").json()
    project = client.post("/projects", json={"name": "Schema fixture"}, headers=HEADERS).json()
    snapshot = engine.create_snapshot(
        project["id"], {"label": "fixture", "nested": {"token": "private", "keep": None}}
    )
    run = client.post(
        f"/projects/{project['id']}/runs",
        json={"snapshot_id": snapshot["id"], "idempotency_key": "schema"},
        headers=HEADERS,
    ).json()
    resource_paths = [
        ("/healthz", "/healthz"),
        ("/readyz", "/readyz"),
        ("/projects", "/projects"),
        (f"/projects/{project['id']}", "/projects/{project_id}"),
        (f"/projects/{project['id']}/snapshots", "/projects/{project_id}/snapshots"),
        (f"/projects/{project['id']}/runs", "/projects/{project_id}/runs"),
        (f"/runs/{run['id']}", "/runs/{run_id}"),
        (f"/runs/{run['id']}/events", "/runs/{run_id}/events"),
        (f"/runs/{run['id']}/plan", "/runs/{run_id}/plan"),
        (f"/runs/{run['id']}/artifacts", "/runs/{run_id}/artifacts"),
    ]
    for actual, template in resource_paths:
        response = client.get(actual, headers=HEADERS)
        assert response.status_code == 200
        response_validator(document, template).validate(response.json())
    observed = client.get(f"/runs/{run['id']}", headers=HEADERS).json()
    for name in (
        "parent_run_id",
        "reason",
        "original_reason",
        "stage",
        "checkpoint_id",
        "final_digest",
    ):
        assert name in observed and observed[name] is None
    assert client.get(f"/runs/{run['id']}/plan", headers=HEADERS).json() is None
    assert client.get(f"/projects/{project['id']}/snapshots", headers=HEADERS).json()[0][
        "manifest"
    ] == {"label": "fixture", "nested": {"keep": None}}


def test_schema_rejects_wrong_or_missing_fields(contract_system):
    client, engine, _ = contract_system
    project = engine.create_project("operator", "Typed output")
    document = client.get("/openapi.json").json()
    validator = response_validator(document, "/projects/{project_id}")
    valid = client.get(f"/projects/{project['id']}", headers=HEADERS).json()
    validator.validate(valid)
    for key, replacement in (("id", 12), ("created_at", "yesterday")):
        broken = {**valid, key: replacement}
        with pytest.raises(ValidationError):
            validator.validate(broken)
    missing = copy.deepcopy(valid)
    del missing["owner_id"]
    with pytest.raises(ValidationError):
        validator.validate(missing)


def test_new_internal_column_is_not_an_accidental_public_field(contract_system, monkeypatch):
    client, engine, _ = contract_system
    project = engine.create_project("operator", "Stable fields")
    original = engine.get_project
    monkeypatch.setattr(
        engine, "get_project", lambda key: {**original(key), "new_internal_column": "private"}
    )
    result = client.get(f"/projects/{project['id']}", headers=HEADERS)
    assert result.status_code == 200
    assert result.json() == project


def test_error_envelopes_validate_for_auth_validation_and_state(contract_system):
    client, engine, _ = contract_system
    document = client.get("/openapi.json").json()
    cases = [
        (client.get("/projects"), "/projects", "get", 401),
        (client.post("/projects", json={"name": False}, headers=HEADERS), "/projects", "post", 422),
        (client.get("/runs/absent", headers=HEADERS), "/runs/{run_id}", "get", 404),
    ]
    project = engine.create_project("operator", "Conflict")
    snapshot = engine.create_snapshot(project["id"], {})
    run = engine.create_run(project["id"], snapshot["id"], "conflict")
    cases.append(
        (
            client.post(f"/runs/{run['id']}/resume", headers=HEADERS),
            "/runs/{run_id}/resume",
            "post",
            409,
        )
    )
    for response, path, method, status in cases:
        assert response.status_code == status
        response_validator(document, path, method, status).validate(response.json())


def test_repository_import_schema_uses_the_same_strict_request_model(contract_system):
    client, _, _ = contract_system
    document = client.get("/openapi.json").json()
    content = document["paths"]["/projects/{project_id}/imports"]["post"]["requestBody"]["content"]
    assert content["application/octet-stream"]["schema"]["format"] == "binary"
    validator = Draft202012Validator(
        {**content["application/json"]["schema"], "components": document["components"]}
    )
    repository = {
        "name": "example",
        "url": "https://git.example.test/repo.git",
        "revision": "a" * 40,
        "secret_ref": None,
    }
    validator.validate({"repositories": [repository]})
    for change in ({"revision": "main"}, {"name": "../../escape"}, {"unexpected": "value"}):
        with pytest.raises(ValidationError):
            validator.validate({"repositories": [{**repository, **change}]})


def test_public_redaction_keeps_json_null_and_secret_filtering():
    assert public(
        {
            "nullable": None,
            "token": "private",
            "nested": [{"root": "/private", "value": "ghp_" + "a" * 30}],
        }
    ) == {
        "nullable": None,
        "nested": [{"value": "[REDACTED]"}],
    }


def test_export_freshness_and_stale_negative_control(tmp_path):
    command = [sys.executable, str(ROOT / "scripts/export_openapi.py")]
    assert subprocess.run([*command, "--check"], cwd=ROOT, capture_output=True).returncode == 0
    target = tmp_path / "contract.json"
    subprocess.run([*command, "--output", str(target)], cwd=ROOT, check=True, capture_output=True)
    assert json.loads(target.read_text())["openapi"].startswith("3.")
    target.write_text("{}\n")
    stale = subprocess.run(
        [*command, "--output", str(target), "--check"], cwd=ROOT, capture_output=True, text=True
    )
    assert stale.returncode == 1
    assert "stale" in stale.stdout
