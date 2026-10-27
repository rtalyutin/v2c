"""Bounded Timeweb HTTP transport and runner identity probe."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from ...adapters.runner import MTLSConfig
from .contracts import GatewayError, GatewaySettings

_API_ROOT = "https://api.timeweb.cloud"


class TimewebClient:
    """Verified API v1 shape. Exactly one request; never logs cloud response bodies."""

    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient | None = None):
        self.settings, self.client = settings, client

    async def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, int | str] | None = None,
        *,
        allow_not_found: bool = False,
    ) -> Any:
        client = self.client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0), trust_env=False
        )
        try:
            async with asyncio.timeout(self.settings.request_timeout_seconds):
                async with client.stream(
                    method,
                    _API_ROOT + path,
                    json=payload,
                    params=params,
                    headers={"Authorization": "Bearer " + self.settings.api_token},
                    timeout=self.settings.request_timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    if response.status_code == 404 and allow_not_found:
                        return None
                    if response.status_code not in (200, 201, 204):
                        category = {
                            401: "AUTH",
                            403: "AUTH",
                            423: "DELETE_CONFIRMATION_REQUIRED",
                            429: "RATE_LIMIT",
                        }.get(response.status_code, "CLOUD_FAILURE")
                        raise GatewayError(
                            category,
                            "Timeweb request was not successful",
                            unknown=method != "GET" and response.status_code >= 500,
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.settings.max_response_bytes:
                            raise GatewayError(
                                "INVALID_RESPONSE",
                                "Timeweb response exceeds configured limit",
                                unknown=method != "GET",
                            )
                    if response.status_code == 204:
                        return {}
                    try:
                        parsed = json.loads(body)
                        if not isinstance(parsed, dict):
                            raise ValueError("object required")
                        return parsed
                    except (ValueError, TypeError) as error:
                        raise GatewayError(
                            "INVALID_RESPONSE",
                            "Timeweb response is invalid",
                            unknown=method != "GET",
                        ) from error
        except (TimeoutError, httpx.RequestError) as error:
            raise GatewayError(
                "TRANSPORT_OR_TIMEOUT",
                "Timeweb request outcome requires readback",
                unknown=method != "GET",
            ) from error
        finally:
            if self.client is None:
                await client.aclose()

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.request("POST", "/api/v1/servers", payload)
        return self.server(result)

    async def get(self, cloud_id: int) -> dict[str, Any] | None:
        result = await self.request("GET", f"/api/v1/servers/{cloud_id}", allow_not_found=True)
        return None if result is None else self.server(result)

    async def delete(self, cloud_id: int) -> None:
        await self.request("DELETE", f"/api/v1/servers/{cloud_id}", allow_not_found=True)

    def server(self, result: dict[str, Any]) -> dict[str, Any]:
        value = result.get("server")
        if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] <= 0:
            raise GatewayError(
                "INVALID_RESPONSE", "Timeweb server identity is absent", unknown=True
            )
        return value

    async def find(self, name: str, marker: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        offset = 0
        for _ in range(self.settings.max_list_pages):
            result = await self.request(
                "GET",
                "/api/v1/servers",
                params={"limit": self.settings.list_page_size, "offset": offset},
            )
            servers = result.get("servers")
            if not isinstance(servers, list) or len(servers) > self.settings.list_page_size:
                raise GatewayError("INVALID_RESPONSE", "invalid Timeweb server page")
            for server in servers:
                if not isinstance(server, dict):
                    raise GatewayError("INVALID_RESPONSE", "invalid Timeweb server list item")
                if server.get("name") == name and server.get("comment") == marker:
                    if type(server.get("id")) is not int or server["id"] <= 0:
                        raise GatewayError("INVALID_RESPONSE", "invalid reconciled server identity")
                    matches.append(server)
            offset += len(servers)
            total = (result.get("meta") or {}).get("total")
            if type(total) is int and total >= 0:
                if offset >= total:
                    return matches
            elif len(servers) < self.settings.list_page_size:
                return matches
            if not servers:
                return matches
        raise GatewayError(
            "LIST_LIMIT", "Timeweb reconciliation requires more pages than configured"
        )


class RunnerProbe:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient | None = None):
        self.settings, self.client = settings, client

    async def verify(self, endpoint: str, allocation_id: str) -> bool:
        config = MTLSConfig(
            endpoint=endpoint,
            ca_file=self.settings.runner_ca_file,
            cert_file=self.settings.runner_cert_file,
            key_file=self.settings.runner_key_file,
            bearer_secret=self.settings.runner_bearer_secret,
            timeout_seconds=self.settings.request_timeout_seconds,
            max_response_bytes=self.settings.max_response_bytes,
            server_name=self.settings.runner_server_name,
        )
        client = self.client or config.client()
        try:
            async with asyncio.timeout(self.settings.request_timeout_seconds):
                async with client.stream(
                    "GET",
                    endpoint + "/health",
                    headers={
                        "Authorization": "Bearer " + self.settings.runner_bearer_secret,
                        **config.request_headers(),
                    },
                    extensions=config.request_extensions(),
                    timeout=self.settings.request_timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        return False
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.settings.max_response_bytes:
                            return False
                value = json.loads(body)
                return bool(
                    value.get("status") == "ok"
                    and value.get("protocol") == "v1"
                    and value.get("allocation_id") == allocation_id
                    and value.get("profile_sha256") == self.settings.expected_runner_profile_sha256
                )
        except (TimeoutError, httpx.RequestError, ValueError, TypeError, AttributeError):
            return False
        finally:
            if self.client is None:
                await client.aclose()
