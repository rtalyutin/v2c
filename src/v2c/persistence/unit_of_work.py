"""One transaction and fencing owner for all control-plane persistence.

Stores receive this object, never an Engine or an arbitrary mutable context.
They may query it inside a transaction, but must not call external services.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.engine import Engine as DatabaseEngine
from sqlalchemy.orm import Session

from ..db import transaction
from ..models import Attempt, Base, Event, Run, RuntimeLock, Step
from ..workflow.errors import DomainError
from ..workflow.states import RUN_TERMINAL

Model = TypeVar("Model", bound=Base)


class ControlDatabase:
    def __init__(self, db: DatabaseEngine, clock: Callable[[], float]) -> None:
        self.db, self.clock = db, clock

    @contextmanager
    def write(self) -> Iterator[tuple[Session, RuntimeLock]]:
        with transaction(self.db) as s:
            lock = s.execute(
                select(RuntimeLock).where(RuntimeLock.id == 1).with_for_update()
            ).scalar_one_or_none()
            if lock is None:
                raise DomainError("SCHEMA_NOT_INITIALIZED")
            yield s, lock

    @staticmethod
    def get(s: Session, model: type[Model], key: Any) -> Model:
        value = s.get(model, key)
        if value is None:
            raise DomainError("NOT_FOUND")
        return value

    def event(self, s: Session, run: Run, kind: str, payload: dict[str, Any] | None = None) -> None:
        run.updated_at = self.clock()
        s.add(Event(run_id=run.id, kind=kind, payload=payload or {}, created_at=self.clock()))

    @staticmethod
    def release(lock: RuntimeLock, run: Run) -> None:
        if lock.active_run_id == run.id:
            lock.active_run_id = None

    def current(
        self,
        s: Session,
        attempt_id: str | None,
        generation: int | None,
        *,
        allow_cancel: bool = False,
        allow_expired: bool = False,
    ) -> tuple[Run, Step, Attempt]:
        attempt = self.get(s, Attempt, attempt_id)
        step = self.get(s, Step, attempt.step_id)
        run = self.get(s, Run, step.run_id)
        if (
            attempt.status != "ACTIVE"
            or attempt.generation != generation
            or step.generation != generation
        ):
            raise DomainError("STALE_GENERATION")
        if not allow_expired and attempt.lease_until <= self.clock():
            raise DomainError("LEASE_EXPIRED")
        if not allow_cancel and (run.cancel_requested or run.status in RUN_TERMINAL):
            raise DomainError("RUN_NOT_EXECUTABLE")
        return run, step, attempt
