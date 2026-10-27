"""Closed operator API. Project code is never executed by this process."""

from __future__ import annotations

import hashlib
import hmac
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import text

from .application.handoff import record_human_handoff
from .application.imports import ImportRejected, import_snapshot
from .config import Settings, build_store
from .contracts.operator import (
    Acceptance,
    ArtifactResponse,
    Decision,
    ErrorResponse,
    EventResponse,
    HealthResponse,
    HumanHandoff,
    NewProject,
    NewRun,
    PlanResponse,
    ProjectResponse,
    ReadinessResponse,
    RepositoryImport,
    RunDetailResponse,
    RunResponse,
    SnapshotResponse,
    UnreadyResponse,
)
from .contracts.operator import (
    AcceptanceCheck as AcceptanceCheck,
)
from .contracts.operator import (
    Body as Body,
)
from .contracts.operator import (
    Repository as Repository,
)
from .engine import DomainError, Engine

SENSITIVE_KEYS = {
    "workspace_root",
    "root",
    "local_path",
    "secret",
    "api_key",
    "token",
    "runner_token",
    "api_token",
}
SECRET_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})"
)


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if k.lower() not in SENSITIVE_KEYS}
    if isinstance(value, (list, tuple)):
        return [public(v) for v in value]
    if isinstance(value, str):
        return SECRET_PATTERN.sub("[REDACTED]", value)
    return value


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """Common safe error envelope; request validation keeps its existing shape."""
    descriptions = {
        400: "Malformed transport metadata.",
        401: "Missing or invalid operator token.",
        404: "Resource not found or belongs to another operator.",
        409: "Operation conflicts with durable state or immutable identity.",
        413: "Source import exceeds its configured byte limit.",
        422: "Invalid request or source import.",
    }
    return {code: {"model": ErrorResponse, "description": descriptions[code]} for code in codes}


IMPORT_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/RepositoryImport"}},
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
        },
        "description": "Pinned HTTPS repositories as JSON, or ZIP/TAR archive bytes. Upload and extraction limits apply before snapshot publication.",
    }
}

WEB_ASSETS = {
    "api-client.js",
    "state.js",
    "render.js",
    "polling.js",
    "panel.css",
}


