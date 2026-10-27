import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from v2c.api import create_app
from v2c.config import Settings
from v2c.engine import Engine
from v2c.storage import LocalArtifactStore

TOKEN = "operator-test-token-" + "a" * 24
OTHER = "operator-test-token-" + "b" * 24


@pytest.fixture
def system(tmp_path):
    engine = Engine(f"sqlite:///{tmp_path / 'api.db'}")
    engine.init_schema()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        api_token=TOKEN,
        operator_tokens={"other": OTHER},
        work_root=tmp_path / "work",
        artifact_root=tmp_path / "objects",
    )
    store = LocalArtifactStore(settings.artifact_root)
    app = create_app(settings, engine=engine, store=store)
    with TestClient(app) as client:
        yield client, engine, settings, store
    engine.db.dispose()


def headers(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def archive(files=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        for path, body in (
            files or {"app.py": "print('hello')\n", "README.md": "A project\n"}
        ).items():
            z.writestr(path, body)
    return output.getvalue()


def create_source(client):
    p = client.post("/projects", json={"name": "Booking"}, headers=headers())
    assert p.status_code == 201
    pid = p.json()["id"]
    snap = client.post(f"/projects/{pid}/imports", content=archive(), headers=headers())
    assert snap.status_code == 201, snap.text
    return pid, snap.json()


def test_operator_auth_and_cross_owner_isolation(system):
    c, _, _, _ = system
    assert c.get("/projects").status_code == 401
    pid, snap = create_source(c)
    assert c.get(f"/projects/{pid}", headers=headers(OTHER)).status_code == 404
    assert c.get(f"/projects/{pid}/snapshots", headers=headers(OTHER)).status_code == 404
    assert (
        c.post(
            f"/projects/{pid}/runs",
            json={"snapshot_id": snap["id"], "idempotency_key": "x"},
            headers=headers(OTHER),
        ).status_code
        == 404
    )
    assert c.get("/projects", headers=headers(OTHER)).json() == []


def test_import_is_durable_and_no_internal_paths_exposed(system):
    c, e, _, store = system
    pid, public_snapshot = create_source(c)
    assert "workspace_root" not in public_snapshot["manifest"]
    actual = e.get_snapshot(public_snapshot["id"])
    m = actual["manifest"]
    assert store.get_bytes(m["archive_key"])
    assert m["source_git_bundles"]
    for ref in m["source_git_bundles"].values():
        assert store.get_bytes(ref["key"])
    run = c.post(
        f"/projects/{pid}/runs",
        json={"snapshot_id": actual["id"], "idempotency_key": "same"},
        headers=headers(),
    )
    assert run.status_code == 201
    again = c.post(
        f"/projects/{pid}/runs",
        json={"snapshot_id": actual["id"], "idempotency_key": "same"},
        headers=headers(),
    )
    assert run.json()["id"] == again.json()["id"]
    assert run.json()["limits"]["max_attempts_per_step"] == 5


def test_unsafe_import_and_body_limit(system, tmp_path):
    c, _, settings, _ = system
    pid = c.post("/projects", json={"name": "Bad input"}, headers=headers()).json()["id"]
    response = c.post(
        f"/projects/{pid}/imports", content=archive({"../escape.py": "bad"}), headers=headers()
    )
    assert response.status_code == 422
    settings.max_upload_bytes = 32
    response = c.post(f"/projects/{pid}/imports", content=b"x" * 33, headers=headers())
    assert response.status_code == 413
    assert not (tmp_path / "escape.py").exists()


def test_strict_input_and_foreign_snapshot(system):
    c, _, _, _ = system
    pid, snapshot = create_source(c)
    other = c.post("/projects", json={"name": "Another"}, headers=headers()).json()["id"]
    r = c.post(
        f"/projects/{other}/runs",
        json={"snapshot_id": snapshot["id"], "idempotency_key": "x"},
        headers=headers(),
    )
    assert r.status_code == 409
    r = c.post(
        f"/projects/{pid}/runs",
        json={"snapshot_id": snapshot["id"], "idempotency_key": "x", "limits": {"attempts": 999}},
        headers=headers(),
    )
    assert r.status_code == 422


def test_cancel_is_requested_not_falsely_confirmed(system):
    c, _, _, _ = system
    pid, s = create_source(c)
    rid = c.post(
        f"/projects/{pid}/runs",
        json={"snapshot_id": s["id"], "idempotency_key": "cancel"},
        headers=headers(),
    ).json()["id"]
    response = c.post(f"/runs/{rid}/cancel", headers=headers())
    assert response.status_code == 200
    assert response.json()["cancel_requested"] is True
    assert response.json()["status"] != "CANCELLED"


def test_ready_reports_uninitialized_database(tmp_path):
    e = Engine(f"sqlite:///{tmp_path / 'empty.db'}")
    app = create_app(
        Settings(api_token=TOKEN, work_root=tmp_path / "w", artifact_root=tmp_path / "a"), engine=e
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
    e.db.dispose()


def test_openapi_and_panel_have_no_inline_project_html(system):
    c, _, _, _ = system
    assert c.get("/").status_code == 200
    assert "frame-ancestors 'none'" in c.get("/").headers["content-security-policy"]
    assert c.get("/openapi.json").status_code == 200
    script = c.get("/panel.js").text
    assert "innerHTML" not in script
    assert "localStorage" not in script
