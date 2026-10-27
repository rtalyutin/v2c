"""Ownership-checked allocation lifecycle with fail-closed reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import sqlite3
import uuid
from typing import Any, cast
from urllib.parse import urlsplit

from ...adapters.runner import VMAllocation
from .client import RunnerProbe, TimewebClient
from .contracts import AllocateRequest, GatewayError, GatewaySettings
from .journal import GatewayJournal
from .network import NetworkPolicy, TimewebPublicNetworkPolicy


class TimewebGateway:
    def __init__(
        self,
        settings: GatewaySettings,
        *,
        cloud: TimewebClient | None = None,
        probe: RunnerProbe | None = None,
        network_policy: NetworkPolicy | None = None,
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
                        parsed = ipaddress.ip_address(cast(str, value))
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
        url = urlsplit("https://" + host)
        if not url.hostname or url.hostname != host:
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
                ready = ready and await self.probe.verify(cast(str, endpoint), row["id"])
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
