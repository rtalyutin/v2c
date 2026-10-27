"""Internal persisted workflow shapes. TypedDict never rewrites stored JSON.

These are not public HTTP response models. The engine remains responsible for
validating state and ownership before emitting records. Unknown metadata is
kept as JSON rather than coerced or defaulted during a refactor.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, NotRequired, TypedDict

# Decoder-owned JSON envelopes: persisted command variants, model-schema
# documents, extension evidence and error details have different nested shapes.
# Do not use this alias for claims, stage results, limits or artifact references;
# those controller interfaces have named structures below. Pydantic proposal
# schemas validate model-owned contents before they can authorize an effect.
JsonObject = dict[str, Any]

# Metadata is read-only to orchestration. Its content belongs to the producing
# adapter/schema, so consuming a nested value requires explicit narrowing.
Metadata = Mapping[str, object]


class ExecutionLimits(TypedDict):
    cpus: int | float
    memory_mb: int
    pids: int
    disk_mb: int
    max_output_bytes: int
    network: str
    image: str


class RunLimits(TypedDict, total=False):
    max_attempts_per_step: int
    run_timeout_seconds: int
    command_timeout_seconds: int
    execution_limits: ExecutionLimits
    model_profile_version: str
    model_profile: Metadata
    model_profile_hash: str


class RunRecord(TypedDict):
    id: str
    project_id: str
    snapshot_id: str
    idempotency_key: str
    intent_hash: str
    parent_run_id: str | None
    status: str
    reason: str | None
    original_reason: str | None
    stage: str | None
    plan_revision: int
    limits: RunLimits
    cancel_requested: bool
    checkpoint_id: str | None
    final_digest: str | None
    created_at: float
    updated_at: float


class StepRecord(TypedDict):
    id: str
    run_id: str
    logical_key: str
    kind: str
    position: int
    payload: JsonObject
    status: str
    attempts_used: int
    generation: int
    plan_revision: int
    result: JsonObject | None


class AttemptRecord(TypedDict):
    id: str
    step_id: str
    number: int
    generation: int
    worker_id: str
    lease_until: float
    status: str
    result: JsonObject | None
    reason: str | None
    execution_confirmed: bool
    reconciliation_required: bool
    started_at: float
    finished_at: float | None


class RunClaim(TypedDict):
    run: RunRecord
    step: StepRecord
    attempt: AttemptRecord


class PendingExecution(TypedDict):
    """An unresolved remote effect keeps its existing attempt running."""

    status: Literal["RUNNING"]
    reason: str
    details: JsonObject
    stop_confirmed: Literal[False]


class AttemptFence(TypedDict):
    attempt_id: str
    generation: int


class ArtifactPointer(TypedDict):
    key: str
    sha256: str
    size: int


class CheckpointRef(ArtifactPointer):
    artifact_id: str
    manifest: Metadata


class RecipeCommandPayload(TypedDict):
    id: str
    argv: list[str]
    cwd: str
    timeout_seconds: int


class PostgresServicePayload(TypedDict):
    name: str
    kind: Literal["postgres"]
    database: str
    user: str
    env_var: Literal["DATABASE_URL"]


class RecipePayload(TypedDict):
    """Persisted EnvironmentRecipe after its existing Pydantic validation."""

    runtime: str
    setup: list[RecipeCommandPayload]
    restore: list[RecipeCommandPayload]
    tests: list[RecipeCommandPayload]
    recovery_checks: list[RecipeCommandPayload]
    state_strategy: Literal["stateless", "rebuild_from_fixtures"]
    services: list[PostgresServicePayload]
    limitations: list[str]


class CommandOutcome(TypedDict):
    """Runner result or the existing local rejection receipt.

    A delivery-confirmed rejection records only state, exit_code and reason.
    Completed runner replies include every CommandResult field. Optional keys
    describe that existing distinction; no defaults are inserted on replay.
    """

    state: Literal["ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"]
    exit_code: int | None
    command_id: NotRequired[str]
    request_hash: NotRequired[str]
    output: NotRequired[str]
    output_truncated: NotRequired[bool]
    stop_confirmed: NotRequired[bool]
    reason: NotRequired[str | None]


class CheckOutcome(CommandOutcome):
    check_id: str


class InspectResult(TypedDict):
    inspection: Metadata
    source_archive: ArtifactPointer
    recipe: RecipePayload


class BaselineResult(TypedDict):
    setup: list[CheckOutcome]
    recovery: list[CheckOutcome]
    checks: list[CheckOutcome]
    checkpoint: CheckpointRef
    known_failures: list[str]


class TransformResult(TypedDict):
    work_id: str
    explanation: str
    files: list[str]
    recovery: list[CheckOutcome]
    post_patch_recovery: list[CheckOutcome]
    checks: list[CheckOutcome]
    checkpoint: CheckpointRef


class VerifyResult(TypedDict):
    checkpoint: CheckpointRef
    final_digest: str
    checks: list[CheckOutcome]
    recovery: list[CheckOutcome]
    verification_vm_id: str


class PackageResult(TypedDict):
    bundle: ArtifactPointer
    final_digest: str
    delivery: Metadata


class HandoffResult(TypedDict):
    final_digest: str
    acceptance: list[CheckOutcome]
    human_handoff: str


class CommandRecord(TypedDict):
    id: str
    attempt_id: str
    generation: int
    request_hash: str
    request: JsonObject
    status: str
    result: JsonObject | None
    created_at: float


StageResult = (
    InspectResult | BaselineResult | TransformResult | VerifyResult | PackageResult | HandoffResult
)
