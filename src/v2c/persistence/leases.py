"""Durable, fenced workflow. External I/O must happen outside these transactions."""

from __future__ import annotations

from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..contracts.workflow import RunClaim, RunRecord
from ..models import (
    Approval,
    Attempt,
    Command,
    Run,
    Step,
)

# Backwards-compatible exports; canonical policies live in workflow.states.
from ..workflow.errors import DomainError
from ..workflow.states import (
    COMMAND_TERMINAL,
    MAX_ATTEMPTS,
    completion,
)
from ..workflow.states import RUN_TERMINAL as TERMINAL
from ..workflow.states import RUN_WAITING as WAITING
from .records import Row, dto, identifier
from .workflow_rows import WorkflowRows


class LeaseStore(WorkflowRows):
    def note_pending(self, attempt_id: str, generation: int, reason: str, details: Row) -> None:
        with self._write() as (s, _):
            run, _, _ = self._current(s, attempt_id, generation)
            run.reason = reason
            self._event(s, run, "EXECUTION_PENDING", {"reason": reason, "details": details})

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

    def assert_current(self, attempt_id: str | None, generation: int | None) -> RunClaim:
        """Read admission guard; dispatch still needs its persisted command identity."""
        with Session(self.db) as s:
            run, step, attempt = self._current(s, attempt_id, generation)
            return self._claim_dto(run, step, attempt)
