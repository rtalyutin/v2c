"""Explicit immutable registrations; no package discovery or method reflection."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from ..contracts.stages import AdmissionCheck, StageHandler, StageSpec
from ..workflow.errors import DomainError


class StageRegistry:
    def __init__(self, stages: Iterable[StageSpec], capabilities: Mapping[str, AdmissionCheck]):
        registrations: dict[str, StageSpec] = {}
        for spec in stages:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", spec.kind):
                raise DomainError("INVALID_STAGE_KIND")
            if spec.kind in registrations:
                raise DomainError("DUPLICATE_STAGE_KIND")
            if any(name not in capabilities for name in spec.requirements):
                raise DomainError("STAGE_CAPABILITY_UNAVAILABLE")
            registrations[spec.kind] = spec
        self.specs: Mapping[str, StageSpec] = MappingProxyType(registrations)
        self.capabilities = MappingProxyType(dict(capabilities))
        self.handlers: Mapping[str, StageHandler] = MappingProxyType(
            {kind: spec.handler for kind, spec in registrations.items()}
        )

    def admit(self, kind: str) -> StageHandler:
        spec = self.specs.get(kind)
        if spec is None:
            raise DomainError("UNKNOWN_STEP_KIND")
        for name in spec.requirements:
            self.capabilities[name]()
        return spec.handler
