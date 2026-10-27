"""Workflow failures, resource limits and stable operation identities."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from ..contracts.workflow import JsonObject


class InputRequired(Exception):
    def __init__(self, reason: str, details: JsonObject | None = None):
        super().__init__(reason)
        self.reason, self.details = reason, details or {}


class StepFailure(Exception):
    def __init__(self, reason: str, evidence: JsonObject | None = None, *, retryable: bool = True):
        super().__init__(reason)
        self.reason, self.evidence, self.retryable = reason, evidence or {}, retryable


class RunCancelled(Exception):
    pass


@dataclass(frozen=True)
class PipelineLimits:
    command_timeout_seconds: int = 120
    provision_timeout_seconds: int = 600
    provision_poll_seconds: float = 5
    run_timeout_seconds: int = 3600
    model_max_input_bytes: int = 262144
    max_patch_bytes: int = 65536
    poll_seconds: float = 0.2


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "v2c:" + ":".join(parts)))


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
