"""Authenticated runner HTTP API and mandatory mTLS entrypoint."""

from __future__ import annotations

import secrets
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..adapters.runner import (
    AuthorizationRequest,
    CommandRequest,
    CommandResult,
    PrepareRequest,
    RunnerError,
)
from .docker import ExecutionBackend
from .service import RunnerService
from .settings import RunnerSettings


def create_app(settings: RunnerSettings, backend: ExecutionBackend | None = None) -> FastAPI:
    service = RunnerService(settings, backend)
    app = FastAPI(title="v2c isolated runner", version="1", docs_url=None, redoc_url=None)
    app.state.service = service

    async def auth(request: Request) -> None:
        supplied = request.headers.get("Authorization", "")
        if not secrets.compare_digest(supplied, "Bearer " + settings.bearer_secret):
            raise HTTPException(401, "runner authorization denied")

    @app.middleware("http")
    async def bound_body(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        limit = ((settings.max_archive_bytes + 2) // 3) * 4 + 1048576
        if request.method in ("POST", "PUT"):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > limit:
                    return JSONResponse({"detail": "body exceeds runner profile"}, status_code=413)
            request._body = bytes(body)
        return await call_next(request)

    @app.exception_handler(RunnerError)
    async def runner_error(request: Request, error: RunnerError) -> JSONResponse:
        code = {
            "NOT_FOUND": 404,
            "INVALID_INPUT": 422,
            "LIMIT": 413,
            "AUTH": 403,
            "INVALID_CONFIG": 503,
            "UNSUPPORTED": 422,
            "UNAVAILABLE": 503,
        }.get(error.code, 409)
        return JSONResponse({"code": error.code, "detail": str(error)}, status_code=code)

    dependencies = [Depends(auth)]

    @app.get("/health", dependencies=dependencies)
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "protocol": "v1",
            "allocation_id": settings.allocation_id,
            "profile_sha256": settings.profile_sha256(),
        }

    @app.post("/v1/workspaces", dependencies=dependencies)
    def prepare(request: PrepareRequest) -> dict[str, Any]:
        return service.prepare(request)

    @app.put("/v1/authorization", dependencies=dependencies)
    def authorize(request: AuthorizationRequest) -> dict[str, Any]:
        return service.authorize(request)

    @app.post(
        "/v1/commands", dependencies=dependencies, response_model=CommandResult, status_code=202
    )
    def execute(request: CommandRequest) -> CommandResult:
        return service.execute(request)

    @app.get("/v1/commands/{command_id}", dependencies=dependencies, response_model=CommandResult)
    def status(command_id: str) -> CommandResult:
        return service.status(command_id)

    @app.post(
        "/v1/commands/{command_id}/cancel", dependencies=dependencies, response_model=CommandResult
    )
    def cancel(command_id: str) -> CommandResult:
        return service.cancel(command_id)

    @app.get("/v1/workspaces/{workspace_id}/checkpoint", dependencies=dependencies)
    def checkpoint(workspace_id: str) -> Response:
        return Response(service.checkpoint(workspace_id), media_type="application/zip")

    @app.delete("/v1/workspaces/{workspace_id}", dependencies=dependencies)
    def destroy(workspace_id: str) -> dict[str, Any]:
        return service.destroy_workspace(workspace_id)

    return app


def main() -> None:
    """Start with mandatory mutual TLS. No unsafe HTTP launch switch."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", help="operator-only JSON RunnerSettings file; otherwise V2C_RUNNER_ environment"
    )
    parser.add_argument("--tls-cert", required=True)
    parser.add_argument("--tls-key", required=True)
    parser.add_argument("--client-ca", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    settings = (
        RunnerSettings.model_validate_json(Path(args.config).read_text())
        if args.config
        else RunnerSettings.from_env()
    )
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        ssl_certfile=args.tls_cert,
        ssl_keyfile=args.tls_key,
        ssl_ca_certs=args.client_ca,
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )
