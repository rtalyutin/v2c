"""Authenticated Timeweb gateway HTTP API and mTLS entrypoint."""

from __future__ import annotations

import json
import os
import secrets
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ...adapters.runner import VMAllocation
from .client import RunnerProbe, TimewebClient
from .contracts import AllocateRequest, GatewayError, GatewaySettings
from .network import NetworkPolicy, public_firewall_rules
from .service import TimewebGateway


def create_app(
    settings: GatewaySettings,
    *,
    cloud: TimewebClient | None = None,
    probe: RunnerProbe | None = None,
    network_policy: NetworkPolicy | None = None,
) -> FastAPI:
    gateway = TimewebGateway(settings, cloud=cloud, probe=probe, network_policy=network_policy)
    app = FastAPI(title="v2c Timeweb VM gateway", version="1", docs_url=None, redoc_url=None)
    app.state.gateway = gateway

    async def auth(request: Request) -> None:
        if not secrets.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + settings.bearer_secret
        ):
            raise HTTPException(401, "gateway authorization denied")

    @app.middleware("http")
    async def bounded_body(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method == "POST":
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 4096:
                    return JSONResponse({"code": "LIMIT"}, status_code=413)
            request._body = bytes(data)
        return await call_next(request)

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, error: GatewayError) -> JSONResponse:
        status = {
            "NOT_FOUND": 404,
            "CONFLICT": 409,
            "CAPACITY": 409,
            "OWNERSHIP": 409,
            "AUTH": 503,
        }.get(error.code, 503)
        return JSONResponse({"code": error.code, "detail": str(error)}, status_code=status)

    @app.get("/health", dependencies=[Depends(auth)])
    async def health() -> dict[str, str | bool]:
        return {
            "status": "ok",
            "protocol": "v1",
            "provider": "timeweb",
            "live_resources_checked": False,
        }

    @app.post("/v1/vms", dependencies=[Depends(auth)], response_model=VMAllocation)
    async def allocate(request: AllocateRequest) -> VMAllocation:
        return await gateway.create(request)

    @app.get("/v1/vms/{allocation_id}", dependencies=[Depends(auth)], response_model=VMAllocation)
    async def status(allocation_id: str) -> VMAllocation:
        return await gateway.status(allocation_id)

    @app.delete(
        "/v1/vms/{allocation_id}", dependencies=[Depends(auth)], response_model=VMAllocation
    )
    async def destroy(allocation_id: str) -> VMAllocation:
        return await gateway.destroy(allocation_id)

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--print-firewall-rules", action="store_true")
    parser.add_argument("--control-cidr")
    parser.add_argument("--runner-port", type=int, default=8443)
    parser.add_argument("--tls-cert", default=os.environ.get("V2C_TIMEWEB_TLS_CERT"))
    parser.add_argument("--tls-key", default=os.environ.get("V2C_TIMEWEB_TLS_KEY"))
    parser.add_argument("--client-ca", default=os.environ.get("V2C_TIMEWEB_CLIENT_CA"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9443)
    args = parser.parse_args()
    if args.print_firewall_rules:
        if not args.control_cidr:
            parser.error("--control-cidr is required for rule generation")
        print(json.dumps(public_firewall_rules(args.control_cidr, args.runner_port), indent=2))
        return
    if not all((args.tls_cert, args.tls_key, args.client_ca)):
        parser.error("TLS certificate/key and client CA are mandatory")
    settings = (
        GatewaySettings.model_validate_json(Path(args.config).read_text())
        if args.config
        else GatewaySettings.from_env()
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
