"""Timeweb Cloud VM gateway with durable, fail-closed create reconciliation.

Timeweb does not document a create-idempotency header in the verified API
profile. A create intent is therefore committed before ONE cloud POST. An
unknown outcome is reconciled by its authenticated ownership marker; absence
from a list is never permission to repeat that POST. No cloud call occurs on
module import. Start this service only after authorizing actual provisioning.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sqlite3
import ssl
import time
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .adapters.runner import MTLSConfig, VMAllocation

_API_ROOT = "https://api.timeweb.cloud"
_PLACEHOLDER_ENDPOINT = "https://unassigned.invalid"
_ID = r"^[A-Za-z0-9_-]{1,128}$"


class GatewayError(Exception):
    def __init__(self, code: str, message: str, *, unknown: bool = False):
        super().__init__(message)
        self.code, self.unknown = code, unknown


class GatewaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path
    api_token: str = Field(min_length=1, repr=False)
    bearer_secret: str = Field(min_length=16, repr=False)
    ownership_secret: str = Field(min_length=32, repr=False)
    server_template_file: Path
    cloud_init_template_file: Path
    expected_runner_profile_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    runner_ca_file: str
    runner_cert_file: str
    runner_key_file: str
    runner_bearer_secret: str = Field(min_length=16, repr=False)
    runner_server_name: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_response_bytes: int = Field(gt=0)
    max_list_pages: int = Field(gt=0)
    list_page_size: int = Field(gt=0, le=1000)
    runner_port: int = Field(ge=1, le=65535)
    max_active_vms: int = Field(default=1, ge=1, le=1)
    runner_host_template: str = "{ip}"
    firewall_group_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    expected_firewall_rules_file: Path | None = None
    control_cidr: str | None = None

    @field_validator("runner_server_name")
    @classmethod
    def checked_server_name(cls, value: str) -> str:
        return MTLSConfig.dns_server_name(value)

    @field_validator("runner_host_template")
    @classmethod
    def validate_host_template(cls, value: str) -> str:
        if value != "{ip}":
            raise ValueError(
                "Timeweb gateway connects to authoritative public IPv4 directly; external DNS templates are unsupported"
            )
        return value

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        path = os.environ.get("V2C_TIMEWEB_SETTINGS_FILE")
        if path:
            return cls.model_validate_json(Path(path).read_text())
        values = {
            field: os.environ["V2C_TIMEWEB_" + field.upper()]
            for field in cls.model_fields
            if "V2C_TIMEWEB_" + field.upper() in os.environ
        }
        return cls.model_validate(values)


class AllocateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    allocation_id: str = Field(pattern=_ID)
    purpose: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class GatewayJournal:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "timeweb-gateway.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS allocation (
                    id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, purpose TEXT NOT NULL,
                    name TEXT NOT NULL, marker TEXT NOT NULL, cloud_id INTEGER UNIQUE,
                    state TEXT NOT NULL, generation INTEGER NOT NULL,
                    delete_requested INTEGER NOT NULL DEFAULT 0,
                    delete_sent INTEGER NOT NULL DEFAULT 0,
                    endpoint TEXT NOT NULL, reason TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS firewall_intent (
                    allocation_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, rules_sha256 TEXT NOT NULL,
                    generation INTEGER NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, allocation_id TEXT NOT NULL,
                    action TEXT NOT NULL, generation INTEGER NOT NULL, reason TEXT, created_at REAL NOT NULL
                );
            """)
        self.path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def get(self, allocation_id: str) -> sqlite3.Row:
        with self.connect() as db:
            row = db.execute("SELECT * FROM allocation WHERE id=?", (allocation_id,)).fetchone()
        if row is None:
            raise GatewayError("NOT_FOUND", "allocation is not owned by this gateway")
        return row

    def intent(
        self, request: AllocateRequest, request_hash: str, name: str, marker: str
    ) -> tuple[sqlite3.Row, bool]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM allocation WHERE id=?", (request.allocation_id,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise GatewayError(
                        "CONFLICT", "allocation ID already has a different immutable request"
                    )
                return existing, False
            if (
                db.execute("SELECT COUNT(*) FROM allocation WHERE state!='DESTROYED'").fetchone()[0]
                >= 1
            ):
                raise GatewayError(
                    "CAPACITY", "one active or unresolved VM already occupies the gateway"
                )
            now = time.time()
            db.execute(
                "INSERT INTO allocation (id,request_hash,purpose,name,marker,state,generation,endpoint,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    request.allocation_id,
                    request_hash,
                    request.purpose,
                    name,
                    marker,
                    "CREATING",
                    1,
                    _PLACEHOLDER_ENDPOINT,
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO event (allocation_id,action,generation,created_at) VALUES (?,?,?,?)",
                (request.allocation_id, "CREATE_INTENT_BEFORE_CLOUD_CALL", 1, now),
            )
        return self.get(request.allocation_id), True

    def update(
        self,
        allocation_id: str,
        *,
        state: str | None = None,
        reason: str | None = None,
        endpoint: str | None = None,
        cloud_id: int | None = None,
        generation: int | None = None,
    ) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM allocation WHERE id=?", (allocation_id,)).fetchone()
            if row is None:
                raise GatewayError("NOT_FOUND", "unknown allocation")
            if generation is not None and row["generation"] != generation:
                return False
            if cloud_id is not None and row["cloud_id"] is not None and row["cloud_id"] != cloud_id:
                raise GatewayError("OWNERSHIP", "allocation has a different cloud VM identity")
            if row["state"] == "DESTROYED":
                return False
            if cloud_id is not None:
                prior = db.execute(
                    "SELECT id FROM allocation WHERE cloud_id=? AND id!=?",
                    (cloud_id, allocation_id),
                ).fetchone()
                if prior:
                    raise GatewayError(
                        "OWNERSHIP", "cloud VM identity was already used by another allocation"
                    )
            if row["delete_requested"] and state in {"CREATING", "READY"}:
                state = "DESTROYING"
            db.execute(
                "UPDATE allocation SET state=?,reason=?,endpoint=?,cloud_id=?,updated_at=? WHERE id=?",
                (
                    state or row["state"],
                    reason,
                    endpoint or row["endpoint"],
                    cloud_id or row["cloud_id"],
                    time.time(),
                    allocation_id,
                ),
            )
            db.execute(
                "INSERT INTO event (allocation_id,action,generation,reason,created_at) VALUES (?,?,?,?,?)",
                (allocation_id, state or row["state"], row["generation"], reason, time.time()),
            )
        return True

    def claim_firewall(
        self, allocation_id: str, group_id: str, rules_sha256: str, generation: int
    ) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM allocation WHERE id=?", (allocation_id,)).fetchone()
            if row is None or row["generation"] != generation or row["delete_requested"]:
                raise GatewayError("STALE_GENERATION", "firewall attachment owner is stale")
            prior = db.execute(
                "SELECT * FROM firewall_intent WHERE allocation_id=?", (allocation_id,)
            ).fetchone()
            if prior:
                if prior["group_id"] != group_id or prior["rules_sha256"] != rules_sha256:
                    raise GatewayError("CONFLICT", "allocation already has another firewall intent")
                return False
            db.execute(
                "INSERT INTO firewall_intent VALUES (?,?,?,?,?)",
                (allocation_id, group_id, rules_sha256, generation, time.time()),
            )
            db.execute(
                "INSERT INTO event (allocation_id,action,generation,created_at) VALUES (?,?,?,?)",
                (
                    allocation_id,
                    "FIREWALL_ATTACH_INTENT_BEFORE_CLOUD_CALL",
                    generation,
                    time.time(),
                ),
            )
            return True

    def delete_intent(self, allocation_id: str) -> sqlite3.Row:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM allocation WHERE id=?", (allocation_id,)).fetchone()
            if row is None:
                raise GatewayError("NOT_FOUND", "allocation is not owned by this gateway")
            if row["state"] == "DESTROYED":
                return row
            if not row["delete_requested"]:
                db.execute(
                    "UPDATE allocation SET delete_requested=1,generation=generation+1,state='DESTROYING',updated_at=? WHERE id=?",
                    (time.time(), allocation_id),
                )
                db.execute(
                    "INSERT INTO event (allocation_id,action,generation,created_at) VALUES (?,?,?,?)",
                    (allocation_id, "DESTROY_INTENT", row["generation"] + 1, time.time()),
                )
        return self.get(allocation_id)

    def claim_delete(self, allocation_id: str, generation: int) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE allocation SET delete_sent=1 WHERE id=? AND generation=? AND delete_sent=0 AND delete_requested=1",
                (allocation_id, generation),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO event (allocation_id,action,generation,created_at) VALUES (?,?,?,?)",
                    (allocation_id, "DELETE_INTENT_BEFORE_CLOUD_CALL", generation, time.time()),
                )
        return bool(changed)


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


class FirewallRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    direction: Literal["ingress", "egress"]
    protocol: Literal["tcp", "udp", "icmp", "tcp6", "udp6", "icmp6"]
    port: str | None = None
    cidr: str

    @field_validator("cidr")
    @classmethod
    def canonical_network(cls, value: str) -> str:
        return str(ipaddress.ip_network(value, strict=True))


def public_control_cidr(value: str) -> str:
    """The MVP has exactly one public unicast IPv4 control address."""
    network = ipaddress.ip_network(value, strict=True)
    address = network.network_address
    if (
        network.version != 4
        or network.prefixlen != 32
        or not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_private
        or address.is_link_local
        or address.is_loopback
        or address.is_unspecified
    ):
        raise ValueError("one public unicast IPv4 /32 control address is required")
    return str(network)


def public_firewall_rules(control_cidr: str, runner_port: int) -> list[dict[str, Any]]:
    """Timeweb DROP whitelist: control ingress and two reserved exceptions.

    The explicit TCP/9 TEST-NET and IPv6 documentation-address exceptions
    activate directional filtering in both families. They are real allow rules,
    not a claim of literally zero new egress. Actual provider behavior, reserved
    destination reachability and post-lockdown control replies require target
    verification. No project container receives external network access.
    """
    control = ipaddress.ip_network(public_control_cidr(control_cidr), strict=True)
    if not 1 <= runner_port <= 65535:
        raise ValueError("public nonreserved control IPv4 CIDR and runner TCP port required")
    rules = [
        FirewallRule(
            direction="ingress", protocol="tcp", cidr=str(control), port=str(runner_port)
        ).model_dump(),
        FirewallRule(
            direction="egress", protocol="tcp", cidr="192.0.2.1/32", port="9"
        ).model_dump(),
        FirewallRule(
            direction="egress", protocol="tcp6", cidr="2001:db8::1/128", port="9"
        ).model_dump(),
    ]
    return sorted(rules, key=lambda item: json.dumps(item, sort_keys=True))


