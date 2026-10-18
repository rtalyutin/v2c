"""Closed operator API. Project code is never executed by this process."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError
from sqlalchemy import text

from .config import Settings, build_store
from .engine import DomainError, Engine


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NewProject(Body):
    name: StrictStr = Field(min_length=1, max_length=200)


class NewRun(Body):
    snapshot_id: StrictStr = Field(min_length=1, max_length=100)
    idempotency_key: StrictStr = Field(min_length=1, max_length=200)
    parent_run_id: StrictStr | None = None


class Decision(Body):
    snapshot_id: StrictStr
    plan_revision: int = Field(ge=1)
    approved: StrictBool


class AcceptanceCheck(Body):
    argv: list[StrictStr] = Field(min_length=1, max_length=100)
    cwd: StrictStr = "."
    timeout_seconds: int = Field(default=120, ge=1, le=600)


class Acceptance(Body):
    task: StrictStr = Field(min_length=1, max_length=20000)
    checks: list[AcceptanceCheck] = Field(min_length=1, max_length=20)
    allowed_new_files: list[StrictStr] = Field(default_factory=list, max_length=100)


class HumanHandoff(Body):
    final_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    passed: StrictBool
    evidence: StrictStr = Field(min_length=10, max_length=20000)


class Repository(Body):
    name: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    url: StrictStr = Field(max_length=2048)
    revision: StrictStr = Field(pattern=r"^[a-fA-F0-9]{40}$")
    secret_ref: StrictStr | None = None


class RepositoryImport(Body):
    repositories: list[Repository] = Field(min_length=1, max_length=20)


SENSITIVE_KEYS = {"workspace_root", "root", "local_path", "secret", "api_key", "token", "runner_token", "api_token"}
SECRET_PATTERN = re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})")


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if k.lower() not in SENSITIVE_KEYS}
    if isinstance(value, (list, tuple)):
        return [public(v) for v in value]
    if isinstance(value, str):
        return SECRET_PATTERN.sub("[REDACTED]", value)
    return value


def create_app(settings: Settings | None = None, *, engine: Engine | None = None, store=None) -> FastAPI:
    settings = settings or Settings.from_env()
    owned_engine = engine is None

    @asynccontextmanager
    async def lifespan(app):
        app.state.tokens = settings.tokens()
        settings.work_root.mkdir(parents=True, exist_ok=True)
        settings.artifact_root.mkdir(parents=True, exist_ok=True)
        if settings.database_url.startswith("sqlite"):
            Path("data").mkdir(exist_ok=True)
        app.state.engine = engine or Engine(settings.database_url)
        # Schema is created by Alembic, never silently by an API process.
        app.state.store = store or build_store(settings)
        yield
        if owned_engine:
            app.state.engine.db.dispose()

    app = FastAPI(title="v2c operator API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    def auth(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
        supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        for owner, token in request.app.state.tokens.items():
            if hmac.compare_digest(supplied, token):
                return owner
        raise HTTPException(401, "UNAUTHORIZED", headers={"WWW-Authenticate": "Bearer"})

    def eng(request: Request) -> Engine:
        return request.app.state.engine

    def project(e: Engine, owner: str, project_id: str):
        p = e.get_project(project_id)
        if p["owner_id"] != owner:
            raise HTTPException(404, "NOT_FOUND")
        return p

    def run(e: Engine, owner: str, run_id: str):
        r = e.get_run(run_id)
        project(e, owner, r["project_id"])
        return r

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        code = exc.code
        status = 404 if code == "NOT_FOUND" else 409
        if code.startswith(("INVALID", "PARAMETER", "UNKNOWN_PARAMETER")):
            status = 422
        return JSONResponse({"detail": code}, status_code=status)

    @app.get("/healthz")
    def health():
        return {"status": "alive"}

    @app.get("/readyz")
    def ready(e: Engine = Depends(eng)):
        try:
            with e.db.connect() as conn:
                conn.execute(text("SELECT 1 FROM runtime_lock LIMIT 1"))
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready"}

    @app.get("/", include_in_schema=False)
    def panel():
        return FileResponse(Path(__file__).parent / "web" / "index.html")

    @app.get("/panel.js", include_in_schema=False)
    def panel_script():
        return FileResponse(Path(__file__).parent / "web" / "panel.js", media_type="application/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/projects")
    def projects(owner: str = Depends(auth), e: Engine = Depends(eng)):
        return public(e.list_projects(owner))

    @app.post("/projects", status_code=201)
    def new_project(body: NewProject, owner: str = Depends(auth), e: Engine = Depends(eng)):
        return public(e.create_project(owner, body.name))

    @app.get("/projects/{project_id}")
    def get_project(project_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        return public(project(e, owner, project_id))

    @app.get("/projects/{project_id}/snapshots")
    def snapshots(project_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        project(e, owner, project_id)
        return public(e.list_snapshots(project_id))

    @app.post("/projects/{project_id}/imports", status_code=201)
    async def imports(project_id: str, request: Request, owner: str = Depends(auth), e: Engine = Depends(eng)):
        from .intake import safe_import_archive, import_repositories, RepositoryInput, HTTPSGitFetcher
        project(e, owner, project_id)
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) < 0 or int(length) > settings.max_upload_bytes:
                    raise HTTPException(413, "IMPORT_TOO_LARGE")
            except ValueError:
                raise HTTPException(400, "INVALID_CONTENT_LENGTH")
        directory = settings.work_root / project_id / "imports" / str(uuid.uuid4())
        directory.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        with tempfile.NamedTemporaryFile(dir=directory.parent, suffix=".upload") as incoming:
            async for chunk in request.stream():
                received += len(chunk)
                if received > settings.max_upload_bytes:
                    raise HTTPException(413, "IMPORT_TOO_LARGE")
                incoming.write(chunk)
            incoming.flush()
            try:
                if request.headers.get("content-type", "").split(";")[0] == "application/json":
                    data = RepositoryImport.model_validate_json(Path(incoming.name).read_bytes())
                    repos = [RepositoryInput(**r.model_dump()) for r in data.repositories]
                    from .adapters.model import EnvSecretResolver
                    fetcher = HTTPSGitFetcher(settings.git_allowed_hosts, EnvSecretResolver())
                    imported = await asyncio.to_thread(import_repositories, repos, directory, project_id=project_id, fetcher=fetcher)
                else:
                    imported = await asyncio.to_thread(safe_import_archive, Path(incoming.name), directory, project_id=project_id)
            except (ValueError, ValidationError) as exc:
                # Never reflect archive entries, command lines or credential-bearing input.
                raise HTTPException(422, "INVALID_IMPORT") from exc
        manifest = dict(imported.manifest)
        manifest["workspace_root"] = str(imported.root.resolve())
        from .intake import snapshot_archive_bytes, source_git_bundles
        archive = await asyncio.to_thread(snapshot_archive_bytes, imported)
        key = f"snapshots/{project_id}/{imported.snapshot_id}.zip"
        ref = await asyncio.to_thread(request.app.state.store.put_bytes, key, archive)
        manifest["archive_key"] = ref.key
        manifest["archive_sha256"] = ref.sha256
        manifest["archive_size"] = ref.size
        git_bundles = await asyncio.to_thread(source_git_bundles, imported)
        manifest["source_git_bundles"] = {}
        for name, data in git_bundles.items():
            bundle_ref = await asyncio.to_thread(request.app.state.store.put_bytes,
                f"snapshots/{project_id}/{imported.snapshot_id}/{name}.bundle", data)
            manifest["source_git_bundles"][name] = {"key": bundle_ref.key, "sha256": bundle_ref.sha256, "size": bundle_ref.size}
        # Only a completely stored, hashed object may acquire its DB pointer.
        snapshot = e.create_snapshot(project_id, manifest)
        return public(snapshot)

    @app.get("/projects/{project_id}/runs")
    def runs(project_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        project(e, owner, project_id)
        return public(e.list_runs(project_id))

    @app.post("/projects/{project_id}/runs", status_code=201)
    def new_run(project_id: str, body: NewRun, owner: str = Depends(auth), e: Engine = Depends(eng)):
        project(e, owner, project_id)
        limits = {"max_attempts_per_step": 5, "run_timeout_seconds": settings.run_timeout_seconds,
                  "command_timeout_seconds": settings.command_timeout_seconds,
                  "execution_limits": settings.execution_limits}
        if settings.model_base_url and settings.model:
            limits["model_profile_version"] = settings.model_profile_version
        return public(e.create_run(project_id, body.snapshot_id, body.idempotency_key, limits, body.parent_run_id))

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        r = run(e, owner, run_id)
        return public({**r, "steps": e.list_steps(run_id), "usage": e.usage(run_id),
                       "verifications": e.verifications(run_id)})

    @app.get("/runs/{run_id}/events")
    def events(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        run(e, owner, run_id)
        return public(e.events(run_id))

    @app.get("/runs/{run_id}/plan")
    def plan(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        run(e, owner, run_id)
        return public(e.get_plan(run_id))

    @app.post("/runs/{run_id}/decisions")
    def decide(run_id: str, body: Decision, owner: str = Depends(auth), e: Engine = Depends(eng)):
        r = run(e, owner, run_id)
        return public(e.decide_plan(run_id, r["project_id"], body.snapshot_id, body.plan_revision, body.approved, owner))

    @app.post("/runs/{run_id}/cancel")
    def cancel(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        run(e, owner, run_id)
        return public(e.request_cancel(run_id))

    @app.post("/runs/{run_id}/resume")
    def resume(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        r = run(e, owner, run_id)
        if r["status"] != "NEEDS_INPUT":
            raise HTTPException(409, "RUN_NOT_WAITING_FOR_INPUT")
        return public(e.resume_run(run_id))

    @app.post("/runs/{run_id}/acceptance")
    def acceptance(run_id: str, body: Acceptance, owner: str = Depends(auth), e: Engine = Depends(eng)):
        r = run(e, owner, run_id)
        if r["status"] not in {"NEEDS_INPUT", "AWAITING_HANDOFF"}:
            raise HTTPException(409, "RUN_NOT_WAITING_FOR_ACCEPTANCE")
        steps = e.list_steps(run_id)
        target = next((s for s in steps if s["kind"] == "ai_handoff"), None)
        if target is None:
            raise HTTPException(409, "CANDIDATE_NOT_AVAILABLE")
        payload = body.model_dump()
        payload["checks"] = [{"id": f"acceptance_{i + 1}", **check} for i, check in enumerate(payload["checks"])]
        return public(e.supply_input(run_id, target["id"], payload))

    @app.post("/runs/{run_id}/handoff")
    def handoff(run_id: str, body: HumanHandoff, request: Request, owner: str = Depends(auth), e: Engine = Depends(eng)):
        r = run(e, owner, run_id)
        if r["status"] not in {"AWAITING_HANDOFF", "NEEDS_INPUT"}:
            raise HTTPException(409, "RUN_NOT_WAITING_FOR_HANDOFF")
        if r.get("final_digest") != body.final_digest:
            raise HTTPException(409, "FINAL_DIGEST_MISMATCH")
        e.record_verification(run_id, "human_handoff", body.passed,
                              {"actor": owner, "notes": body.evidence}, body.final_digest)
        if not body.passed:
            return public(e.get_run(run_id))
        from .pipeline import Pipeline
        Pipeline(e, request.app.state.store, settings=settings).seal_handoff_bundle(run_id, body.final_digest)
        return public(e.complete_handoff(run_id, body.final_digest))

    @app.get("/runs/{run_id}/artifacts")
    def artifacts(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        run(e, owner, run_id)
        return public(e.artifacts(run_id))

    @app.get("/runs/{run_id}/artifacts/{artifact_id}")
    def artifact(run_id: str, artifact_id: str, request: Request, owner: str = Depends(auth), e: Engine = Depends(eng)):
        run(e, owner, run_id)
        a = next((a for a in e.artifacts(run_id) if a["id"] == artifact_id), None)
        if not a:
            raise HTTPException(404, "NOT_FOUND")
        data = request.app.state.store.get_bytes(a["object_key"])
        if hashlib.sha256(data).hexdigest() != a["sha256"]:
            raise HTTPException(409, "ARTIFACT_INTEGRITY_ERROR")
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="v2c-{artifact_id}.zip"',
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @app.middleware("http")
    async def headers(request, call_next):
        # Operator UI has no third-party assets and never injects project HTML.
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
        if request.url.path not in {"/", "/panel.js", "/healthz"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


app = create_app()
