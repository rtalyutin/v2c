"""Platform metadata and durable workflow state; input project schemas are untouched."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class EntityType(Base):
    __tablename__ = "entity_types"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(200))


class Entity(Base):
    __tablename__ = "entities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    entity_type: Mapped[str] = mapped_column(ForeignKey("entity_types.id"))
    created_at: Mapped[float] = mapped_column(Float)
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        UniqueConstraint("id", "entity_type", "owner_id"),
    )


class EntityParameter(Base):
    __tablename__ = "entity_parameters"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    entity_type: Mapped[str] = mapped_column(ForeignKey("entity_types.id"))
    name: Mapped[str] = mapped_column(String(100))
    value_type: Mapped[str] = mapped_column(String(20))
    __table_args__ = (
        UniqueConstraint("entity_type", "name"),
        UniqueConstraint("id", "entity_type", "value_type"),
        CheckConstraint(
            "value_type IN ('text','integer','number','boolean','json','reference')",
            name="parameter_type",
        ),
    )


class EntityParameterValue(Base):
    __tablename__ = "entity_parameter_values"
    entity_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    parameter_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(200))
    entity_type: Mapped[str] = mapped_column(String(40))
    value_type: Mapped[str] = mapped_column(String(20))
    is_null: Mapped[bool] = mapped_column(Boolean, default=False)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_integer: Mapped[int | None] = mapped_column(BigInteger)
    value_number: Mapped[float | None] = mapped_column(Float)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean)
    value_json: Mapped[dict | list | None] = mapped_column(JSON(none_as_null=True))
    value_reference: Mapped[str | None] = mapped_column(String(36))
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        ForeignKeyConstraint(
            ["entity_id", "entity_type", "owner_id"],
            ["entities.id", "entities.entity_type", "entities.owner_id"],
        ),
        ForeignKeyConstraint(
            ["parameter_id", "entity_type", "value_type"],
            [
                "entity_parameters.id",
                "entity_parameters.entity_type",
                "entity_parameters.value_type",
            ],
        ),
        ForeignKeyConstraint(["value_reference", "owner_id"], ["entities.id", "entities.owner_id"]),
        CheckConstraint(
            "(CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN value_integer IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN value_number IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN value_boolean IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN value_json IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN value_reference IS NOT NULL THEN 1 ELSE 0 END) = CASE WHEN is_null THEN 0 ELSE 1 END",
            name="value_exactly_one",
        ),
        CheckConstraint(
            "is_null OR (value_type = 'text' AND value_text IS NOT NULL) OR (value_type = 'integer' AND value_integer IS NOT NULL) OR (value_type = 'number' AND value_number IS NOT NULL) OR (value_type = 'boolean' AND value_boolean IS NOT NULL) OR (value_type = 'json' AND value_json IS NOT NULL) OR (value_type = 'reference' AND value_reference IS NOT NULL)",
            name="value_matches_type",
        ),
    )


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    artifact_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("id", "project_id"),)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(String(36))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    intent_hash: Mapped[str] = mapped_column(String(64))
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    status: Mapped[str] = mapped_column(String(40), default="QUEUED", index=True)
    reason: Mapped[str | None] = mapped_column(String(100))
    original_reason: Mapped[str | None] = mapped_column(String(100))
    stage: Mapped[str | None] = mapped_column(String(100))
    plan_revision: Mapped[int] = mapped_column(Integer, default=0)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(36))
    final_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["snapshot_id", "project_id"], ["snapshots.id", "snapshots.project_id"]
        ),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','NEEDS_INPUT','AWAITING_PLAN_APPROVAL','AWAITING_HANDOFF','READY','FAILED','CANCELLED')",
            name="run_status",
        ),
    )


class Step(Base):
    __tablename__ = "steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    logical_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    attempts_used: Mapped[int] = mapped_column(Integer, default=0)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    plan_revision: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("run_id", "logical_key"),
        CheckConstraint("attempts_used >= 0 AND attempts_used <= 5", name="attempt_budget"),
    )


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("steps.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(200))
    lease_until: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    result: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(100))
    execution_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[float] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        UniqueConstraint("step_id", "number"),
        CheckConstraint("number >= 1 AND number <= 5", name="attempt_number"),
    )


class RuntimeLock(Base):
    __tablename__ = "runtime_lock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    active_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))


class Plan(Base):
    __tablename__ = "plans"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id"))
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    plan_revision: Mapped[int] = mapped_column(Integer)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id"))
    approved: Mapped[bool] = mapped_column(Boolean)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[float] = mapped_column(Float)
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "plan_revision"], ["plans.run_id", "plans.revision"]),
        UniqueConstraint("run_id", "plan_revision"),
    )


class Command(Base):
    __tablename__ = "commands"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="PREPARED")
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(100))
    object_key: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    restore_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float)


class VerificationResult(Base):
    __tablename__ = "verification_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(100))
    sequence: Mapped[int] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean)
    evidence: Mapped[dict] = mapped_column(JSON)
    final_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float)

    __table_args__ = (UniqueConstraint("run_id", "sequence"),)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("attempts.id"))
    provider: Mapped[str] = mapped_column(String(100))
    usage: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)
