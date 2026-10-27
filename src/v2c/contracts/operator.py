"""Operator HTTP contracts, independent of storage columns.

Responses deliberately retain the baseline wire names and explicit nulls.
JSON documents in manifests, payloads, usage and evidence are extensible
project/provider records; their enclosing API resource is never untyped.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictStr

JsonDocument = dict[str, JsonValue]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NewProject(Body):
    name: StrictStr = Field(min_length=1, max_length=200)


class NewRun(Body):
    snapshot_id: StrictStr = Field(min_length=1, max_length=100)
    idempotency_key: StrictStr = Field(min_length=1, max_length=200)
    parent_run_id: StrictStr | None = None


class Decision(Body):
    snapshot_id: StrictStr
    plan_revision: int = Field(ge=1)
    approved: StrictBool


class AcceptanceCheck(Body):
    argv: list[StrictStr] = Field(min_length=1, max_length=100)
    cwd: StrictStr = "."
    timeout_seconds: int = Field(default=120, ge=1, le=600)


class Acceptance(Body):
    task: StrictStr = Field(min_length=1, max_length=20000)
    checks: list[AcceptanceCheck] = Field(min_length=1, max_length=20)
    allowed_new_files: list[StrictStr] = Field(default_factory=list, max_length=100)


class HumanHandoff(Body):
    final_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    passed: StrictBool
    evidence: StrictStr = Field(min_length=10, max_length=20000)


class Repository(Body):
    name: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    url: StrictStr = Field(max_length=2048)
    revision: StrictStr = Field(pattern=r"^[a-fA-F0-9]{40}$")
    secret_ref: StrictStr | None = None


class RepositoryImport(Body):
    repositories: list[Repository] = Field(min_length=1, max_length=20)


class Output(BaseModel):
    # New ORM columns cannot silently become public fields. This does not
    # suppress missing/invalid required fields: those still fail validation.
    model_config = ConfigDict(extra="ignore", strict=True)


class HealthResponse(Output):
    status: Literal["alive"]


class ReadinessResponse(Output):
    status: Literal["ready"]


class UnreadyResponse(Output):
    status: Literal["not_ready"]


class ErrorResponse(Output):
    detail: str | list[JsonDocument] = Field(
        description="Stable application error code, or FastAPI request-validation details."
    )


class ProjectResponse(Output):
    id: str = Field(description="Opaque project identifier.")
    owner_id: str
    name: str | None
    created_at: float = Field(description="Unix timestamp in seconds, UTC.")


class SnapshotResponse(Output):
    id: str
    project_id: str
    digest: str = Field(description="SHA-256 of the persisted source manifest.")
    manifest: JsonDocument = Field(
        description="Extensible source inventory and immutable object references, redacted for public use."
    )
    artifact_id: str | None
    created_at: float = Field(description="Unix timestamp in seconds, UTC.")


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


class RunResponse(Output):
    id: str
    project_id: str
    snapshot_id: str
    idempotency_key: str
    intent_hash: str
    parent_run_id: str | None
    status: RunStatus
    reason: str | None
    original_reason: str | None
    stage: str | None
    plan_revision: int
    limits: JsonDocument = Field(
        description="Frozen run limits and model/execution profile metadata; timeouts are seconds, sizes are bytes unless named otherwise."
    )
    cancel_requested: bool
    checkpoint_id: str | None
    final_digest: str | None
    created_at: float = Field(description="Unix timestamp in seconds, UTC.")
    updated_at: float = Field(description="Unix timestamp in seconds, UTC.")


class StepResponse(Output):
    id: str
    run_id: str
    logical_key: str
    kind: str
    position: int
    payload: JsonDocument = Field(
        description="Stage-specific input; JSON keys are owned by the stage contract."
    )
    status: str
    attempts_used: int
    generation: int
    plan_revision: int
    result: JsonDocument | None


class UsageResponse(Output):
    id: str
    run_id: str
    attempt_id: str | None
    provider: str
    usage: JsonDocument = Field(
        description="Provider-specific usage receipt. Unknown usage remains unknown, never converted to zero."
    )
    created_at: float = Field(description="Unix timestamp in seconds, UTC.")


class VerificationResponse(Output):
    id: str
    run_id: str
    kind: str
    sequence: int
    passed: bool
    evidence: JsonDocument = Field(
        description="Verification-specific evidence, redacted for public use."
    )
    final_digest: str
    created_at: float = Field(description="Unix timestamp in seconds, UTC.")


class RunDetailResponse(RunResponse):
    steps: list[StepResponse]
    usage: list[UsageResponse]
    verifications: list[VerificationResponse]


class EventResponse(Output):
    id: int
    run_id: str
    kind: str
    payload: JsonDocument = Field(
        description="Event-specific data; stable event kind defines its semantics."
    )
    created_at: float = Field(description="Unix timestamp in seconds, UTC.")


class PlanResponse(Output):
    revision: int
    snapshot_id: str
    plan: JsonDocument = Field(
        description="Stored project-specific plan. The endpoint returns null until a plan exists."
    )


class ArtifactResponse(Output):
    id: str
    run_id: str
    kind: str
    object_key: str
    sha256: str
    size: int = Field(description="Object size in bytes.")
    manifest: JsonDocument = Field(
        description="Artifact-specific manifest, including candidate/sealed status where applicable."
    )
    verified: bool
    restore_verified: bool
    created_at: float = Field(description="Unix timestamp in seconds, UTC.")
