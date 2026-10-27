"""Compatibility entrypoint for the Timeweb gateway.

Implementations live in ``v2c.providers.timeweb``. Keep this facade while
Compose/deployment integrations use ``python -m v2c.timeweb_gateway``.
"""

from .providers.timeweb.api import create_app, main
from .providers.timeweb.client import RunnerProbe, TimewebClient
from .providers.timeweb.contracts import AllocateRequest, GatewayError, GatewaySettings
from .providers.timeweb.journal import GatewayJournal
from .providers.timeweb.network import (
    FirewallRule,
    TimewebPublicNetworkPolicy,
    public_control_cidr,
    public_firewall_rules,
)
from .providers.timeweb.service import TimewebGateway

__all__ = [
    "create_app",
    "main",
    "RunnerProbe",
    "TimewebClient",
    "AllocateRequest",
    "GatewayError",
    "GatewaySettings",
    "GatewayJournal",
    "FirewallRule",
    "TimewebPublicNetworkPolicy",
    "public_control_cidr",
    "public_firewall_rules",
    "TimewebGateway",
]

if __name__ == "__main__":
    main()
