"""Durable, fenced workflow. External I/O must happen outside these transactions."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, TypeVar, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..contracts.workflow import AttemptRecord, RunClaim, RunRecord, StepRecord
from ..models import (
    Attempt,
    Base,
    Run,
    RuntimeLock,
    Step,
)

# Backwards-compatible exports; canonical policies live in workflow.states.
from ..workflow.errors import DomainError
from .records import Row, dto, identifier
from .unit_of_work import ControlDatabase

Model = TypeVar("Model", bound=Base)


class WorkflowRows:
    """Shared row helpers; each public operation owns one ControlDatabase transaction."""

    def __init__(self, database: ControlDatabase) -> None:
        self.database = database
        self.db = database.db
        self.clock = database.clock

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

    def _claim_dto(self, run: Run, step: Step, attempt: Attempt) -> RunClaim:
        return {
            "run": cast(RunRecord, dto(run)),
            "step": cast(StepRecord, dto(step)),
            "attempt": cast(AttemptRecord, dto(attempt)),
        }

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
