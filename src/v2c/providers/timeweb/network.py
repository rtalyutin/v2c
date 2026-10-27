"""Reviewed external firewall profile and attachment readback policy."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import sqlite3
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, field_validator

from .client import TimewebClient
from .contracts import GatewayError, GatewaySettings
from .journal import GatewayJournal


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


class NetworkPolicy(Protocol):
    def preflight(self) -> None: ...
    async def verify_and_attach(self, row: sqlite3.Row, server: dict[str, Any]) -> bool: ...


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
            compiled = public_firewall_rules(
                cast(str, self.settings.control_cidr), self.settings.runner_port
            )
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
