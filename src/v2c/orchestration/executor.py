"""Generic attempt lifecycle. No stage names, concrete providers or model internals."""

from __future__ import annotations

from typing import cast

from pydantic import ValidationError

from ..contracts.control import AttemptControl
from ..contracts.model import AdapterError
from ..contracts.ports import CleanupPort
from ..contracts.runner import RunnerError
from ..contracts.storage import StorageError
from ..contracts.workflow import JsonObject, PendingExecution, RunClaim, RunRecord
from ..delivery import DeliveryError
from ..intake import IntakeError
from ..workflow.errors import DomainError
from .common import InputRequired, RunCancelled, StepFailure
from .journal import OperationJournal
from .registry import StageRegistry


class StageExecutor:
    def __init__(
        self,
        engine: AttemptControl,
        journal: OperationJournal,
        effects: CleanupPort,
        registry: StageRegistry,
    ) -> None:
        self.engine, self.journal, self.effects, self.registry = engine, journal, effects, registry

    async def execute(self, claim: RunClaim) -> RunRecord | PendingExecution:
        """Execute one leased step; waiting never means verified completion."""
        kind = claim["step"]["kind"]
        # A forged in-memory claim cannot change the persisted stage identity.
        if (
            kind not in self.registry.specs
            and self.engine.get_step(claim["step"]["id"])["kind"] != kind
        ):
            raise DomainError("UNKNOWN_STEP_KIND")
        result = None
        failure: Exception | None = None
        try:
            self.journal.guard(claim)
            handler = self.registry.admit(kind)
            result = await handler(claim)
        except (InputRequired, StepFailure, RunCancelled, AdapterError, RunnerError) as error:
            failure = error
        except ValidationError:
            failure = InputRequired("INVALID_STAGE_INPUT")
        except DomainError as error:
            if error.code != "UNKNOWN_STEP_KIND":
                raise
            failure = InputRequired(error.code)
        except (IntakeError, DeliveryError, StorageError) as error:
            failure = InputRequired(str(error))
        finally:
            active = self.effects.allocation(claim["attempt"]["id"])
            if active is not None:
                try:
                    await self.effects.destroy(claim, active)
                except (InputRequired, RunnerError):
                    failure = InputRequired(
                        "VM_STOP_UNCONFIRMED",
                        {"original_reason": str(failure) if failure else None},
                    )
        if (
            isinstance(failure, RunCancelled)
            or self.engine.get_run(claim["run"]["id"])["cancel_requested"]
        ):
            await self.effects.cancel_run(claim["run"]["id"])
            return self.engine.get_run(claim["run"]["id"])
        if isinstance(failure, InputRequired):
            # Unknown remote effects stay ACTIVE until a stop is confirmed; no
            # fresh attempt is licensed merely by a timeout or expired lease.
            unresolved = [
                c
                for c in self.engine.commands_for_attempt(claim["attempt"]["id"])
                if c["status"] not in {"SUCCEEDED", "FAILED", "CANCELLED", "STOPPED"}
            ]
            if unresolved:
                self.engine.note_pending(
                    claim["attempt"]["id"],
                    claim["attempt"]["generation"],
                    failure.reason,
                    failure.details,
                )
                return {
                    "status": "RUNNING",
                    "reason": failure.reason,
                    "details": failure.details,
                    "stop_confirmed": False,
                }
            return self.engine.set_waiting(
                claim["attempt"]["id"],
                claim["attempt"]["generation"],
                "NEEDS_INPUT",
                failure.reason,
                failure.details,
            )
        if failure is not None:
            if isinstance(failure, RunnerError):
                return self.engine.set_waiting(
                    claim["attempt"]["id"],
                    claim["attempt"]["generation"],
                    "NEEDS_INPUT",
                    failure.code,
                    {},
                )
            if isinstance(failure, StepFailure):
                return self.engine.finish_attempt(
                    claim["attempt"]["id"],
                    claim["attempt"]["generation"],
                    failure.evidence,
                    outcome="FAILED",
                    reason=failure.reason,
                    retryable=failure.retryable,
                    execution_confirmed=True,
                )
            raise failure
        if result is None:  # A stage published a deliberate waiting state.
            return self.engine.get_run(claim["run"]["id"])
        self.journal.guard(claim)
        return self.engine.finish_attempt(
            claim["attempt"]["id"], claim["attempt"]["generation"], cast(JsonObject, result)
        )
