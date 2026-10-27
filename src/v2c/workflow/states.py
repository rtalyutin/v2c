"""Separate control-plane state vocabularies and retry policy.

These are persisted string values, not a new workflow language. In particular
command STOPPED/DESTROYED are control observations, not runner wire statuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .errors import DomainError

RunStatus = Literal[
    "QUEUED",
    "RUNNING",
    "NEEDS_INPUT",
    "AWAITING_PLAN_APPROVAL",
    "AWAITING_HANDOFF",
    "READY",
    "FAILED",
    "CANCELLED",
]
StepStatus = Literal["PENDING", "RUNNING", "WAITING", "SUCCEEDED", "FAILED", "CANCELLED"]
AttemptStatus = Literal["ACTIVE", "WAITING", "SUCCEEDED", "FAILED", "CANCELLED"]
CommandStatus = Literal[
    "PREPARED",
    "ACCEPTED",
    "RUNNING",
    "UNKNOWN",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "STOPPED",
    "DESTROYED",
]

RUN_STATES = frozenset(
    {
        "QUEUED",
        "RUNNING",
        "NEEDS_INPUT",
        "AWAITING_PLAN_APPROVAL",
        "AWAITING_HANDOFF",
        "READY",
        "FAILED",
        "CANCELLED",
    }
)
RUN_TERMINAL = frozenset({"READY", "FAILED", "CANCELLED"})
RUN_WAITING = frozenset({"NEEDS_INPUT", "AWAITING_PLAN_APPROVAL", "AWAITING_HANDOFF"})
STEP_STATES = frozenset({"PENDING", "RUNNING", "WAITING", "SUCCEEDED", "FAILED", "CANCELLED"})
STEP_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
ATTEMPT_STATES = frozenset({"ACTIVE", "WAITING", "SUCCEEDED", "FAILED", "CANCELLED"})
ATTEMPT_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
COMMAND_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "STOPPED", "DESTROYED"})
COMMAND_STATES = COMMAND_TERMINAL | {"PREPARED", "ACCEPTED", "RUNNING", "UNKNOWN"}
NON_RETRYABLE = frozenset(
    {
        "AUTH",
        "INVALID_CONFIG",
        "UNSUPPORTED",
        "UNSUPPORTED_CAPABILITY",
        "MISSING_INPUT",
        "ACCESS_DENIED",
        "PLAN_REJECTED",
        "CANCELLED",
    }
)
INPUT_FAILURES = frozenset({"MISSING_INPUT", "AUTH", "INVALID_CONFIG", "ACCESS_DENIED"})
REQUIRED_VERIFICATIONS = frozenset({"final_regression", "ai_handoff", "human_handoff", "recovery"})
MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class AttemptCompletion:
    """A pure decision; its caller persists all effects in one transaction."""

    step_status: StepStatus
    run_status: RunStatus
    reason: str | None
    original_reason: str | None


def completion(
    outcome: str,
    *,
    pending: bool,
    retryable: bool,
    reason: str | None,
    attempts_used: int,
    previous_reason: str | None,
    previous_original_reason: str | None,
) -> AttemptCompletion:
    if outcome == "SUCCEEDED":
        return AttemptCompletion(
            "SUCCEEDED",
            "QUEUED" if pending else "AWAITING_HANDOFF",
            previous_reason,
            previous_original_reason,
        )
    if outcome != "FAILED":
        raise DomainError("INVALID_OUTCOME")
    permitted = retryable and reason not in NON_RETRYABLE
    if permitted and attempts_used < MAX_ATTEMPTS:
        return AttemptCompletion("PENDING", "QUEUED", reason, previous_original_reason)
    if permitted and attempts_used >= MAX_ATTEMPTS:
        return AttemptCompletion("FAILED", "FAILED", "RETRY_LIMIT_EXHAUSTED", reason)
    if reason in INPUT_FAILURES:
        return AttemptCompletion("WAITING", "NEEDS_INPUT", reason, reason)
    return AttemptCompletion("FAILED", "FAILED", reason or "STEP_FAILED", reason)
