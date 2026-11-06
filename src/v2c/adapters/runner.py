"""Runner protocol v1. No command executes on the control machine.

The VM-provider HTTP protocol is an operator-supplied integration endpoint, not
an assertion that any cloud vendor implements this protocol directly.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import ssl
from typing import Any, cast
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..contracts.runner import (
    AuthorizationRequest as AuthorizationRequest,
)
from ..contracts.runner import (
    AuthorizeReply,
    PrepareReply,
)
from ..contracts.runner import (
    CommandRequest as CommandRequest,
)
from ..contracts.runner import (
    CommandResult as CommandResult,
)
from ..contracts.runner import (
    PostgresService as PostgresService,
)
from ..contracts.runner import (
    PrepareRequest as PrepareRequest,
)
from ..contracts.runner import (
    RunnerError as RunnerError,
)
from ..contracts.runner import (
    VMAllocation as VMAllocation,
)

_ID = r"^[A-Za-z0-9_-]{1,128}$"


class MTLSConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    endpoint: str
    ca_file: str
    cert_file: str
    key_file: str
    bearer_secret: str = Field(repr=False, min_length=16)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_response_bytes: int = Field(gt=0)
    server_name: str | None = None

    @field_validator("server_name")
    @classmethod
    def dns_server_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 253 or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value
        ):
            raise ValueError("TLS server_name must be a DNS name without scheme, port or wildcard")
        if any(
            not part or len(part) > 63 or part.startswith("-") or part.endswith("-")
            for part in value.split(".")
        ):
            raise ValueError("invalid TLS DNS name")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return value
        raise ValueError("TLS server_name must be an explicit DNS SAN, not an IP")

    def request_extensions(self) -> dict[str, str]:
        return {"sni_hostname": self.server_name} if self.server_name else {}

    def request_headers(self) -> dict[str, str]:
        return {"Host": self.server_name} if self.server_name else {}

    @field_validator("endpoint")
    @classmethod
    def https_only(cls, value: str) -> str:
        p = urlsplit(value)
        if (
            p.scheme != "https"
            or not p.hostname
            or p.username
            or p.password
            or p.query
            or p.fragment
        ):
            raise ValueError(
                "integration endpoint must use HTTPS without credentials/query/fragment"
            )
        return value.rstrip("/")

    def client(self) -> httpx.AsyncClient:
        context = ssl.create_default_context(cafile=self.ca_file)
        context.load_cert_chain(self.cert_file, self.key_file)
        return httpx.AsyncClient(
            verify=context,
            transport=httpx.AsyncHTTPTransport(verify=context, retries=0),
            trust_env=False,
            timeout=self.timeout_seconds,
            follow_redirects=False,
        )


class _HTTPSClient:
    def __init__(self, config: MTLSConfig, http_client: httpx.AsyncClient | None = None):
        self.config, self.http_client = config, http_client

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> bytes:
        client = self.http_client or self.config.client()
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                return await self._exchange(client, method, path, payload)
        except TimeoutError as error:
            raise RunnerError(
                "TRANSPORT_OR_TIMEOUT",
                "runner/provider deadline exceeded; outcome requires readback",
                delivery_unknown=True,
            ) from error
        except httpx.RequestError as error:
            raise RunnerError(
                "TRANSPORT_OR_TIMEOUT",
                "runner/provider outcome requires readback",
                delivery_unknown=True,
            ) from error
        finally:
            if self.http_client is None:
                await client.aclose()

    async def _exchange(
        self, client: httpx.AsyncClient, method: str, path: str, payload: dict[str, Any] | None
    ) -> bytes:
        async with client.stream(
            method,
            self.config.endpoint + path,
            json=payload,
            headers={
                "Authorization": "Bearer " + self.config.bearer_secret,
                **self.config.request_headers(),
            },
            extensions=self.config.request_extensions(),
            timeout=self.config.timeout_seconds,
            follow_redirects=False,
        ) as response:
            if response.status_code not in (200, 201, 202, 204):
                code = {
                    401: "AUTH",
                    403: "AUTH",
                    404: "NOT_FOUND",
                    409: "CONFLICT",
                    413: "LIMIT",
                    422: "INVALID_INPUT",
                    503: "UNAVAILABLE",
                }.get(response.status_code, "REMOTE_FAILURE")
                diagnostic = bytearray()
                async for chunk in response.aiter_bytes():
                    diagnostic.extend(chunk[: max(0, 4096 - len(diagnostic))])
                    if len(diagnostic) >= 4096:
                        break
                try:
                    specific = json.loads(diagnostic).get("code")
                    if specific in {
                        "AUTH",
                        "NOT_FOUND",
                        "CONFLICT",
                        "LIMIT",
                        "INVALID_INPUT",
                        "INVALID_CONFIG",
                        "UNSUPPORTED",
                        "STALE_GENERATION",
                        "UNKNOWN",
                        "UNAVAILABLE",
                    }:
                        code = specific
                except (ValueError, TypeError, AttributeError):
                    pass
                raise RunnerError(
                    code,
                    "runner/provider rejected request",
                    delivery_unknown=response.status_code >= 500 or code == "UNKNOWN",
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self.config.max_response_bytes:
                    raise RunnerError(
                        "LIMIT",
                        "integration response exceeds configured byte limit",
                        delivery_unknown=method != "GET",
                    )
            return bytes(body)


class RunnerClient(_HTTPSClient):
    async def _object(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raw = await self._request(method, path, payload)
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("object required")
            return result
        except (ValueError, TypeError) as error:
            raise RunnerError(
                "INVALID_RESPONSE",
                "runner returned an invalid object",
                delivery_unknown=method != "GET",
            ) from error

    async def _command_result(
        self,
        method: str,
        path: str,
        command_id: str,
        payload: dict[str, Any] | None = None,
        expected_hash: str | None = None,
    ) -> CommandResult:
        raw = await self._request(method, path, payload)
        try:
            result = CommandResult.model_validate_json(raw)
            if result.command_id != command_id or (
                expected_hash is not None and result.request_hash != expected_hash
            ):
                raise ValueError("runner command identity differs")
            return result
        except (ValueError, TypeError) as error:
            raise RunnerError(
                "INVALID_RESPONSE",
                "runner returned an invalid command outcome",
                delivery_unknown=True,
            ) from error

    async def prepare(self, request: PrepareRequest) -> PrepareReply:
        result = await self._object("POST", "/v1/workspaces", request.model_dump())
        if result.get("workspace_id") != request.workspace_id or result.get("state") != "READY":
            raise RunnerError(
                "INVALID_RESPONSE",
                "runner workspace readiness is unconfirmed",
                delivery_unknown=True,
            )
        return cast(PrepareReply, result)

    async def authorize(
        self, workspace_id: str, attempt_id: str, generation: int
    ) -> AuthorizeReply:
        request = AuthorizationRequest(
            workspace_id=workspace_id, attempt_id=attempt_id, generation=generation
        )
        result = await self._object("PUT", "/v1/authorization", request.model_dump())
        if result.get("workspace_id") != workspace_id or result.get("generation") != generation:
            raise RunnerError(
                "INVALID_RESPONSE",
                "runner authorization identity is unconfirmed",
                delivery_unknown=True,
            )
        return cast(AuthorizeReply, result)

    async def execute(self, request: CommandRequest) -> CommandResult:
        return await self._command_result(
            "POST", "/v1/commands", request.command_id, request.model_dump(), request.content_hash()
        )

    async def status(self, command_id: str) -> CommandResult:
        return await self._command_result(
            "GET", "/v1/commands/" + quote(command_id, safe=""), command_id
        )

    async def cancel(self, command_id: str) -> CommandResult:
        return await self._command_result(
            "POST", "/v1/commands/" + quote(command_id, safe="") + "/cancel", command_id
        )

    async def checkpoint(self, workspace_id: str) -> bytes:
        return await self._request(
            "GET", "/v1/workspaces/" + quote(workspace_id, safe="") + "/checkpoint"
        )

    async def destroy_workspace(self, workspace_id: str) -> dict[str, Any]:
        result = await self._object("DELETE", "/v1/workspaces/" + quote(workspace_id, safe=""))
        if result.get("workspace_id") != workspace_id or result.get("state") != "DESTROYED":
            raise RunnerError(
                "INVALID_RESPONSE", "runner workspace removal is unconfirmed", delivery_unknown=True
            )
        return result


class HTTPVMProvider(_HTTPSClient):
    """Operator endpoint: POST /v1/vms, GET/DELETE /v1/vms/{vm_id}.

    create is idempotent by allocation_id, which MUST be persisted by the caller
    before invocation. DELETE acceptance does not prove destruction: poll status.
    """

    async def create(self, allocation_id: str, purpose: str) -> VMAllocation:
        AuthorizationRequest(workspace_id=allocation_id, attempt_id=allocation_id, generation=1)
        raw = await self._request(
            "POST", "/v1/vms", {"allocation_id": allocation_id, "purpose": purpose}
        )
        return self._allocation(raw)

    def _allocation(self, raw: bytes, expected_vm: str | None = None) -> VMAllocation:
        try:
            result = VMAllocation.model_validate_json(raw)
            if expected_vm is not None and result.vm_id != expected_vm:
                raise ValueError("VM identity differs")
            return result
        except (ValueError, TypeError) as error:
            raise RunnerError(
                "INVALID_RESPONSE",
                "VM provider returned an invalid allocation outcome",
                delivery_unknown=True,
            ) from error

    async def status(self, vm_id: str) -> VMAllocation:
        return self._allocation(
            await self._request("GET", "/v1/vms/" + quote(vm_id, safe="")), vm_id
        )

    async def destroy(self, vm_id: str) -> VMAllocation:
        return self._allocation(
            await self._request("DELETE", "/v1/vms/" + quote(vm_id, safe="")), vm_id
        )
