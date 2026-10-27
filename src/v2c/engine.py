"""Durable, fenced workflow. External I/O must happen outside these transactions."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, TypeVar, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .contracts.workflow import AttemptRecord, RunClaim, RunRecord, StepRecord
from .db import make_engine, transaction
from .models import (
    Approval,
    Attempt,
    Base,
    Command,
    Entity,
    EntityParameter,
    EntityType,
    Event,
    Plan,
    Run,
    RuntimeLock,
    Snapshot,
    Step,
)
from .persistence.commands import CommandStore
from .persistence.evidence import EvidenceStore
from .persistence.metadata import MetadataStore
from .persistence.records import ProjectRecord, Row, SnapshotRecord, digest, dto, identifier
from .persistence.unit_of_work import ControlDatabase
from .persistence.usage import UsageStore

# Backwards-compatible exports; canonical policies live in workflow.states.
from .workflow.errors import DomainError
from .workflow.states import (
    COMMAND_TERMINAL,
    MAX_ATTEMPTS,
    NON_RETRYABLE,
    REQUIRED_VERIFICATIONS,
    completion,
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
        if not idempotency_key or len(idempotency_key) > 200:
            raise DomainError("INVALID_IDEMPOTENCY_KEY")
        limits = limits or {}
        fingerprint = digest(
            {"snapshot_id": snapshot_id, "limits": limits, "parent_run_id": parent_run_id}
        )
        with self._write() as (s, _):
            snapshot = self._get(s, Snapshot, snapshot_id)
            if snapshot.project_id != project_id:
                raise DomainError("SNAPSHOT_PROJECT_MISMATCH")
            old = s.execute(
                select(Run).where(
                    Run.project_id == project_id, Run.idempotency_key == idempotency_key
                )
            ).scalar_one_or_none()
            if old:
                if old.intent_hash != fingerprint:
                    raise DomainError("IDEMPOTENCY_CONFLICT")
                return cast(RunRecord, dto(old))
            if parent_run_id and self._get(s, Run, parent_run_id).project_id != project_id:
                raise DomainError("PARENT_PROJECT_MISMATCH")
            run = Run(
                id=identifier(),
                project_id=project_id,
                snapshot_id=snapshot_id,
                idempotency_key=idempotency_key,
                intent_hash=fingerprint,
                parent_run_id=parent_run_id,
                limits=limits,
                status="QUEUED",
                plan_revision=0,
                cancel_requested=False,
                created_at=self.clock(),
                updated_at=self.clock(),
            )
            s.add(run)
            s.flush()
            self._add_step(s, run, "inspect", "inspect")
            self._event(s, run, "RUN_CREATED")
            return cast(RunRecord, dto(run))

    def get_run(self, run_id: str) -> RunRecord:
        with Session(self.db) as s:
            return cast(RunRecord, dto(self._get(s, Run, run_id)))

    def list_runs(self, project_id: str) -> list[Row]:
        with Session(self.db) as s:
            return [
                dto(r)
                for r in s.scalars(
                    select(Run).where(Run.project_id == project_id).order_by(Run.created_at)
                )
            ]

    def list_pending_cancel_runs(self) -> list[Row]:
        with Session(self.db) as s:
            return [
                dto(r)
                for r in s.scalars(
                    select(Run).where(Run.cancel_requested.is_(True), Run.status != "CANCELLED")
                )
            ]

    def events(self, run_id: str) -> list[Row]:
        with Session(self.db) as s:
            return [
                dto(e)
                for e in s.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.id))
            ]

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
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.status in TERMINAL or run.cancel_requested:
                raise DomainError("RUN_NOT_EXECUTABLE")
            if attempt_id is not None:
                current_run, _, _ = self._current(s, attempt_id, generation)
                if current_run.id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            return dto(self._add_step(s, run, logical_key, kind, payload, position))

    def get_step(self, step_id: str) -> Row:
        with Session(self.db) as s:
            return dto(self._get(s, Step, step_id))

    def list_steps(self, run_id: str) -> list[Row]:
        with Session(self.db) as s:
            return [
                dto(x)
                for x in s.scalars(
                    select(Step).where(Step.run_id == run_id).order_by(Step.position, Step.id)
                )
            ]

    def _claim_dto(self, run: Run, step: Step, attempt: Attempt) -> RunClaim:
        return {
            "run": cast(RunRecord, dto(run)),
            "step": cast(StepRecord, dto(step)),
            "attempt": cast(AttemptRecord, dto(attempt)),
        }

    def claim_next(self, worker_id: str, lease_seconds: int = 60) -> RunClaim | None:
        candidate: Run | None = None
        if lease_seconds <= 0:
            raise DomainError("INVALID_LEASE")
        with self._write() as (s, lock):
            if lock.active_run_id:
                run = self._get(s, Run, lock.active_run_id)
                active = s.execute(
                    select(Attempt)
                    .join(Step)
                    .where(Step.run_id == run.id, Attempt.status == "ACTIVE")
                ).scalar_one_or_none()
                if active:
                    return None  # Expiry never implicitly grants permission to retry.
                if run.cancel_requested or run.status in TERMINAL | WAITING:
                    self._release(lock, run)
                else:
                    candidate = run
            if not lock.active_run_id:
                candidate = (
                    s.execute(
                        select(Run)
                        .where(Run.status == "QUEUED", Run.cancel_requested.is_(False))
                        .order_by(Run.created_at, Run.id)
                        .with_for_update(skip_locked=True)
                    )
                    .scalars()
                    .first()
                )
            if candidate is None:
                return None
            run = candidate
            step = (
                s.execute(
                    select(Step)
                    .where(Step.run_id == run.id, Step.status == "PENDING")
                    .order_by(Step.position, Step.id)
                    .with_for_update()
                )
                .scalars()
                .first()
            )
            if not step:
                run.status = "AWAITING_HANDOFF"
                self._release(lock, run)
                return None
            if step.attempts_used >= MAX_ATTEMPTS:
                run.status, run.reason = "FAILED", "RETRY_LIMIT_EXHAUSTED"
                self._release(lock, run)
                return None
            if step.kind == "transform":
                approval = s.get(
                    Approval,
                    s.scalar(
                        select(Approval.id).where(
                            Approval.run_id == run.id, Approval.plan_revision == run.plan_revision
                        )
                    ),
                )
                if not approval or not approval.approved:
                    run.status = "AWAITING_PLAN_APPROVAL"
                    self._release(lock, run)
                    return None
            step.attempts_used += 1
            step.generation += 1
            step.status = "RUNNING"
            attempt = Attempt(
                id=identifier(),
                step_id=step.id,
                number=step.attempts_used,
                generation=step.generation,
                worker_id=worker_id,
                lease_until=self.clock() + lease_seconds,
                status="ACTIVE",
                execution_confirmed=False,
                started_at=self.clock(),
            )
            s.add(attempt)
            lock.active_run_id = run.id
            run.status, run.stage, run.reason = "RUNNING", step.kind, None
            self._event(
                s,
                run,
                "ATTEMPT_STARTED",
                {
                    "step_id": step.id,
                    "attempt_id": attempt.id,
                    "number": attempt.number,
                    "generation": attempt.generation,
                },
            )
            s.flush()
            return self._claim_dto(run, step, attempt)

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
        with self._write() as (s, _):
            _, _, attempt = self._current(s, attempt_id, generation)
            if attempt.worker_id != worker_id or lease_seconds <= 0:
                raise DomainError("INVALID_LEASE_OWNER")
            attempt.lease_until = self.clock() + lease_seconds
            return dto(attempt)

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
        if outcome not in {"SUCCEEDED", "FAILED"}:
            raise DomainError("INVALID_OUTCOME")
        with self._write() as (s, lock):
            saved = self._get(s, Attempt, attempt_id)
            saved_step = self._get(s, Step, saved.step_id)
            if (
                saved.status in {"SUCCEEDED", "FAILED"}
                and saved.generation == generation
                and saved_step.generation == generation
            ):
                if (
                    saved.status != outcome
                    or saved.result != (result or {})
                    or saved.reason != reason
                ):
                    raise DomainError("ATTEMPT_RESULT_CONFLICT")
                return cast(RunRecord, dto(self._get(s, Run, saved_step.run_id)))
            run, step, attempt = self._current(s, attempt_id, generation)
            unresolved = s.scalar(
                select(func.count())
                .select_from(Command)
                .where(Command.attempt_id == attempt.id, Command.status.not_in(COMMAND_TERMINAL))
            )
            if not execution_confirmed or unresolved or attempt.reconciliation_required:
                raise DomainError("EXECUTION_OUTCOME_UNKNOWN")
            attempt.status, attempt.result, attempt.reason = outcome, result or {}, reason
            attempt.execution_confirmed, attempt.finished_at = True, self.clock()
            step.result = result or {}
            # The decision is pure; all row updates and the event remain in
            # this one locked transaction. Querying can autoflush SUCCEEDED,
            # exactly as before, but cannot commit a partial transition.
            pending = (
                bool(
                    s.scalar(
                        select(func.count())
                        .select_from(Step)
                        .where(Step.run_id == run.id, Step.status == "PENDING")
                    )
                )
                if outcome == "SUCCEEDED"
                else False
            )
            decided = completion(
                outcome,
                pending=pending,
                retryable=retryable,
                reason=reason,
                attempts_used=step.attempts_used,
                previous_reason=run.reason,
                previous_original_reason=run.original_reason,
            )
            step.status, run.status = decided.step_status, decided.run_status
            run.reason, run.original_reason = decided.reason, decided.original_reason
            self._release(lock, run)
            self._event(
                s,
                run,
                "ATTEMPT_FINISHED",
                {
                    "attempt_id": attempt.id,
                    "outcome": outcome,
                    "reason": reason,
                    "retryable": retryable,
                },
            )
            s.flush()
            return cast(RunRecord, dto(run))

    def set_waiting(
        self,
        attempt_id: str | None,
        generation: int | None,
        status: str,
        reason: str | None,
        payload: Row | None = None,
    ) -> RunRecord:
        if status not in WAITING:
            raise DomainError("INVALID_WAIT_STATUS")
        with self._write() as (s, lock):
            run, step, attempt = self._current(s, attempt_id, generation)
            if s.scalar(
                select(func.count())
                .select_from(Command)
                .where(Command.attempt_id == attempt.id, Command.status.not_in(COMMAND_TERMINAL))
            ):
                raise DomainError("EXECUTION_OUTCOME_UNKNOWN")
            attempt.status, attempt.finished_at, attempt.execution_confirmed = (
                "WAITING",
                self.clock(),
                True,
            )
            step.status = "WAITING"
            step.result = payload or {}
            run.status, run.reason = status, reason
            self._release(lock, run)
            self._event(
                s, run, "WAITING", {"status": status, "reason": reason, "details": payload or {}}
            )
            return cast(RunRecord, dto(run))

    def resume_run(self, run_id: str, payload: Row | None = None) -> RunRecord:
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.status != "NEEDS_INPUT" or run.cancel_requested:
                raise DomainError("RUN_NOT_RESUMABLE")
            for step in s.scalars(
                select(Step).where(Step.run_id == run_id, Step.status == "WAITING")
            ):
                step.status = "PENDING"
                if payload:
                    step.payload = {**step.payload, **payload}
            run.status, run.reason = "QUEUED", None
            self._event(s, run, "INPUT_PROVIDED")
            return cast(RunRecord, dto(run))

    def publish_plan(
        self,
        run_id: str,
        plan: Row,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row:
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.cancel_requested or run.status in TERMINAL:
                raise DomainError("RUN_NOT_EXECUTABLE")
            if attempt_id is not None:
                current_run, _, _ = self._current(s, attempt_id, generation)
                if current_run.id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            current = s.get(Plan, (run_id, run.plan_revision))
            if current and digest(current.document) == digest(plan):
                return {
                    "revision": current.revision,
                    "snapshot_id": current.snapshot_id,
                    "plan": current.document,
                }
            # A revised plan preserves stable step identities/counters.
            run.plan_revision += 1
            p = Plan(
                run_id=run.id,
                revision=run.plan_revision,
                snapshot_id=run.snapshot_id,
                document=plan,
                created_at=self.clock(),
            )
            s.add(p)
            self._event(s, run, "PLAN_PUBLISHED", {"revision": p.revision})
            return {"revision": p.revision, "snapshot_id": p.snapshot_id, "plan": p.document}

    def get_plan(self, run_id: str) -> Row | None:
        with Session(self.db) as s:
            run = self._get(s, Run, run_id)
            p = s.get(Plan, (run_id, run.plan_revision))
            return (
                {"revision": p.revision, "snapshot_id": p.snapshot_id, "plan": p.document}
                if p
                else None
            )

    def decide_plan(
        self,
        run_id: str,
        project_id: str,
        snapshot_id: str,
        plan_revision: int,
        approved: bool,
        actor: str,
    ) -> RunRecord:
        with self._write() as (s, lock):
            run = self._get(s, Run, run_id)
            if (run.project_id, run.snapshot_id, run.plan_revision) != (
                project_id,
                snapshot_id,
                plan_revision,
            ) or not plan_revision:
                raise DomainError("PLAN_VERSION_MISMATCH")
            if run.cancel_requested or run.status in TERMINAL:
                raise DomainError("RUN_NOT_EXECUTABLE")
            old = s.execute(
                select(Approval).where(
                    Approval.run_id == run_id, Approval.plan_revision == plan_revision
                )
            ).scalar_one_or_none()
            if old:
                if old.approved != approved:
                    raise DomainError("DECISION_CONFLICT")
                return cast(RunRecord, dto(run))
            if run.status != "AWAITING_PLAN_APPROVAL":
                raise DomainError("NOT_AWAITING_PLAN_APPROVAL")
            s.add(
                Approval(
                    id=identifier(),
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    plan_revision=plan_revision,
                    approved=approved,
                    actor=actor,
                    created_at=self.clock(),
                )
            )
            if approved:
                for waiting in s.scalars(
                    select(Step).where(
                        Step.run_id == run_id,
                        Step.kind.in_(["inspect", "plan"]),
                        Step.status == "WAITING",
                    )
                ):
                    waiting.status = "SUCCEEDED"
                plan = self._get(s, Plan, (run_id, plan_revision)).document
                works = plan.get("steps")
                if not isinstance(works, list) or any(
                    not isinstance(work, dict)
                    or not isinstance(work.get("id"), str)
                    or not work["id"]
                    for work in works
                ):
                    raise DomainError("PLAN_WORK_IDENTITY_REQUIRED")
                work_ids = [work["id"] for work in works]
                if len(work_ids) != len(set(work_ids)):
                    raise DomainError("PLAN_WORK_IDENTITY_CONFLICT")
                existing_works = list(
                    s.scalars(
                        select(Step)
                        .where(Step.run_id == run_id, Step.kind == "transform")
                        .order_by(Step.position)
                    )
                )
                if len(works) < len(existing_works):
                    raise DomainError("PLAN_WORK_IDENTITY_CONFLICT")
                # MVP plans may append genuinely new, explicitly approved work.
                # Existing machine identities and their positions are immutable:
                # neither insertion, reorder nor an ID rename creates new budget.
                # Human-readable title changes are harmless and retain counters.
                for index, existing in enumerate(existing_works):
                    if (
                        existing.logical_key != f"transform:{index}"
                        or works[index]["id"] != existing.payload["work_id"]
                    ):
                        raise DomainError("PLAN_WORK_IDENTITY_CONFLICT")
                for index, work in enumerate(works):
                    payload = {
                        "plan_revision": plan_revision,
                        "work_id": work.get("id", str(index)),
                        "plan_step": work,
                    }
                    step = self._add_step(s, run, f"transform:{index}", "transform", payload)
                    if step.status == "RUNNING":
                        raise DomainError("PLAN_WORK_ACTIVE")
                    previous_work = step.payload.get("plan_step", {})

                    def substantive(value: Row) -> Row:
                        return {k: v for k, v in value.items() if k not in {"id", "title"}}

                    changed_work = digest(substantive(previous_work)) != digest(substantive(work))
                    if step.status in {"WAITING", "FAILED", "PENDING"} or changed_work:
                        step.status, step.payload, step.plan_revision = (
                            "PENDING",
                            payload,
                            plan_revision,
                        )
                if not works:
                    self._add_step(
                        s,
                        run,
                        "verify",
                        "verify",
                        {"plan_revision": plan_revision, "no_transform": True},
                    )
                run.status, run.reason = "QUEUED", None
            else:
                run.status, run.reason = "NEEDS_INPUT", "PLAN_REJECTED"
            self._release(lock, run)
            self._event(
                s,
                run,
                "PLAN_DECISION",
                {"revision": plan_revision, "approved": approved, "actor": actor},
            )
            return cast(RunRecord, dto(run))

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
        """Take over the SAME attempt for readback; no new attempt or command is created."""
        with self._write() as (s, lock):
            run = self._get(s, Run, run_id)
            if run.cancel_requested or run.status != "RUNNING":
                raise DomainError("RUN_NOT_RECOVERABLE")
            if lock.active_run_id not in {None, run_id}:
                raise DomainError("OTHER_RUN_ACTIVE")
            attempt = s.execute(
                select(Attempt).join(Step).where(Step.run_id == run_id, Attempt.status == "ACTIVE")
            ).scalar_one_or_none()
            if not attempt or attempt.lease_until > self.clock():
                raise DomainError("LEASE_NOT_EXPIRED")
            step = self._get(s, Step, attempt.step_id)
            step.generation += 1
            attempt.generation, attempt.worker_id = step.generation, worker_id
            attempt.lease_until = self.clock() + lease_seconds
            # The caller may read existing commands under the new generation. Unknown
            # commands still block finish/retry until trusted readback or confirmed stop.
            if stop_confirmed:
                for command in s.scalars(
                    select(Command).where(
                        Command.attempt_id == attempt.id, Command.status.not_in(COMMAND_TERMINAL)
                    )
                ):
                    command.status, command.result = (
                        "STOPPED",
                        {"stop_confirmed": True, "outcome_before_stop": "UNKNOWN"},
                    )
            attempt.execution_confirmed = bool(execution_confirmed or stop_confirmed)
            attempt.reconciliation_required = not bool(stop_confirmed)
            lock.active_run_id = run.id
            self._event(
                s,
                run,
                "ATTEMPT_RECOVERED",
                {
                    "attempt_id": attempt.id,
                    "generation": attempt.generation,
                    "stop_confirmed": stop_confirmed,
                },
            )
            return self._claim_dto(run, step, attempt)

    def request_cancel(self, run_id: str) -> RunRecord:
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.status in TERMINAL:
                return cast(RunRecord, dto(run))
            run.cancel_requested = True
            self._event(s, run, "CANCEL_REQUESTED")
            return cast(RunRecord, dto(run))

    def confirm_cancel(self, run_id: str, stop_confirmed: bool = False) -> RunRecord:
        with self._write() as (s, lock):
            run = self._get(s, Run, run_id)
            if not run.cancel_requested or not stop_confirmed:
                raise DomainError("STOP_NOT_CONFIRMED")
            if run.status == "CANCELLED":
                return cast(RunRecord, dto(run))
            for attempt in s.scalars(
                select(Attempt).join(Step).where(Step.run_id == run_id, Attempt.status == "ACTIVE")
            ):
                attempt.status, attempt.finished_at = "CANCELLED", self.clock()
                step = self._get(s, Step, attempt.step_id)
                step.generation += 1
                step.status = "CANCELLED"
                for c in s.scalars(
                    select(Command).where(
                        Command.attempt_id == attempt.id, Command.status.not_in(COMMAND_TERMINAL)
                    )
                ):
                    c.status = "STOPPED"
            run.status, run.reason = "CANCELLED", "CANCELLED"
            self._release(lock, run)
            self._event(s, run, "CANCEL_CONFIRMED")
            return cast(RunRecord, dto(run))

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
        with Session(self.db) as s:
            return [
                dto(r)
                for r in s.scalars(
                    select(Run)
                    .join(Step)
                    .join(Attempt)
                    .where(
                        Run.status == "RUNNING",
                        Run.cancel_requested.is_(False),
                        Attempt.status == "ACTIVE",
                        Attempt.lease_until <= self.clock(),
                    )
                    .distinct()
                )
            ]

    def confirm_reconciliation(
        self, attempt_id: str | None, generation: int | None, stop_confirmed: bool = False
    ) -> Row:
        with self._write() as (s, _):
            run, _, attempt = self._current(s, attempt_id, generation)
            unresolved = list(
                s.scalars(
                    select(Command).where(
                        Command.attempt_id == attempt_id, Command.status.not_in(COMMAND_TERMINAL)
                    )
                )
            )
            if unresolved and not stop_confirmed:
                raise DomainError("EXECUTION_OUTCOME_UNKNOWN")
            if stop_confirmed:
                for c in unresolved:
                    c.status, c.result = (
                        "STOPPED",
                        {"stop_confirmed": True, "previous_outcome": "UNKNOWN"},
                    )
            attempt.reconciliation_required = False
            attempt.execution_confirmed = True
            self._event(
                s,
                run,
                "EXECUTION_RECONCILED",
                {"attempt_id": attempt_id, "stop_confirmed": stop_confirmed},
            )
            return dto(attempt)

    def update_step_input(self, run_id: str, logical_key: str, payload: Row) -> Row:
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.cancel_requested or run.status in TERMINAL:
                raise DomainError("RUN_NOT_EXECUTABLE")
            step = s.execute(
                select(Step).where(Step.run_id == run_id, Step.logical_key == logical_key)
            ).scalar_one_or_none()
            if not step or step.status not in {"WAITING", "PENDING"}:
                raise DomainError("STEP_NOT_WAITING")
            step.payload = {**step.payload, **payload}
            return dto(step)

    def supply_input(self, run_id: str, step_id: str, payload: Row) -> RunRecord:
        """Operator supplies a held-out AI task, never a transformer-visible fixture."""
        with self._write() as (s, _):
            run, step = self._get(s, Run, run_id), self._get(s, Step, step_id)
            if step.run_id != run_id:
                raise DomainError("STEP_RUN_MISMATCH")
            if (
                run.cancel_requested
                or run.status != "NEEDS_INPUT"
                or step.status != "WAITING"
                or step.kind != "ai_handoff"
            ):
                raise DomainError("INPUT_NOT_EXPECTED")
            step.payload = {**step.payload, **payload}
            step.status = "PENDING"
            run.status, run.reason = "QUEUED", None
            self._event(s, run, "HELDOUT_INPUT_PROVIDED", {"step_id": step_id})
            return cast(RunRecord, dto(run))

    def define_parameter(self, entity_type: str, name: str, value_type: str) -> Row:
        return self.metadata.define_parameter(entity_type, name, value_type)

    def set_entity_value(
        self, entity_id: str, name: str, value: Any, provenance: Row | None
    ) -> Row:
        return self.metadata.set_entity_value(entity_id, name, value, provenance)

    def get_entity(self, entity_id: str) -> Row:
        return self.metadata.get_entity(entity_id)

    def assert_current(self, attempt_id: str | None, generation: int | None) -> RunClaim:
        """Read admission guard; dispatch still needs its persisted command identity."""
        with Session(self.db) as s:
            run, step, attempt = self._current(s, attempt_id, generation)
            return self._claim_dto(run, step, attempt)

    def bind_model_profile(
        self, run_id: str, profile_manifest: Row, *, attempt_id: str | None, generation: int | None
    ) -> Row:
        """Bind one full model configuration before the run's first dispatch.

        Only a secret reference belongs in this manifest. The resolved key never
        enters workflow state, events, project context or runner payloads.
        """
        if (
            not isinstance(profile_manifest, dict)
            or not isinstance(profile_manifest.get("version"), str)
            or not profile_manifest["version"]
        ):
            raise DomainError("INVALID_MODEL_PROFILE")
        if any(key in profile_manifest for key in {"api_key", "authorization", "secret", "token"}):
            raise DomainError("INVALID_MODEL_PROFILE")
        normalized: Row = dict(profile_manifest)
        capabilities = normalized.get("capabilities")
        if capabilities is not None:
            if not isinstance(capabilities, (list, tuple, set, frozenset)) or any(
                not isinstance(value, str) for value in capabilities
            ):
                raise DomainError("INVALID_MODEL_PROFILE")
            normalized["capabilities"] = sorted(set(capabilities))
        try:
            # Detach caller-owned containers and reject unserializable/NaN data.
            normalized = json.loads(json.dumps(normalized, sort_keys=True, allow_nan=False))
            profile_hash = digest(normalized)
        except (TypeError, ValueError):
            raise DomainError("INVALID_MODEL_PROFILE") from None
        with self._write() as (s, _):
            run, _, _ = self._current(s, attempt_id, generation)
            if run.id != run_id:
                raise DomainError("ATTEMPT_RUN_MISMATCH")
            selected_version = run.limits.get("model_profile_version")
            if selected_version and selected_version != normalized["version"]:
                raise DomainError("MODEL_PROFILE_CHANGED")
            bound = run.limits.get("model_profile")
            if bound is not None:
                if digest(bound) != profile_hash:
                    raise DomainError("MODEL_PROFILE_CHANGED")
                return cast(Row, bound)
            run.limits = {**run.limits, "model_profile": normalized}
            self._event(
                s,
                run,
                "MODEL_PROFILE_BOUND",
                {"profile_hash": profile_hash, "version": normalized["version"]},
            )
            return normalized