class TimewebPublicNetworkPolicy:
    """External DROP whitelist with authoritative readback.

    A later real post-lockdown health request is mandatory; API shape does not
    prove established-response semantics or runtime network isolation.
    """

    def __init__(self, settings: GatewaySettings, cloud: TimewebClient, journal: GatewayJournal):
        self.settings, self.cloud, self.journal = settings, cloud, journal

    def preflight(self) -> None:
        if not self.settings.firewall_group_id or not self.settings.control_cidr:
            raise GatewayError(
                "EXTERNAL_FIREWALL_PROFILE_REQUIRED",
                "external firewall group and control IPv4 CIDR are required",
            )
        self.expected_rules()

    def expected_rules(self) -> list[dict[str, Any]]:
        try:
            compiled = public_firewall_rules(self.settings.control_cidr, self.settings.runner_port)
            if self.settings.expected_firewall_rules_file:
                values = json.loads(self.settings.expected_firewall_rules_file.read_text())
                if not isinstance(values, list) or len(values) > 200:
                    raise ValueError("bounded rule list required")
                provided = sorted(
                    [FirewallRule.model_validate(value).model_dump() for value in values],
                    key=lambda item: json.dumps(item, sort_keys=True),
                )
                if provided != compiled:
                    raise ValueError("operator rules differ from the native isolation profile")
            return compiled
        except (ValueError, TypeError, OSError) as error:
            raise GatewayError(
                "EXTERNAL_FIREWALL_PROFILE_INVALID", "reviewed firewall rule profile is invalid"
            ) from error

    async def _collection(self, path: str, key: str) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        offset = 0
        for _ in range(self.settings.max_list_pages):
            result = await self.cloud.request(
                "GET", path, params={"limit": self.settings.list_page_size, "offset": offset}
            )
            page = result.get(key)
            if (
                not isinstance(page, list)
                or len(page) > self.settings.list_page_size
                or any(not isinstance(x, dict) for x in page)
            ):
                raise GatewayError("INVALID_RESPONSE", "invalid Timeweb firewall page")
            values.extend(page)
            offset += len(page)
            total = (result.get("meta") or {}).get("total")
            if type(total) is int and total >= 0 and offset >= total:
                return values
            if not page or (total is None and len(page) < self.settings.list_page_size):
                return values
        raise GatewayError(
            "LIST_LIMIT", "Timeweb firewall readback requires more pages than configured"
        )

    async def verify_and_attach(self, row: sqlite3.Row, server: dict[str, Any]) -> bool:
        """Wire/readback implementation, NOT a waiver of preflight semantics."""
        if any(
            network.get("type") != "public"
            for network in server.get("networks", [])
            if isinstance(network, dict)
        ):
            raise GatewayError(
                "EXTERNAL_NETWORK_UNSUPPORTED", "private/VPC network attachment is forbidden"
            )
        group_id = self.settings.firewall_group_id
        if not group_id:
            raise GatewayError(
                "EXTERNAL_FIREWALL_PROFILE_REQUIRED", "external firewall group is required"
            )
        expected = self.expected_rules()
        metadata = await self.cloud.request("GET", "/api/v1/firewall/groups/" + group_id)
        group = metadata.get("group")
        if (
            not isinstance(group, dict)
            or group.get("id") != group_id
            or group.get("policy") != "DROP"
        ):
            raise GatewayError(
                "EXTERNAL_FIREWALL_MISMATCH",
                "external firewall must use the reviewed DROP whitelist group",
            )
        actual = await self._collection("/api/v1/firewall/groups/" + group_id + "/rules", "rules")
        try:
            normalized = sorted(
                [
                    FirewallRule.model_validate(
                        {field: rule.get(field) for field in FirewallRule.model_fields}
                    ).model_dump()
                    for rule in actual
                ],
                key=lambda item: json.dumps(item, sort_keys=True),
            )
            if any(rule.get("group_id") != group_id for rule in actual) or normalized != expected:
                raise ValueError("rules differ")
        except (ValueError, TypeError) as error:
            raise GatewayError(
                "EXTERNAL_FIREWALL_MISMATCH",
                "external firewall rules differ from the reviewed profile",
            ) from error
        path = "/api/v1/firewall/service/server/" + str(server["id"])
        groups = await self._collection(path, "groups")
        if groups and (
            [item.get("id") for item in groups] != [group_id] or groups[0].get("policy") != "DROP"
        ):
            raise GatewayError(
                "EXTERNAL_FIREWALL_MISMATCH", "unexpected additional firewall groups are attached"
            )
        rules_hash = hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if not groups:
            claimed = self.journal.claim_firewall(
                row["id"], group_id, rules_hash, row["generation"]
            )
            if claimed:
                await self.cloud.request(
                    "POST",
                    "/api/v1/firewall/groups/" + group_id + "/resources/" + str(server["id"]),
                    params={"resource_type": "server"},
                )
            groups = await self._collection(path, "groups")
        if [item.get("id") for item in groups] != [group_id] or groups[0].get("policy") != "DROP":
            raise GatewayError(
                "EXTERNAL_FIREWALL_UNCONFIRMED",
                "firewall attachment is not authoritatively confirmed",
            )
        return True


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
                return (
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


class TimewebGateway:
    def __init__(
        self,
        settings: GatewaySettings,
        *,
        cloud: TimewebClient | None = None,
        probe: RunnerProbe | None = None,
        network_policy: Any = None,
    ):
        self.settings = settings
        self.journal = GatewayJournal(settings.data_dir)
        self.cloud = cloud or TimewebClient(settings)
        self.probe = probe or RunnerProbe(settings)
        self.network_policy = network_policy or TimewebPublicNetworkPolicy(
            settings, self.cloud, self.journal
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self.template = json.loads(settings.server_template_file.read_text())
        allowed = {"preset_id", "image_id", "ssh_keys_ids", "project_id", "availability_zone"}
        if (
            not isinstance(self.template, dict)
            or not {"preset_id", "image_id"} <= self.template.keys()
            or set(self.template) - allowed
            or type(self.template["preset_id"]) is not int
            or self.template["preset_id"] <= 0
        ):
            raise ValueError(
                "server template must select explicit preset_id/image_id and documented optional fields only"
            )
        try:
            uuid.UUID(self.template["image_id"])
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("image_id must be a Timeweb image UUID") from error
        self.cloud_init = settings.cloud_init_template_file.read_text()
        if settings.api_token in self.cloud_init or settings.ownership_secret in self.cloud_init:
            raise ValueError("cloud-init must not contain gateway/Timeweb credentials")
        if (
            not self.cloud_init.startswith("#cloud-config")
            or "__V2C_ALLOCATION_ID__" not in self.cloud_init
        ):
            raise ValueError(
                "cloud-init template must be #cloud-config and inject __V2C_ALLOCATION_ID__"
            )
        self.network_profile_hash = hashlib.sha256(
            json.dumps(
                {
                    "firewall_group_id": settings.firewall_group_id,
                    "control_cidr": settings.control_cidr,
                    "runner_port": settings.runner_port,
                    "profile": "DROP-default-deny-reserved-tcp9-v1",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        self.template_hash = hashlib.sha256(
            json.dumps(
                {"server": self.template, "cloud_init": self.cloud_init},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def lock(self, allocation_id: str) -> asyncio.Lock:
        return self._locks.setdefault(allocation_id, asyncio.Lock())

    def identity(self, allocation_id: str) -> tuple[str, str]:
        signature = hmac.new(
            self.settings.ownership_secret.encode(), allocation_id.encode(), hashlib.sha256
        ).hexdigest()
        return "v2c-" + signature[:24], "v2c:" + allocation_id + ":" + signature

    def allocation(self, row: sqlite3.Row) -> VMAllocation:
        return VMAllocation(
            vm_id=row["id"],
            runner_endpoint=row["endpoint"],
            state=row["state"],
            reason=row["reason"],
        )

    def owned(self, row: sqlite3.Row, server: dict[str, Any]) -> bool:
        return (
            server.get("name") == row["name"]
            and server.get("comment") == row["marker"]
            and (row["cloud_id"] is None or server.get("id") == row["cloud_id"])
        )

    async def create(self, request: AllocateRequest) -> VMAllocation:
        async with self.lock(request.allocation_id):
            name, marker = self.identity(request.allocation_id)
            request_hash = hashlib.sha256(
                json.dumps(
                    {
                        "request": request.model_dump(),
                        "template_sha256": self.template_hash,
                        "profile_sha256": self.settings.expected_runner_profile_sha256,
                        "network_profile_sha256": self.network_profile_hash,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            try:
                self.journal.get(request.allocation_id)
            except GatewayError as error:
                if error.code != "NOT_FOUND":
                    raise
                self.network_policy.preflight()
            row, fresh = self.journal.intent(request, request_hash, name, marker)
            if not fresh:
                return await self._status(row)
            payload = {
                **self.template,
                "name": name,
                "comment": marker,
                "cloud_init": self.cloud_init.replace(
                    "__V2C_ALLOCATION_ID__", request.allocation_id
                ),
            }
            try:
                server = await self.cloud.create(payload)
                if not self.owned(row, server):
                    raise GatewayError(
                        "OWNERSHIP",
                        "Timeweb creation response does not match the durable intent",
                        unknown=True,
                    )
                # Preserve the observation even if a concurrent cancellation
                # advanced generation. It cannot publish READY for that owner.
                current = self.journal.get(request.allocation_id)
                self.journal.update(
                    row["id"],
                    cloud_id=server["id"],
                    state="DESTROYING" if current["delete_requested"] else "CREATING",
                    reason="CREATE_RESPONSE_OBSERVED",
                )
            except asyncio.CancelledError:
                self.journal.update(
                    row["id"],
                    state="UNKNOWN",
                    reason="CREATE_CALL_CANCELLED_OUTCOME_UNKNOWN",
                    generation=row["generation"],
                )
                raise
            except GatewayError as error:
                if error.code == "AUTH" and not error.unknown:
                    # A verified Timeweb POST 401/403 rejected this request
                    # before resource creation. Keep this immutable allocation
                    # terminal; a new allocation may use corrected credentials.
                    self.journal.update(
                        row["id"],
                        state="DESTROYED",
                        reason="CREATE_REJECTED_AUTH",
                        generation=row["generation"],
                    )
                else:
                    self.journal.update(
                        row["id"], state="UNKNOWN", reason=error.code, generation=row["generation"]
                    )
                # Neither case permits a second POST for the same intent.
                # Ambiguous outcomes continue to occupy the capacity slot.
                return self.allocation(self.journal.get(row["id"]))
            return await self._status(self.journal.get(row["id"]))

    def endpoint(self, server: dict[str, Any], allocation_id: str) -> str | None:
        # API v1 supplies interface IP objects. Accept only explicit public IPv4
        # addresses, never caller/project URLs or redirects.
        candidates: list[str] = []
        for network in server.get("networks", []):
            if not isinstance(network, dict) or network.get("type") != "public":
                continue
            for address in network.get("ips") or []:
                if isinstance(address, dict):
                    value = address.get("ip")
                    try:
                        parsed = ipaddress.ip_address(value)
                        if (
                            parsed.version == 4
                            and not parsed.is_loopback
                            and not parsed.is_link_local
                            and not parsed.is_multicast
                            and not parsed.is_unspecified
                            and not any(
                                parsed in ipaddress.ip_network(cidr)
                                for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
                            )
                        ):
                            candidates.append(str(parsed))
                    except ValueError:
                        pass
        if len(set(candidates)) != 1:
            return None
        host = self.settings.runner_host_template.replace("{ip}", candidates[0]).replace(
            "{allocation_id}", allocation_id
        )
        parsed = urlsplit("https://" + host)
        if not parsed.hostname or parsed.hostname != host:
            return None
        return f"https://{host}:{self.settings.runner_port}"

    async def _status(self, row: sqlite3.Row) -> VMAllocation:
        if row["state"] == "DESTROYED":
            return self.allocation(row)
        try:
            if row["cloud_id"] is None:
                matches = await self.cloud.find(row["name"], row["marker"])
                if len(matches) != 1:
                    self.journal.update(
                        row["id"],
                        state="UNKNOWN",
                        reason="CREATE_NOT_AUTHORITATIVELY_RESOLVED"
                        if not matches
                        else "MULTIPLE_OWNED_RESOURCES",
                    )
                    return self.allocation(self.journal.get(row["id"]))
                self.journal.update(
                    row["id"], cloud_id=matches[0]["id"], reason="CREATE_RECONCILED"
                )
                row = self.journal.get(row["id"])
            server = await self.cloud.get(row["cloud_id"])
            if server is None:
                self.journal.update(
                    row["id"], state="DESTROYED", reason="KNOWN_CLOUD_ID_AUTHORITATIVELY_NOT_FOUND"
                )
                return self.allocation(self.journal.get(row["id"]))
            if not self.owned(row, server):
                self.journal.update(row["id"], state="UNKNOWN", reason="OWNERSHIP_MISMATCH")
                return self.allocation(self.journal.get(row["id"]))
            if row["delete_requested"]:
                self.journal.update(
                    row["id"], state="DESTROYING", reason="CLOUD_RESOURCE_STILL_EXISTS"
                )
                return self.allocation(self.journal.get(row["id"]))
            endpoint = self.endpoint(server, row["id"])
            ready = endpoint is not None and await self.probe.verify(endpoint, row["id"])
            lockdown_attempted = False
            with self.journal.connect() as db:
                prior_lockdown = (
                    db.execute(
                        "SELECT 1 FROM firewall_intent WHERE allocation_id=?", (row["id"],)
                    ).fetchone()
                    is not None
                )
            if ready:
                self.network_policy.preflight()
                ready = await self.network_policy.verify_and_attach(row, server)
                lockdown_attempted = True
                # A successful health probe before bootstrap lockdown cannot
                # attest the post-lockdown path. Check it again after readback.
                ready = ready and await self.probe.verify(endpoint, row["id"])
            lockdown_failed = not ready and (lockdown_attempted or prior_lockdown)
            self.journal.update(
                row["id"],
                state="READY" if ready else ("UNKNOWN" if lockdown_failed else "CREATING"),
                endpoint=endpoint,
                reason="RUNNER_IDENTITY_PROFILE_AND_FIREWALL_VERIFIED"
                if ready
                else ("NETWORK_LOCKDOWN_HEALTH_FAILED" if lockdown_failed else "RUNNER_NOT_READY"),
                generation=row["generation"],
            )
        except GatewayError as error:
            self.journal.update(
                row["id"], state="UNKNOWN", reason=error.code, generation=row["generation"]
            )
        return self.allocation(self.journal.get(row["id"]))

    async def status(self, allocation_id: str) -> VMAllocation:
        async with self.lock(allocation_id):
            return await self._status(self.journal.get(allocation_id))

    async def destroy(self, allocation_id: str) -> VMAllocation:
        async with self.lock(allocation_id):
            row = self.journal.delete_intent(allocation_id)
            if row["state"] == "DESTROYED":
                return self.allocation(row)
            resolved = await self._status(row)
            row = self.journal.get(allocation_id)
            if resolved.state == "DESTROYED" or row["cloud_id"] is None:
                return resolved
            server = await self.cloud.get(row["cloud_id"])
            if server is None:
                self.journal.update(
                    allocation_id,
                    state="DESTROYED",
                    reason="KNOWN_CLOUD_ID_AUTHORITATIVELY_NOT_FOUND",
                )
                return self.allocation(self.journal.get(allocation_id))
            if not self.owned(row, server):
                self.journal.update(allocation_id, state="UNKNOWN", reason="OWNERSHIP_MISMATCH")
                return self.allocation(self.journal.get(allocation_id))
            if self.journal.claim_delete(allocation_id, row["generation"]):
                try:
                    await self.cloud.delete(row["cloud_id"])
                except GatewayError as error:
                    self.journal.update(
                        allocation_id,
                        state="DESTROYING",
                        reason=error.code,
                        generation=row["generation"],
                    )
                    return self.allocation(self.journal.get(allocation_id))
            # HTTP 204 is acceptance; GET 404 for the owned mapping proves removal.
            return await self._status(self.journal.get(allocation_id))


def create_app(
    settings: GatewaySettings,
    *,
    cloud: TimewebClient | None = None,
    probe: RunnerProbe | None = None,
    network_policy: Any = None,
) -> FastAPI:
    gateway = TimewebGateway(settings, cloud=cloud, probe=probe, network_policy=network_policy)
    app = FastAPI(title="v2c Timeweb VM gateway", version="1", docs_url=None, redoc_url=None)
    app.state.gateway = gateway

    async def auth(request: Request):
        if not secrets.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + settings.bearer_secret
        ):
            raise HTTPException(401, "gateway authorization denied")

    @app.middleware("http")
    async def bounded_body(request: Request, call_next):
        if request.method == "POST":
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 4096:
                    return JSONResponse({"code": "LIMIT"}, status_code=413)
            request._body = bytes(data)
        return await call_next(request)

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, error: GatewayError):
        status = {
            "NOT_FOUND": 404,
            "CONFLICT": 409,
            "CAPACITY": 409,
            "OWNERSHIP": 409,
            "AUTH": 503,
        }.get(error.code, 503)
        return JSONResponse({"code": error.code, "detail": str(error)}, status_code=status)

    @app.get("/health", dependencies=[Depends(auth)])
    async def health():
        return {
            "status": "ok",
            "protocol": "v1",
            "provider": "timeweb",
            "live_resources_checked": False,
        }

    @app.post("/v1/vms", dependencies=[Depends(auth)], response_model=VMAllocation)
    async def allocate(request: AllocateRequest):
        return await gateway.create(request)

    @app.get("/v1/vms/{allocation_id}", dependencies=[Depends(auth)], response_model=VMAllocation)
    async def status(allocation_id: str):
        return await gateway.status(allocation_id)

    @app.delete(
        "/v1/vms/{allocation_id}", dependencies=[Depends(auth)], response_model=VMAllocation
    )
    async def destroy(allocation_id: str):
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


if __name__ == "__main__":
    main()
