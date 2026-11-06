"""Compatibility facade and control-plane composition. Atomic owners live in persistence.

Canonical consumers depend on narrow ports, never ORM or this facade type.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, TypeVar, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .contracts.workflow import AttemptRecord, RunClaim, RunRecord, StepRecord
from .db import make_engine, transaction
from .models import (
    Attempt,
    Base,
    Entity,
    EntityParameter,
    EntityType,
    Run,
    RuntimeLock,
    Step,
)
from .persistence.commands import CommandStore
from .persistence.evidence import EvidenceStore
from .persistence.leases import LeaseStore
from .persistence.metadata import MetadataStore
from .persistence.plans import PlanStore
from .persistence.records import ProjectRecord, Row, SnapshotRecord, digest, dto, identifier
from .persistence.runs import RunStore
from .persistence.unit_of_work import ControlDatabase
from .persistence.usage import UsageStore

# Backwards-compatible exports; canonical policies live in workflow.states.
from .workflow.errors import DomainError
from .workflow.states import (
    COMMAND_TERMINAL,
    NON_RETRYABLE,
    REQUIRED_VERIFICATIONS,
)
from .workflow.states import RUN_TERMINAL as TERMINAL
from .workflow.states import RUN_WAITING as WAITING

__all__ = [
    "Engine",
    "DomainError",
    "TERMINAL",
    "WAITING",
    "COMMAND_TERMINAL",
    "NON_RETRYABLE",
    "REQUIRED_VERIFICATIONS",
    "digest",
    "dto",
    "identifier",
]

Model = TypeVar("Model", bound=Base)


class Engine:
    def __init__(self, database_url: str, *, clock: Callable[[], float] = time.time) -> None:
        self.db = make_engine(database_url)
        self.clock = clock
        # Clock remains injectable through Engine for existing callers/tests.
        self.database = ControlDatabase(self.db, lambda: self.clock())
        self.metadata = MetadataStore(self.database)
        self.commands = CommandStore(self.database)
        self.evidence = EvidenceStore(self.database)
        self.accounting = UsageStore(self.database)
        self.runs = RunStore(self.database)
        self.leases = LeaseStore(self.database)
        self.plans = PlanStore(self.database)

    def init_schema(self) -> None:
        """Disposable developer/test DB only; production uses Alembic upgrade head."""
        Base.metadata.create_all(self.db)
        self.seed_schema()

    def seed_schema(self) -> None:
        with transaction(self.db) as s:
            if s.get(RuntimeLock, 1) is None:
                s.add(RuntimeLock(id=1))
            for kind in ("project", "component", "finding"):
                if s.get(EntityType, kind) is None:
                    s.add(EntityType(id=kind, label=kind))
            s.flush()
            for kind in ("project", "component", "finding"):
                for name, typ in (("name", "text"), ("facts", "json"), ("project", "reference")):
                    key = f"{kind}.{name}"
                    if s.get(EntityParameter, key) is None:
                        s.add(EntityParameter(id=key, entity_type=kind, name=name, value_type=typ))

    def _write(self) -> AbstractContextManager[tuple[Session, RuntimeLock]]:
        return self.database.write()

    @staticmethod
    def _get(s: Session, model: type[Model], key: Any) -> Model:
        return ControlDatabase.get(s, model, key)

    def _event(self, s: Session, run: Run, kind: str, payload: Row | None = None) -> None:
        return self.database.event(s, run, kind, payload)

    @staticmethod
    def _release(lock: RuntimeLock, run: Run) -> None:
        ControlDatabase.release(lock, run)

    def create_project(self, owner_id: str, name: str) -> ProjectRecord:
        return self.metadata.create_project(owner_id, name)

    def _project(self, s: Session, entity: Entity) -> ProjectRecord:
        return self.metadata.project_row(s, entity)

    def get_project(self, project_id: str) -> ProjectRecord:
        return self.metadata.get_project(project_id)

    def list_projects(self, owner_id: str) -> list[ProjectRecord]:
        return self.metadata.list_projects(owner_id)

    def create_entity(
        self, owner_id: str, entity_type: str, values: Row, provenance: Row | None = None
    ) -> Row:
        return self.metadata.create_entity(owner_id, entity_type, values, provenance)

    def _set_value(
        self, s: Session, entity: Entity, name: str, value: Any, provenance: Row | None
    ) -> None:
        return self.metadata.set_parameter_value(s, entity, name, value, provenance)

    def create_snapshot(
        self, project_id: str, manifest: Row | None, artifact_id: str | None = None
    ) -> SnapshotRecord:
        return self.metadata.create_snapshot(project_id, manifest, artifact_id)

    def get_snapshot(self, snapshot_id: str) -> SnapshotRecord:
        return self.metadata.get_snapshot(snapshot_id)

    def list_snapshots(self, project_id: str) -> list[SnapshotRecord]:
        return self.metadata.list_snapshots(project_id)

    def _add_step(
        self,
        s: Session,
        run: Run,
        logical_key: str,
        kind: str,
        payload: Row | None = None,
        position: int | None = None,
    ) -> Step:
        existing = s.execute(
            select(Step).where(Step.run_id == run.id, Step.logical_key == logical_key)
        ).scalar_one_or_none()
        if existing:
            if existing.kind != kind:
                raise DomainError("STEP_IDENTITY_CONFLICT")
            return existing
        if position is None:
            position = (
                s.scalar(select(func.max(Step.position)).where(Step.run_id == run.id)) or 0
            ) + 1
        step = Step(
            id=identifier(),
            run_id=run.id,
            logical_key=logical_key,
            kind=kind,
            payload=payload or {},
            position=position,
            status="PENDING",
            attempts_used=0,
            generation=0,
            plan_revision=run.plan_revision,
        )
        s.add(step)
        s.flush()
        return step

    def create_run(
        self,
        project_id: str,
        snapshot_id: str,
        idempotency_key: str,
        limits: Row | None = None,
        parent_run_id: str | None = None,
    ) -> RunRecord:
        return self.runs.create_run(project_id, snapshot_id, idempotency_key, limits, parent_run_id)

    def get_run(self, run_id: str) -> RunRecord:
        return self.runs.get_run(run_id)

    def list_runs(self, project_id: str) -> list[Row]:
        return self.runs.list_runs(project_id)

    def list_pending_cancel_runs(self) -> list[Row]:
        return self.runs.list_pending_cancel_runs()

    def events(self, run_id: str) -> list[Row]:
        return self.runs.events(run_id)

    def add_step(
        self,
        run_id: str,
        logical_key: str,
        kind: str,
        payload: Row | None = None,
        position: int | None = None,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row:
        return self.runs.add_step(
            run_id,
            logical_key,
            kind,
            payload,
            position,
            attempt_id=attempt_id,
            generation=generation,
        )

    def get_step(self, step_id: str) -> Row:
        return self.runs.get_step(step_id)

    def list_steps(self, run_id: str) -> list[Row]:
        return self.runs.list_steps(run_id)

    def _claim_dto(self, run: Run, step: Step, attempt: Attempt) -> RunClaim:
        return {
            "run": cast(RunRecord, dto(run)),
            "step": cast(StepRecord, dto(step)),
            "attempt": cast(AttemptRecord, dto(attempt)),
        }

    def note_pending(self, attempt_id: str, generation: int, reason: str, details: Row) -> None:
        self.leases.note_pending(attempt_id, generation, reason, details)

    def claim_next(self, worker_id: str, lease_seconds: int = 60) -> RunClaim | None:
        return self.leases.claim_next(worker_id, lease_seconds)

    def _current(
        self,
        s: Session,
        attempt_id: str | None,
        generation: int | None,
        *,
        allow_cancel: bool = False,
        allow_expired: bool = False,
    ) -> tuple[Run, Step, Attempt]:
        return self.database.current(
            s, attempt_id, generation, allow_cancel=allow_cancel, allow_expired=allow_expired
        )

    def heartbeat(
        self,
        attempt_id: str | None,
        generation: int | None,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> Row:
        return self.leases.heartbeat(attempt_id, generation, worker_id, lease_seconds)

    def finish_attempt(
        self,
        attempt_id: str | None,
        generation: int | None,
        result: Row | None = None,
        outcome: str = "SUCCEEDED",
        reason: str | None = None,
        retryable: bool = False,
        execution_confirmed: bool = True,
    ) -> RunRecord:
        return self.leases.finish_attempt(
            attempt_id, generation, result, outcome, reason, retryable, execution_confirmed
        )

    def set_waiting(
        self,
        attempt_id: str | None,
        generation: int | None,
        status: str,
        reason: str | None,
        payload: Row | None = None,
    ) -> RunRecord:
        return self.leases.set_waiting(attempt_id, generation, status, reason, payload)

    def resume_run(self, run_id: str, payload: Row | None = None) -> RunRecord:
        return self.runs.resume_run(run_id, payload)

    def publish_plan(
        self,
        run_id: str,
        plan: Row,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row:
        return self.plans.publish_plan(run_id, plan, attempt_id=attempt_id, generation=generation)

    def get_plan(self, run_id: str) -> Row | None:
        return self.plans.get_plan(run_id)

    def decide_plan(
        self,
        run_id: str,
        project_id: str,
        snapshot_id: str,
        plan_revision: int,
        approved: bool,
        actor: str,
    ) -> RunRecord:
        return self.plans.decide_plan(
            run_id, project_id, snapshot_id, plan_revision, approved, actor
        )

    def prepare_command(
        self, attempt_id: str | None, generation: int | None, command_id: str, request: Row
    ) -> Row:
        return self.commands.prepare_command(attempt_id, generation, command_id, request)

    def get_command(self, command_id: str) -> Row:
        return self.commands.get_command(command_id)

    def commands_for_attempt(self, attempt_id: str | None) -> list[Row]:
        return self.commands.commands_for_attempt(attempt_id)

    def record_command_result(
        self, command_id: str, generation: int | None, status: str, result: Row | None = None
    ) -> Row:
        return self.commands.record_command_result(command_id, generation, status, result)

    def recover_expired(
        self,
        run_id: str,
        worker_id: str,
        execution_confirmed: bool = False,
        stop_confirmed: bool = False,
        lease_seconds: int = 60,
    ) -> RunClaim:
        return self.leases.recover_expired(
            run_id, worker_id, execution_confirmed, stop_confirmed, lease_seconds
        )

    def request_cancel(self, run_id: str) -> RunRecord:
        return self.leases.request_cancel(run_id)

    def confirm_cancel(self, run_id: str, stop_confirmed: bool = False) -> RunRecord:
        return self.leases.confirm_cancel(run_id, stop_confirmed)

    def record_artifact(
        self,
        run_id: str,
        kind: str,
        object_key: str,
        sha256: str,
        size: int,
        manifest: Row | None = None,
        verified: bool = False,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row:
        return self.evidence.record_artifact(
            run_id,
            kind,
            object_key,
            sha256,
            size,
            manifest,
            verified,
            attempt_id=attempt_id,
            generation=generation,
        )

    def artifacts(self, run_id: str) -> list[Row]:
        return self.evidence.artifacts(run_id)

    def confirm_checkpoint(
        self,
        run_id: str,
        artifact_id: str | None,
        restore_verified: bool = False,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row:
        return self.evidence.confirm_checkpoint(
            run_id, artifact_id, restore_verified, attempt_id=attempt_id, generation=generation
        )

    def record_verification(
        self,
        run_id: str,
        kind: str,
        passed: bool,
        evidence: Row,
        final_digest: str,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row:
        return self.evidence.record_verification(
            run_id,
            kind,
            passed,
            evidence,
            final_digest,
            attempt_id=attempt_id,
            generation=generation,
        )

    def verifications(self, run_id: str) -> list[Row]:
        return self.evidence.verifications(run_id)

    def complete_handoff(self, run_id: str, final_digest: str) -> RunRecord:
        return self.evidence.complete_handoff(run_id, final_digest)

    def record_usage(
        self, run_id: str, request_id: str, provider: str, usage: Row, attempt_id: str | None = None
    ) -> Row:
        return self.accounting.record_usage(run_id, request_id, provider, usage, attempt_id)

    def usage(self, run_id: str) -> list[Row]:
        return self.accounting.usage(run_id)

    def list_commands(self, run_id: str | None = None, attempt_id: str | None = None) -> list[Row]:
        return self.commands.list_commands(run_id, attempt_id)

    def list_recoverable_runs(self) -> list[Row]:
        return self.leases.list_recoverable_runs()

    def confirm_reconciliation(
        self, attempt_id: str | None, generation: int | None, stop_confirmed: bool = False
    ) -> Row:
        return self.leases.confirm_reconciliation(attempt_id, generation, stop_confirmed)

    def update_step_input(self, run_id: str, logical_key: str, payload: Row) -> Row:
        return self.runs.update_step_input(run_id, logical_key, payload)

    def supply_input(self, run_id: str, step_id: str, payload: Row) -> RunRecord:
        return self.runs.supply_input(run_id, step_id, payload)

    def define_parameter(self, entity_type: str, name: str, value_type: str) -> Row:
        return self.metadata.define_parameter(entity_type, name, value_type)

    def set_entity_value(
        self, entity_id: str, name: str, value: Any, provenance: Row | None
    ) -> Row:
        return self.metadata.set_entity_value(entity_id, name, value, provenance)

    def get_entity(self, entity_id: str) -> Row:
        return self.metadata.get_entity(entity_id)

    def assert_current(self, attempt_id: str | None, generation: int | None) -> RunClaim:
        return self.leases.assert_current(attempt_id, generation)

    def bind_model_profile(
        self, run_id: str, profile_manifest: Row, *, attempt_id: str | None, generation: int | None
    ) -> Row:
        return self.runs.bind_model_profile(
            run_id, profile_manifest, attempt_id=attempt_id, generation=generation
        )
