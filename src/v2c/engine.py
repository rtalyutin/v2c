"""Durable, fenced workflow. External I/O must happen outside these transactions."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import make_engine, transaction
from .models import (Approval, Artifact, Attempt, Base, Command, Entity, EntityParameter,
                     EntityParameterValue, EntityType, Event, Plan, Run, RuntimeLock,
                     Snapshot, Step, UsageEvent, VerificationResult)

TERMINAL = {"READY", "FAILED", "CANCELLED"}
WAITING = {"NEEDS_INPUT", "AWAITING_PLAN_APPROVAL", "AWAITING_HANDOFF"}
COMMAND_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "STOPPED", "DESTROYED"}
NON_RETRYABLE = {"AUTH", "INVALID_CONFIG", "UNSUPPORTED", "UNSUPPORTED_CAPABILITY", "MISSING_INPUT", "ACCESS_DENIED", "PLAN_REJECTED", "CANCELLED"}
REQUIRED_VERIFICATIONS = {"final_regression", "ai_handoff", "human_handoff", "recovery"}


class DomainError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def identifier():
    return str(uuid.uuid4())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def dto(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


class Engine:
    def __init__(self, database_url, *, clock=time.time):
        self.db = make_engine(database_url)
        self.clock = clock

    def init_schema(self):
        """Disposable developer/test DB only; production uses Alembic upgrade head."""
        Base.metadata.create_all(self.db)
        self.seed_schema()

    def seed_schema(self):
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

    @contextmanager
    def _write(self):
        with transaction(self.db) as s:
            lock = s.execute(select(RuntimeLock).where(RuntimeLock.id == 1).with_for_update()).scalar_one_or_none()
            if lock is None:
                raise DomainError("SCHEMA_NOT_INITIALIZED")
            yield s, lock

    @staticmethod
    def _get(s, model, key):
        value = s.get(model, key)
        if value is None:
            raise DomainError("NOT_FOUND")
        return value

    def _event(self, s, run, kind, payload=None):
        run.updated_at = self.clock()
        s.add(Event(run_id=run.id, kind=kind, payload=payload or {}, created_at=self.clock()))

    @staticmethod
    def _release(lock, run):
        if lock.active_run_id == run.id:
            lock.active_run_id = None

    def create_project(self, owner_id, name):
        if not owner_id or not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise DomainError("INVALID_PROJECT")
        with self._write() as (s, _):
            entity = Entity(id=identifier(), owner_id=owner_id, entity_type="project", created_at=self.clock())
            s.add(entity)
            s.flush()
            s.add(EntityParameterValue(entity_id=entity.id, owner_id=owner_id, entity_type="project", parameter_id="project.name", value_type="text", value_text=name, is_null=False, provenance={"source": "operator"}))
            return {"id": entity.id, "owner_id": owner_id, "name": name, "created_at": entity.created_at}

    def _project(self, s, entity):
        if entity.entity_type != "project":
            raise DomainError("NOT_FOUND")
        value = s.get(EntityParameterValue, (entity.id, "project.name"))
        return {"id": entity.id, "owner_id": entity.owner_id, "name": value.value_text if value else None, "created_at": entity.created_at}

    def get_project(self, project_id):
        with Session(self.db) as s:
            return self._project(s, self._get(s, Entity, project_id))

    def list_projects(self, owner_id):
        with Session(self.db) as s:
            return [self._project(s, p) for p in s.scalars(select(Entity).where(Entity.owner_id == owner_id, Entity.entity_type == "project").order_by(Entity.created_at))]

    def create_entity(self, owner_id, entity_type, values, provenance=None):
        with self._write() as (s, _):
            self._get(s, EntityType, entity_type)
            entity = Entity(id=identifier(), owner_id=owner_id, entity_type=entity_type, created_at=self.clock())
            s.add(entity)
            s.flush()
            for name, value in values.items():
                self._set_value(s, entity, name, value, provenance or {})
            return {**dto(entity), "values": values}

    def _set_value(self, s, entity, name, value, provenance):
        p = s.execute(select(EntityParameter).where(EntityParameter.entity_type == entity.entity_type, EntityParameter.name == name)).scalar_one_or_none()
        if p is None:
            raise DomainError("UNKNOWN_PARAMETER")
        types = {"text": str, "integer": int, "number": (int, float), "boolean": bool, "json": (dict, list), "reference": str}
        if value is not None and (not isinstance(value, types[p.value_type]) or (isinstance(value, bool) and p.value_type in {"integer", "number"})):
            raise DomainError("PARAMETER_TYPE_MISMATCH")
        if value is not None and p.value_type == "reference":
            ref = self._get(s, Entity, value)
            if ref.owner_id != entity.owner_id:
                raise DomainError("REFERENCE_OWNER_MISMATCH")
        old = s.get(EntityParameterValue, (entity.id, p.id))
        if old:
            s.delete(old)
            s.flush()
        column = "value_" + p.value_type
        s.add(EntityParameterValue(entity_id=entity.id, owner_id=entity.owner_id, entity_type=entity.entity_type, parameter_id=p.id, value_type=p.value_type, is_null=value is None, provenance=provenance, **({column: value} if value is not None else {})))

    def create_snapshot(self, project_id, manifest, artifact_id=None):
        with self._write() as (s, _):
            self._project(s, self._get(s, Entity, project_id))
            snapshot = Snapshot(id=identifier(), project_id=project_id, digest=digest(manifest), manifest=manifest, artifact_id=artifact_id, created_at=self.clock())
            s.add(snapshot)
            return dto(snapshot)

    def get_snapshot(self, snapshot_id):
        with Session(self.db) as s:
            return dto(self._get(s, Snapshot, snapshot_id))

    def list_snapshots(self, project_id):
        with Session(self.db) as s:
            return [dto(x) for x in s.scalars(select(Snapshot).where(Snapshot.project_id == project_id).order_by(Snapshot.created_at))]

    def _add_step(self, s, run, logical_key, kind, payload=None, position=None):
        existing = s.execute(select(Step).where(Step.run_id == run.id, Step.logical_key == logical_key)).scalar_one_or_none()
        if existing:
            if existing.kind != kind:
                raise DomainError("STEP_IDENTITY_CONFLICT")
            return existing
        if position is None:
            position = (s.scalar(select(func.max(Step.position)).where(Step.run_id == run.id)) or 0) + 1
        step = Step(id=identifier(), run_id=run.id, logical_key=logical_key, kind=kind, payload=payload or {}, position=position, status="PENDING", attempts_used=0, generation=0, plan_revision=run.plan_revision)
        s.add(step)
        s.flush()
        return step

    def create_run(self, project_id, snapshot_id, idempotency_key, limits=None, parent_run_id=None):
        if not idempotency_key or len(idempotency_key) > 200:
            raise DomainError("INVALID_IDEMPOTENCY_KEY")
        limits = limits or {}
        fingerprint = digest({"snapshot_id": snapshot_id, "limits": limits, "parent_run_id": parent_run_id})
        with self._write() as (s, _):
            snapshot = self._get(s, Snapshot, snapshot_id)
            if snapshot.project_id != project_id:
                raise DomainError("SNAPSHOT_PROJECT_MISMATCH")
            old = s.execute(select(Run).where(Run.project_id == project_id, Run.idempotency_key == idempotency_key)).scalar_one_or_none()
            if old:
                if old.intent_hash != fingerprint:
                    raise DomainError("IDEMPOTENCY_CONFLICT")
                return dto(old)
            if parent_run_id and self._get(s, Run, parent_run_id).project_id != project_id:
                raise DomainError("PARENT_PROJECT_MISMATCH")
            run = Run(id=identifier(), project_id=project_id, snapshot_id=snapshot_id, idempotency_key=idempotency_key, intent_hash=fingerprint, parent_run_id=parent_run_id, limits=limits, status="QUEUED", plan_revision=0, cancel_requested=False, created_at=self.clock(), updated_at=self.clock())
            s.add(run)
            s.flush()
            self._add_step(s, run, "inspect", "inspect")
            self._event(s, run, "RUN_CREATED")
            return dto(run)

    def get_run(self, run_id):
        with Session(self.db) as s:
            return dto(self._get(s, Run, run_id))

    def list_runs(self, project_id):
        with Session(self.db) as s:
            return [dto(r) for r in s.scalars(select(Run).where(Run.project_id == project_id).order_by(Run.created_at))]

    def list_pending_cancel_runs(self):
        with Session(self.db) as s:
            return [dto(r) for r in s.scalars(select(Run).where(Run.cancel_requested.is_(True), Run.status != "CANCELLED"))]

    def events(self, run_id):
        with Session(self.db) as s:
            return [dto(e) for e in s.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.id))]

    def add_step(self, run_id, logical_key, kind, payload=None, position=None, *, attempt_id=None, generation=None):
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.status in TERMINAL or run.cancel_requested:
                raise DomainError("RUN_NOT_EXECUTABLE")
            if attempt_id is not None:
                current_run, _, _ = self._current(s, attempt_id, generation)
                if current_run.id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            return dto(self._add_step(s, run, logical_key, kind, payload, position))

    def get_step(self, step_id):
        with Session(self.db) as s:
            return dto(self._get(s, Step, step_id))

    def list_steps(self, run_id):
        with Session(self.db) as s:
            return [dto(x) for x in s.scalars(select(Step).where(Step.run_id == run_id).order_by(Step.position, Step.id))]

    def _claim_dto(self, run, step, attempt):
        return {"run": dto(run), "step": dto(step), "attempt": dto(attempt)}

    def claim_next(self, worker_id, lease_seconds=60):
        if lease_seconds <= 0:
            raise DomainError("INVALID_LEASE")
        with self._write() as (s, lock):
            if lock.active_run_id:
                run = self._get(s, Run, lock.active_run_id)
                active = s.execute(select(Attempt).join(Step).where(Step.run_id == run.id, Attempt.status == "ACTIVE")).scalar_one_or_none()
                if active:
                    return None  # Expiry never implicitly grants permission to retry.
                if run.cancel_requested or run.status in TERMINAL | WAITING:
                    self._release(lock, run)
                else:
                    candidate = run
            if not lock.active_run_id:
                candidate = s.execute(select(Run).where(Run.status == "QUEUED", Run.cancel_requested.is_(False)).order_by(Run.created_at, Run.id).with_for_update(skip_locked=True)).scalars().first()
            if candidate is None:
                return None
            run = candidate
            step = s.execute(select(Step).where(Step.run_id == run.id, Step.status == "PENDING").order_by(Step.position, Step.id).with_for_update()).scalars().first()
            if not step:
                run.status = "AWAITING_HANDOFF"
                self._release(lock, run)
                return None
            if step.attempts_used >= 5:
                run.status, run.reason = "FAILED", "RETRY_LIMIT_EXHAUSTED"
                self._release(lock, run)
                return None
            if step.kind == "transform":
                approval = s.get(Approval, s.scalar(select(Approval.id).where(Approval.run_id == run.id, Approval.plan_revision == run.plan_revision)))
                if not approval or not approval.approved:
                    run.status = "AWAITING_PLAN_APPROVAL"
                    self._release(lock, run)
                    return None
            step.attempts_used += 1
            step.generation += 1
            step.status = "RUNNING"
            attempt = Attempt(id=identifier(), step_id=step.id, number=step.attempts_used, generation=step.generation, worker_id=worker_id, lease_until=self.clock() + lease_seconds, status="ACTIVE", execution_confirmed=False, started_at=self.clock())
            s.add(attempt)
            lock.active_run_id = run.id
            run.status, run.stage, run.reason = "RUNNING", step.kind, None
            self._event(s, run, "ATTEMPT_STARTED", {"step_id": step.id, "attempt_id": attempt.id, "number": attempt.number, "generation": attempt.generation})
            s.flush()
            return self._claim_dto(run, step, attempt)

    def _current(self, s, attempt_id, generation, *, allow_cancel=False, allow_expired=False):
        attempt = self._get(s, Attempt, attempt_id)
        step = self._get(s, Step, attempt.step_id)
        run = self._get(s, Run, step.run_id)
        if attempt.status != "ACTIVE" or attempt.generation != generation or step.generation != generation:
            raise DomainError("STALE_GENERATION")
        if not allow_expired and attempt.lease_until <= self.clock():
            raise DomainError("LEASE_EXPIRED")
        if not allow_cancel and (run.cancel_requested or run.status in TERMINAL):
            raise DomainError("RUN_NOT_EXECUTABLE")
        return run, step, attempt

    def heartbeat(self, attempt_id, generation, worker_id, lease_seconds=60):
        with self._write() as (s, _):
            _, _, attempt = self._current(s, attempt_id, generation)
            if attempt.worker_id != worker_id or lease_seconds <= 0:
                raise DomainError("INVALID_LEASE_OWNER")
            attempt.lease_until = self.clock() + lease_seconds
            return dto(attempt)

    def finish_attempt(self, attempt_id, generation, result=None, outcome="SUCCEEDED", reason=None, retryable=False, execution_confirmed=True):
        if outcome not in {"SUCCEEDED", "FAILED"}:
            raise DomainError("INVALID_OUTCOME")
        with self._write() as (s, lock):
            saved = self._get(s, Attempt, attempt_id)
            saved_step = self._get(s, Step, saved.step_id)
            if saved.status in {"SUCCEEDED", "FAILED"} and saved.generation == generation and saved_step.generation == generation:
                if saved.status != outcome or saved.result != (result or {}) or saved.reason != reason:
                    raise DomainError("ATTEMPT_RESULT_CONFLICT")
                return dto(self._get(s, Run, saved_step.run_id))
            run, step, attempt = self._current(s, attempt_id, generation)
            unresolved = s.scalar(select(func.count()).select_from(Command).where(Command.attempt_id == attempt.id, Command.status.not_in(COMMAND_TERMINAL)))
            if not execution_confirmed or unresolved or attempt.reconciliation_required:
                raise DomainError("EXECUTION_OUTCOME_UNKNOWN")
            attempt.status, attempt.result, attempt.reason = outcome, result or {}, reason
            attempt.execution_confirmed, attempt.finished_at = True, self.clock()
            step.result = result or {}
            if outcome == "SUCCEEDED":
                step.status = "SUCCEEDED"
                pending = s.scalar(select(func.count()).select_from(Step).where(Step.run_id == run.id, Step.status == "PENDING"))
                run.status = "QUEUED" if pending else "AWAITING_HANDOFF"
            elif retryable and reason not in NON_RETRYABLE and step.attempts_used < 5:
                step.status, run.status, run.reason = "PENDING", "QUEUED", reason
            else:
                step.status = "FAILED"
                run.original_reason = reason
                if retryable and reason not in NON_RETRYABLE and step.attempts_used >= 5:
                    run.status, run.reason = "FAILED", "RETRY_LIMIT_EXHAUSTED"
                elif reason in {"MISSING_INPUT", "AUTH", "INVALID_CONFIG", "ACCESS_DENIED"}:
                    step.status, run.status, run.reason = "WAITING", "NEEDS_INPUT", reason
                else:
                    run.status, run.reason = "FAILED", reason or "STEP_FAILED"
            self._release(lock, run)
            self._event(s, run, "ATTEMPT_FINISHED", {"attempt_id": attempt.id, "outcome": outcome, "reason": reason, "retryable": retryable})
            s.flush()
            return dto(run)

    def set_waiting(self, attempt_id, generation, status, reason, payload=None):
        if status not in WAITING:
            raise DomainError("INVALID_WAIT_STATUS")
        with self._write() as (s, lock):
            run, step, attempt = self._current(s, attempt_id, generation)
            if s.scalar(select(func.count()).select_from(Command).where(Command.attempt_id == attempt.id, Command.status.not_in(COMMAND_TERMINAL))):
                raise DomainError("EXECUTION_OUTCOME_UNKNOWN")
            attempt.status, attempt.finished_at, attempt.execution_confirmed = "WAITING", self.clock(), True
            step.status = "WAITING"
            step.result = payload or {}
            run.status, run.reason = status, reason
            self._release(lock, run)
            self._event(s, run, "WAITING", {"status": status, "reason": reason, "details": payload or {}})
            return dto(run)

    def resume_run(self, run_id, payload=None):
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.status != "NEEDS_INPUT" or run.cancel_requested:
                raise DomainError("RUN_NOT_RESUMABLE")
            for step in s.scalars(select(Step).where(Step.run_id == run_id, Step.status == "WAITING")):
                step.status = "PENDING"
                if payload:
                    step.payload = {**step.payload, **payload}
            run.status, run.reason = "QUEUED", None
            self._event(s, run, "INPUT_PROVIDED")
            return dto(run)

    def publish_plan(self, run_id, plan, *, attempt_id=None, generation=None):
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
                return {"revision": current.revision, "snapshot_id": current.snapshot_id, "plan": current.document}
            # A revised plan preserves stable step identities/counters.
            run.plan_revision += 1
            p = Plan(run_id=run.id, revision=run.plan_revision, snapshot_id=run.snapshot_id, document=plan, created_at=self.clock())
            s.add(p)
            self._event(s, run, "PLAN_PUBLISHED", {"revision": p.revision})
            return {"revision": p.revision, "snapshot_id": p.snapshot_id, "plan": p.document}

    def get_plan(self, run_id):
        with Session(self.db) as s:
            run = self._get(s, Run, run_id)
            p = s.get(Plan, (run_id, run.plan_revision))
            return {"revision": p.revision, "snapshot_id": p.snapshot_id, "plan": p.document} if p else None

    def decide_plan(self, run_id, project_id, snapshot_id, plan_revision, approved, actor):
        with self._write() as (s, lock):
            run = self._get(s, Run, run_id)
            if (run.project_id, run.snapshot_id, run.plan_revision) != (project_id, snapshot_id, plan_revision) or not plan_revision:
                raise DomainError("PLAN_VERSION_MISMATCH")
            if run.cancel_requested or run.status in TERMINAL:
                raise DomainError("RUN_NOT_EXECUTABLE")
            old = s.execute(select(Approval).where(Approval.run_id == run_id, Approval.plan_revision == plan_revision)).scalar_one_or_none()
            if old:
                if old.approved != approved:
                    raise DomainError("DECISION_CONFLICT")
                return dto(run)
            if run.status != "AWAITING_PLAN_APPROVAL":
                raise DomainError("NOT_AWAITING_PLAN_APPROVAL")
            s.add(Approval(id=identifier(), run_id=run_id, snapshot_id=snapshot_id, plan_revision=plan_revision, approved=approved, actor=actor, created_at=self.clock()))
            if approved:
                for waiting in s.scalars(select(Step).where(Step.run_id == run_id, Step.kind.in_(["inspect", "plan"]), Step.status == "WAITING")):
                    waiting.status = "SUCCEEDED"
                plan = self._get(s, Plan, (run_id, plan_revision)).document
                works = plan.get("steps")
                if not isinstance(works, list) or any(not isinstance(work, dict) or not isinstance(work.get("id"), str) or not work["id"] for work in works):
                    raise DomainError("PLAN_WORK_IDENTITY_REQUIRED")
                work_ids = [work["id"] for work in works]
                if len(work_ids) != len(set(work_ids)):
                    raise DomainError("PLAN_WORK_IDENTITY_CONFLICT")
                existing_works = list(s.scalars(select(Step).where(Step.run_id == run_id, Step.kind == "transform").order_by(Step.position)))
                if len(works) < len(existing_works):
                    raise DomainError("PLAN_WORK_IDENTITY_CONFLICT")
                # MVP plans may append genuinely new, explicitly approved work.
                # Existing machine identities and their positions are immutable:
                # neither insertion, reorder nor an ID rename creates new budget.
                # Human-readable title changes are harmless and retain counters.
                for index, existing in enumerate(existing_works):
                    if existing.logical_key != f"transform:{index}" or works[index]["id"] != existing.payload["work_id"]:
                        raise DomainError("PLAN_WORK_IDENTITY_CONFLICT")
                for index, work in enumerate(works):
                    payload = {"plan_revision": plan_revision, "work_id": work.get("id", str(index)), "plan_step": work}
                    step = self._add_step(s, run, f"transform:{index}", "transform", payload)
                    if step.status == "RUNNING":
                        raise DomainError("PLAN_WORK_ACTIVE")
                    previous_work = step.payload.get("plan_step", {})
                    substantive = lambda value: {k: v for k, v in value.items() if k not in {"id", "title"}}
                    changed_work = digest(substantive(previous_work)) != digest(substantive(work))
                    if step.status in {"WAITING", "FAILED", "PENDING"} or changed_work:
                        step.status, step.payload, step.plan_revision = "PENDING", payload, plan_revision
                if not works:
                    self._add_step(s, run, "verify", "verify", {"plan_revision": plan_revision, "no_transform": True})
                run.status, run.reason = "QUEUED", None
            else:
                run.status, run.reason = "NEEDS_INPUT", "PLAN_REJECTED"
            self._release(lock, run)
            self._event(s, run, "PLAN_DECISION", {"revision": plan_revision, "approved": approved, "actor": actor})
            return dto(run)

    def prepare_command(self, attempt_id, generation, command_id, request):
        with self._write() as (s, _):
            run, _, attempt = self._current(s, attempt_id, generation)
            fingerprint = digest(request)
            old = s.get(Command, command_id)
            if old:
                if old.attempt_id != attempt_id or old.request_hash != fingerprint:
                    raise DomainError("COMMAND_ID_CONFLICT")
                return dto(old)
            if attempt.reconciliation_required:
                raise DomainError("RECONCILIATION_REQUIRED")
            command = Command(id=command_id, attempt_id=attempt_id, generation=generation, request_hash=fingerprint, request=request, status="PREPARED", created_at=self.clock())
            s.add(command)
            self._event(s, run, "COMMAND_PREPARED", {"command_id": command_id, "request_hash": fingerprint})
            return dto(command)

    def get_command(self, command_id):
        with Session(self.db) as s:
            return dto(self._get(s, Command, command_id))

    def commands_for_attempt(self, attempt_id):
        with Session(self.db) as s:
            return [dto(c) for c in s.scalars(select(Command).where(Command.attempt_id == attempt_id).order_by(Command.created_at))]

    def record_command_result(self, command_id, generation, status, result=None):
        if status not in COMMAND_TERMINAL | {"PREPARED", "ACCEPTED", "RUNNING", "UNKNOWN"}:
            raise DomainError("INVALID_COMMAND_STATUS")
        with self._write() as (s, _):
            command = self._get(s, Command, command_id)
            run, _, _ = self._current(s, command.attempt_id, generation, allow_cancel=True)
            if command.status in COMMAND_TERMINAL:
                if command.status != status or (result is not None and command.result != result):
                    raise DomainError("COMMAND_RESULT_CONFLICT")
                return dto(command)
            command.status, command.result = status, result or {}
            self._event(s, run, "COMMAND_OBSERVED", {"command_id": command_id, "status": status})
            return dto(command)

    def recover_expired(self, run_id, worker_id, execution_confirmed=False, stop_confirmed=False, lease_seconds=60):
        """Take over the SAME attempt for readback; no new attempt or command is created."""
        with self._write() as (s, lock):
            run = self._get(s, Run, run_id)
            if run.cancel_requested or run.status != "RUNNING":
                raise DomainError("RUN_NOT_RECOVERABLE")
            if lock.active_run_id not in {None, run_id}:
                raise DomainError("OTHER_RUN_ACTIVE")
            attempt = s.execute(select(Attempt).join(Step).where(Step.run_id == run_id, Attempt.status == "ACTIVE")).scalar_one_or_none()
            if not attempt or attempt.lease_until > self.clock():
                raise DomainError("LEASE_NOT_EXPIRED")
            step = self._get(s, Step, attempt.step_id)
            step.generation += 1
            attempt.generation, attempt.worker_id = step.generation, worker_id
            attempt.lease_until = self.clock() + lease_seconds
            # The caller may read existing commands under the new generation. Unknown
            # commands still block finish/retry until trusted readback or confirmed stop.
            if stop_confirmed:
                for command in s.scalars(select(Command).where(Command.attempt_id == attempt.id, Command.status.not_in(COMMAND_TERMINAL))):
                    command.status, command.result = "STOPPED", {"stop_confirmed": True, "outcome_before_stop": "UNKNOWN"}
            attempt.execution_confirmed = bool(execution_confirmed or stop_confirmed)
            attempt.reconciliation_required = not bool(stop_confirmed)
            lock.active_run_id = run.id
            self._event(s, run, "ATTEMPT_RECOVERED", {"attempt_id": attempt.id, "generation": attempt.generation, "stop_confirmed": stop_confirmed})
            return self._claim_dto(run, step, attempt)

    def request_cancel(self, run_id):
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.status in TERMINAL:
                return dto(run)
            run.cancel_requested = True
            self._event(s, run, "CANCEL_REQUESTED")
            return dto(run)

    def confirm_cancel(self, run_id, stop_confirmed=False):
        with self._write() as (s, lock):
            run = self._get(s, Run, run_id)
            if not run.cancel_requested or not stop_confirmed:
                raise DomainError("STOP_NOT_CONFIRMED")
            if run.status == "CANCELLED":
                return dto(run)
            for attempt in s.scalars(select(Attempt).join(Step).where(Step.run_id == run_id, Attempt.status == "ACTIVE")):
                attempt.status, attempt.finished_at = "CANCELLED", self.clock()
                step = self._get(s, Step, attempt.step_id)
                step.generation += 1
                step.status = "CANCELLED"
                for c in s.scalars(select(Command).where(Command.attempt_id == attempt.id, Command.status.not_in(COMMAND_TERMINAL))):
                    c.status = "STOPPED"
            run.status, run.reason = "CANCELLED", "CANCELLED"
            self._release(lock, run)
            self._event(s, run, "CANCEL_CONFIRMED")
            return dto(run)

    def record_artifact(self, run_id, kind, object_key, sha256, size, manifest=None, verified=False, *, attempt_id=None, generation=None):
        if len(sha256) != 64 or size < 0 or not verified:
            raise DomainError("ARTIFACT_NOT_VERIFIED")
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if attempt_id is not None:
                current_run, _, _ = self._current(s, attempt_id, generation)
                if current_run.id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            artifact = Artifact(id=identifier(), run_id=run_id, kind=kind, object_key=object_key, sha256=sha256, size=size, manifest=manifest or {}, verified=True, restore_verified=False, created_at=self.clock())
            s.add(artifact)
            if kind == "bundle" and (manifest or {}).get("final_digest"):
                if run.final_digest is not None and run.final_digest != manifest["final_digest"]:
                    raise DomainError("FINAL_VERSION_MISMATCH")
                run.final_digest = manifest["final_digest"]
            self._event(s, run, "ARTIFACT_PUBLISHED", {"artifact_id": artifact.id, "kind": kind, "sha256": sha256})
            return dto(artifact)

    def artifacts(self, run_id):
        with Session(self.db) as s:
            return [dto(x) for x in s.scalars(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at))]

    def confirm_checkpoint(self, run_id, artifact_id, restore_verified=False, *, attempt_id=None, generation=None):
        with self._write() as (s, _):
            run, artifact = self._get(s, Run, run_id), self._get(s, Artifact, artifact_id)
            if attempt_id is not None:
                current_run, _, _ = self._current(s, attempt_id, generation)
                if current_run.id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            if artifact.run_id != run_id or artifact.kind != "checkpoint" or not artifact.verified:
                raise DomainError("INVALID_CHECKPOINT")
            required = {"repositories", "plan_revision", "environment", "files", "services"}
            if not required <= artifact.manifest.keys():
                raise DomainError("INCOMPLETE_CHECKPOINT")
            artifact.restore_verified = bool(restore_verified)
            run.checkpoint_id = artifact_id
            self._event(s, run, "CHECKPOINT_CONFIRMED", {"artifact_id": artifact_id, "restore_verified": restore_verified})
            return dto(artifact)

    def record_verification(self, run_id, kind, passed, evidence, final_digest, *, attempt_id=None, generation=None):
        if not final_digest or not evidence:
            raise DomainError("VERIFICATION_EVIDENCE_REQUIRED")
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if attempt_id is not None:
                current_run, _, _ = self._current(s, attempt_id, generation)
                if current_run.id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            sequence = (s.scalar(select(func.max(VerificationResult.sequence)).where(VerificationResult.run_id == run_id)) or 0) + 1
            v = VerificationResult(id=identifier(), run_id=run_id, kind=kind, passed=passed, evidence=evidence, final_digest=final_digest, sequence=sequence, created_at=self.clock())
            s.add(v)
            if run.status == "READY" and kind in REQUIRED_VERIFICATIONS and final_digest == run.final_digest:
                run.status, run.reason = "AWAITING_HANDOFF", "HANDOFF_RECEIPTS_CHANGED"
            self._event(s, run, "VERIFICATION_RECORDED", {"kind": kind, "passed": passed, "final_digest": final_digest})
            return dto(v)

    def verifications(self, run_id):
        with Session(self.db) as s:
            return [dto(v) for v in s.scalars(select(VerificationResult).where(VerificationResult.run_id == run_id).order_by(VerificationResult.sequence))]

    def complete_handoff(self, run_id, final_digest):
        with self._write() as (s, lock):
            run = self._get(s, Run, run_id)
            if run.cancel_requested or run.status not in {"AWAITING_HANDOFF", "READY"}:
                raise DomainError("HANDOFF_NOT_AVAILABLE")
            if s.scalar(select(func.count()).select_from(Step).where(Step.run_id == run_id, Step.status.in_(["PENDING", "RUNNING", "FAILED"]))):
                raise DomainError("WORK_INCOMPLETE")
            if run.final_digest != final_digest:
                raise DomainError("FINAL_VERSION_MISMATCH")
            latest = {}
            for v in s.scalars(select(VerificationResult).where(VerificationResult.run_id == run_id, VerificationResult.final_digest == final_digest).order_by(VerificationResult.sequence)):
                latest[v.kind] = v
            if not REQUIRED_VERIFICATIONS <= {k for k, verification in latest.items() if verification.passed}:
                raise DomainError("HANDOFF_NOT_VERIFIED")
            expected_receipts = {kind: latest[kind].id for kind in REQUIRED_VERIFICATIONS}
            bundles = list(s.scalars(select(Artifact).where(Artifact.run_id == run_id, Artifact.kind == "bundle", Artifact.verified.is_(True))))
            if not any(a.manifest.get("final_digest") == final_digest for a in bundles):
                raise DomainError("HANDOFF_BUNDLE_MISSING")
            if not any(a.manifest.get("final_digest") == final_digest
                       and a.manifest.get("status") == "VERIFIED"
                       and a.manifest.get("sealed_receipts") == expected_receipts for a in bundles):
                raise DomainError("HANDOFF_BUNDLE_NOT_SEALED")
            run.status, run.final_digest, run.reason = "READY", final_digest, None
            self._release(lock, run)
            self._event(s, run, "HANDOFF_READY", {"final_digest": final_digest})
            return dto(run)

    def record_usage(self, run_id, request_id, provider, usage, attempt_id=None):
        with self._write() as (s, _):
            self._get(s, Run, run_id)
            if attempt_id:
                a = self._get(s, Attempt, attempt_id)
                if self._get(s, Step, a.step_id).run_id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            old = s.get(UsageEvent, request_id)
            if old:
                if old.run_id != run_id or old.provider != provider or old.attempt_id != attempt_id:
                    raise DomainError("USAGE_ID_CONFLICT")
                # Cost observations can arrive after cancel/recovery. Refine the
                # same request monotonically; never erase known quantities or
                # count a late response as another request. Preserve the before
                # image in the append-only event journal.
                unknown_flags = {"usage_unknown", "delivery_unknown"}
                def merge_observation(before, after):
                    merged = dict(before)
                    for key, incoming in after.items():
                        previous = before.get(key)
                        if key in unknown_flags:
                            merged[key] = bool(previous) and bool(incoming) if previous is not None else bool(incoming)
                        elif previous is None:
                            merged[key] = incoming
                        elif incoming is None:
                            continue
                        elif isinstance(previous, dict) and isinstance(incoming, dict):
                            merged[key] = merge_observation(previous, incoming)
                        elif previous != incoming:
                            raise DomainError("USAGE_ID_CONFLICT")
                    return merged
                before = dict(old.usage)
                observed = dict(usage)
                if "usage" in observed:
                    observed["delivery_unknown"] = False
                    metrics = observed["usage"]
                    observed["usage_unknown"] = not isinstance(metrics, dict) or not metrics or any(value is None for value in metrics.values())
                if "usage" in before:
                    before.setdefault("delivery_unknown", False)
                    metrics = before["usage"]
                    before.setdefault("usage_unknown", not isinstance(metrics, dict) or not metrics or any(value is None for value in metrics.values()))
                merged = merge_observation(before, observed)
                if merged != old.usage:
                    prior = old.usage
                    old.usage = merged
                    self._event(s, self._get(s, Run, run_id), "USAGE_REFINED", {"request_id": request_id, "before": prior, "after": merged})
                return dto(old)
            u = UsageEvent(id=request_id, run_id=run_id, attempt_id=attempt_id, provider=provider, usage=usage, created_at=self.clock())
            s.add(u)
            return dto(u)

    def usage(self, run_id):
        with Session(self.db) as s:
            return [dto(u) for u in s.scalars(select(UsageEvent).where(UsageEvent.run_id == run_id).order_by(UsageEvent.created_at))]

    def list_commands(self, run_id=None, attempt_id=None):
        with Session(self.db) as s:
            q = select(Command)
            if run_id:
                q = q.join(Attempt).join(Step).where(Step.run_id == run_id)
            if attempt_id:
                q = q.where(Command.attempt_id == attempt_id)
            if not run_id and not attempt_id:
                raise DomainError("COMMAND_SCOPE_REQUIRED")
            return [dto(c) for c in s.scalars(q.order_by(Command.created_at))]

    def list_recoverable_runs(self):
        with Session(self.db) as s:
            return [dto(r) for r in s.scalars(select(Run).join(Step).join(Attempt).where(Run.status == "RUNNING", Run.cancel_requested.is_(False), Attempt.status == "ACTIVE", Attempt.lease_until <= self.clock()).distinct())]

    def confirm_reconciliation(self, attempt_id, generation, stop_confirmed=False):
        with self._write() as (s, _):
            run, _, attempt = self._current(s, attempt_id, generation)
            unresolved = list(s.scalars(select(Command).where(Command.attempt_id == attempt_id, Command.status.not_in(COMMAND_TERMINAL))))
            if unresolved and not stop_confirmed:
                raise DomainError("EXECUTION_OUTCOME_UNKNOWN")
            if stop_confirmed:
                for c in unresolved:
                    c.status, c.result = "STOPPED", {"stop_confirmed": True, "previous_outcome": "UNKNOWN"}
            attempt.reconciliation_required = False
            attempt.execution_confirmed = True
            self._event(s, run, "EXECUTION_RECONCILED", {"attempt_id": attempt_id, "stop_confirmed": stop_confirmed})
            return dto(attempt)

    def update_step_input(self, run_id, logical_key, payload):
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.cancel_requested or run.status in TERMINAL:
                raise DomainError("RUN_NOT_EXECUTABLE")
            step = s.execute(select(Step).where(Step.run_id == run_id, Step.logical_key == logical_key)).scalar_one_or_none()
            if not step or step.status not in {"WAITING", "PENDING"}:
                raise DomainError("STEP_NOT_WAITING")
            step.payload = {**step.payload, **payload}
            return dto(step)

    def supply_input(self, run_id, step_id, payload):
        """Operator supplies a held-out AI task, never a transformer-visible fixture."""
        with self._write() as (s, _):
            run, step = self._get(s, Run, run_id), self._get(s, Step, step_id)
            if step.run_id != run_id:
                raise DomainError("STEP_RUN_MISMATCH")
            if run.cancel_requested or run.status != "NEEDS_INPUT" or step.status != "WAITING" or step.kind != "ai_handoff":
                raise DomainError("INPUT_NOT_EXPECTED")
            step.payload = {**step.payload, **payload}
            step.status = "PENDING"
            run.status, run.reason = "QUEUED", None
            self._event(s, run, "HELDOUT_INPUT_PROVIDED", {"step_id": step_id})
            return dto(run)

    def define_parameter(self, entity_type, name, value_type):
        """Trusted operator metadata configuration; existing definitions are immutable."""
        if value_type not in {"text", "integer", "number", "boolean", "json", "reference"} or not name or len(name) > 60:
            raise DomainError("INVALID_PARAMETER")
        with self._write() as (s, _):
            self._get(s, EntityType, entity_type)
            key = f"{entity_type}.{name}"
            old = s.get(EntityParameter, key)
            if old:
                if old.value_type != value_type:
                    raise DomainError("PARAMETER_DEFINITION_CONFLICT")
                return dto(old)
            p = EntityParameter(id=key, entity_type=entity_type, name=name, value_type=value_type)
            s.add(p)
            return dto(p)

    def set_entity_value(self, entity_id, name, value, provenance):
        with self._write() as (s, _):
            entity = self._get(s, Entity, entity_id)
            self._set_value(s, entity, name, value, provenance)
            return {"id": entity_id, "parameter": name, "value": value}

    def get_entity(self, entity_id):
        with Session(self.db) as s:
            entity = self._get(s, Entity, entity_id)
            values = {}
            provenance = {}
            for v in s.scalars(select(EntityParameterValue).where(EntityParameterValue.entity_id == entity_id)):
                p = self._get(s, EntityParameter, v.parameter_id)
                values[p.name] = None if v.is_null else getattr(v, "value_" + v.value_type)
                provenance[p.name] = v.provenance
            return {**dto(entity), "values": values, "provenance": provenance}

    def assert_current(self, attempt_id, generation):
        """Read admission guard; dispatch still needs its persisted command identity."""
        with Session(self.db) as s:
            run, step, attempt = self._current(s, attempt_id, generation)
            return self._claim_dto(run, step, attempt)

    def bind_model_profile(self, run_id, profile_manifest, *, attempt_id, generation):
        """Bind one full model configuration before the run's first dispatch.

        Only a secret reference belongs in this manifest. The resolved key never
        enters workflow state, events, project context or runner payloads.
        """
        if not isinstance(profile_manifest, dict) or not isinstance(profile_manifest.get("version"), str) or not profile_manifest["version"]:
            raise DomainError("INVALID_MODEL_PROFILE")
        if any(key in profile_manifest for key in {"api_key", "authorization", "secret", "token"}):
            raise DomainError("INVALID_MODEL_PROFILE")
        normalized = dict(profile_manifest)
        capabilities = normalized.get("capabilities")
        if capabilities is not None:
            if not isinstance(capabilities, (list, tuple, set, frozenset)) or any(not isinstance(value, str) for value in capabilities):
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
                return bound
            run.limits = {**run.limits, "model_profile": normalized}
            self._event(s, run, "MODEL_PROFILE_BOUND", {"profile_hash": profile_hash, "version": normalized["version"]})
            return normalized
