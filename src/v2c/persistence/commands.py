"""Durable command intent and observed outcome persistence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Attempt,
    Command,
    Step,
)
from ..workflow.errors import DomainError
from ..workflow.states import COMMAND_STATES, COMMAND_TERMINAL
from .records import Row, digest, dto
from .unit_of_work import ControlDatabase


class CommandStore:
    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def prepare_command(
        self, attempt_id: str | None, generation: int | None, command_id: str, request: Row
    ) -> Row:
        with self.database.write() as (s, _):
            run, _, attempt = self.database.current(s, attempt_id, generation)
            fingerprint = digest(request)
            old = s.get(Command, command_id)
            if old:
                if old.attempt_id != attempt_id or old.request_hash != fingerprint:
                    raise DomainError("COMMAND_ID_CONFLICT")
                return dto(old)
            if attempt.reconciliation_required:
                raise DomainError("RECONCILIATION_REQUIRED")
            command = Command(
                id=command_id,
                attempt_id=attempt_id,
                generation=generation,
                request_hash=fingerprint,
                request=request,
                status="PREPARED",
                created_at=self.database.clock(),
            )
            s.add(command)
            self.database.event(
                s, run, "COMMAND_PREPARED", {"command_id": command_id, "request_hash": fingerprint}
            )
            return dto(command)

    def get_command(self, command_id: str) -> Row:
        with Session(self.database.db) as s:
            return dto(self.database.get(s, Command, command_id))

    def commands_for_attempt(self, attempt_id: str | None) -> list[Row]:
        with Session(self.database.db) as s:
            return [
                dto(c)
                for c in s.scalars(
                    select(Command)
                    .where(Command.attempt_id == attempt_id)
                    .order_by(Command.created_at)
                )
            ]

    def record_command_result(
        self, command_id: str, generation: int | None, status: str, result: Row | None = None
    ) -> Row:
        if status not in COMMAND_STATES:
            raise DomainError("INVALID_COMMAND_STATUS")
        with self.database.write() as (s, _):
            command = self.database.get(s, Command, command_id)
            run, _, _ = self.database.current(s, command.attempt_id, generation, allow_cancel=True)
            if command.status in COMMAND_TERMINAL:
                if command.status != status or (result is not None and command.result != result):
                    raise DomainError("COMMAND_RESULT_CONFLICT")
                return dto(command)
            command.status, command.result = status, result or {}
            self.database.event(
                s, run, "COMMAND_OBSERVED", {"command_id": command_id, "status": status}
            )
            return dto(command)

    def list_commands(self, run_id: str | None = None, attempt_id: str | None = None) -> list[Row]:
        with Session(self.database.db) as s:
            q = select(Command)
            if run_id:
                q = q.join(Attempt).join(Step).where(Step.run_id == run_id)
            if attempt_id:
                q = q.where(Command.attempt_id == attempt_id)
            if not run_id and not attempt_id:
                raise DomainError("COMMAND_SCOPE_REQUIRED")
            return [dto(c) for c in s.scalars(q.order_by(Command.created_at))]