def create_app(
    settings: Settings | None = None, *, engine: Engine | None = None, store=None
) -> FastAPI:
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
        supplied = (
            authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        )
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

    @app.get("/healthz", response_model=HealthResponse)
    def health():
        return {"status": "alive"}

    @app.get(
        "/readyz",
        response_model=ReadinessResponse,
        responses={503: {"model": UnreadyResponse, "description": "Database schema is not ready."}},
    )
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
        return FileResponse(
            Path(__file__).parent / "web" / "panel.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/web/{filename}", include_in_schema=False)
    def panel_asset(filename: str):
        if filename not in WEB_ASSETS:
            raise HTTPException(404, "NOT_FOUND")
        media_type = "text/css" if filename.endswith(".css") else "application/javascript"
        return FileResponse(
            Path(__file__).parent / "web" / filename,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/projects", response_model=list[ProjectResponse], responses=error_responses(401))
    def projects(owner: str = Depends(auth), e: Engine = Depends(eng)):
        return public(e.list_projects(owner))

    @app.post(
        "/projects",
        status_code=201,
        response_model=ProjectResponse,
        responses=error_responses(401, 409, 422),
    )
    def new_project(body: NewProject, owner: str = Depends(auth), e: Engine = Depends(eng)):
        return public(e.create_project(owner, body.name))

    @app.get(
        "/projects/{project_id}",
        response_model=ProjectResponse,
        responses=error_responses(401, 404, 422),
    )
    def get_project(project_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        return public(project(e, owner, project_id))

    @app.get(
        "/projects/{project_id}/snapshots",
        response_model=list[SnapshotResponse],
        responses=error_responses(401, 404, 422),
    )
    def snapshots(project_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        project(e, owner, project_id)
        return public(e.list_snapshots(project_id))

    @app.post(
        "/projects/{project_id}/imports",
        status_code=201,
        response_model=SnapshotResponse,
        responses=error_responses(400, 401, 404, 409, 413, 422),
        openapi_extra=IMPORT_BODY,
    )
    async def imports(
        project_id: str, request: Request, owner: str = Depends(auth), e: Engine = Depends(eng)
    ):
        project(e, owner, project_id)
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) < 0 or int(length) > settings.max_upload_bytes:
                    raise HTTPException(413, "IMPORT_TOO_LARGE")
            except ValueError:
                raise HTTPException(400, "INVALID_CONTENT_LENGTH") from None
        try:
            snapshot = await import_snapshot(
                e,
                request.app.state.store,
                settings,
                project_id,
                request.stream(),
                repositories_json=request.headers.get("content-type", "").split(";")[0]
                == "application/json",
            )
        except ImportRejected as error:
            raise HTTPException(error.status_code, error.code) from error
        return public(snapshot)

    @app.get(
        "/projects/{project_id}/runs",
        response_model=list[RunResponse],
        responses=error_responses(401, 404, 422),
    )
    def runs(project_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        project(e, owner, project_id)
        return public(e.list_runs(project_id))

    @app.post(
        "/projects/{project_id}/runs",
        status_code=201,
        response_model=RunResponse,
        responses=error_responses(401, 404, 409, 422),
    )
    def new_run(
        project_id: str, body: NewRun, owner: str = Depends(auth), e: Engine = Depends(eng)
    ):
        project(e, owner, project_id)
        limits = {
            "max_attempts_per_step": 5,
            "run_timeout_seconds": settings.run_timeout_seconds,
            "command_timeout_seconds": settings.command_timeout_seconds,
            "execution_limits": settings.execution_limits,
        }
        if settings.model_base_url and settings.model:
            limits["model_profile_version"] = settings.model_profile_version
        return public(
            e.create_run(
                project_id, body.snapshot_id, body.idempotency_key, limits, body.parent_run_id
            )
        )

    @app.get(
        "/runs/{run_id}", response_model=RunDetailResponse, responses=error_responses(401, 404, 422)
    )
    def get_run(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        r = run(e, owner, run_id)
        return public(
            {
                **r,
                "steps": e.list_steps(run_id),
                "usage": e.usage(run_id),
                "verifications": e.verifications(run_id),
            }
        )

    @app.get(
        "/runs/{run_id}/events",
        response_model=list[EventResponse],
        responses=error_responses(401, 404, 422),
    )
    def events(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        run(e, owner, run_id)
        return public(e.events(run_id))

    @app.get(
        "/runs/{run_id}/plan",
        response_model=PlanResponse | None,
        responses=error_responses(401, 404, 422),
    )
    def plan(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        run(e, owner, run_id)
        return public(e.get_plan(run_id))

    @app.post(
        "/runs/{run_id}/decisions",
        response_model=RunResponse,
        responses=error_responses(401, 404, 409, 422),
    )
    def decide(run_id: str, body: Decision, owner: str = Depends(auth), e: Engine = Depends(eng)):
        r = run(e, owner, run_id)
        return public(
            e.decide_plan(
                run_id, r["project_id"], body.snapshot_id, body.plan_revision, body.approved, owner
            )
        )

    @app.post(
        "/runs/{run_id}/cancel",
        response_model=RunResponse,
        responses=error_responses(401, 404, 409, 422),
    )
    def cancel(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        run(e, owner, run_id)
        return public(e.request_cancel(run_id))

    @app.post(
        "/runs/{run_id}/resume",
        response_model=RunResponse,
        responses=error_responses(401, 404, 409, 422),
    )
    def resume(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        r = run(e, owner, run_id)
        if r["status"] != "NEEDS_INPUT":
            raise HTTPException(409, "RUN_NOT_WAITING_FOR_INPUT")
        return public(e.resume_run(run_id))

    @app.post(
        "/runs/{run_id}/acceptance",
        response_model=RunResponse,
        responses=error_responses(401, 404, 409, 422),
    )
    def acceptance(
        run_id: str, body: Acceptance, owner: str = Depends(auth), e: Engine = Depends(eng)
    ):
        r = run(e, owner, run_id)
        if r["status"] not in {"NEEDS_INPUT", "AWAITING_HANDOFF"}:
            raise HTTPException(409, "RUN_NOT_WAITING_FOR_ACCEPTANCE")
        steps = e.list_steps(run_id)
        target = next((s for s in steps if s["kind"] == "ai_handoff"), None)
        if target is None:
            raise HTTPException(409, "CANDIDATE_NOT_AVAILABLE")
        payload = body.model_dump()
        payload["checks"] = [
            {"id": f"acceptance_{i + 1}", **check} for i, check in enumerate(payload["checks"])
        ]
        return public(e.supply_input(run_id, target["id"], payload))

    @app.post(
        "/runs/{run_id}/handoff",
        response_model=RunResponse,
        responses=error_responses(401, 404, 409, 422),
    )
    def handoff(
        run_id: str,
        body: HumanHandoff,
        request: Request,
        owner: str = Depends(auth),
        e: Engine = Depends(eng),
    ):
        run(e, owner, run_id)
        return public(record_human_handoff(e, request.app.state.store, run_id, body, owner))

    @app.get(
        "/runs/{run_id}/artifacts",
        response_model=list[ArtifactResponse],
        responses=error_responses(401, 404, 422),
    )
    def artifacts(run_id: str, owner: str = Depends(auth), e: Engine = Depends(eng)):
        run(e, owner, run_id)
        return public(e.artifacts(run_id))

    @app.get(
        "/runs/{run_id}/artifacts/{artifact_id}",
        response_class=Response,
        responses={
            **error_responses(401, 404, 409, 422),
            200: {
                "description": "Integrity-checked artifact bytes.",
                "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
            },
        },
    )
    def artifact(
        run_id: str,
        artifact_id: str,
        request: Request,
        owner: str = Depends(auth),
        e: Engine = Depends(eng),
    ):
        run(e, owner, run_id)
        a = next((a for a in e.artifacts(run_id) if a["id"] == artifact_id), None)
        if not a:
            raise HTTPException(404, "NOT_FOUND")
        data = request.app.state.store.get_bytes(a["object_key"])
        if hashlib.sha256(data).hexdigest() != a["sha256"]:
            raise HTTPException(409, "ARTIFACT_INTEGRITY_ERROR")
        return Response(
            data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="v2c-{artifact_id}.zip"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.middleware("http")
    async def headers(request, call_next):
        # Operator UI has no third-party assets and never injects project HTML.
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.url.path not in {"/", "/panel.js", "/healthz"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    original_openapi = app.openapi

    def operator_openapi() -> dict[str, Any]:
        schema = original_openapi()
        # Streaming import deliberately avoids FastAPI body buffering. Its body
        # contract is generated from the same parser model used by the use case.
        request_schema = RepositoryImport.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.update(request_schema.pop("$defs", {}))
        components["RepositoryImport"] = request_schema
        return schema

    app.openapi = operator_openapi
    return app


app = create_app()
