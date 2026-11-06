"""Generic attempt lifecycle. No stage names, concrete providers or model internals."""

from __future__ import annotations

from typing import cast

from ..contracts.control import AttemptControl
from ..contracts.model import AdapterError
from ..contracts.ports import CleanupPort
from ..contracts.runner import RunnerError
from ..contracts.storage import StorageError
from ..contracts.workflow import JsonObject, PendingExecution, RunClaim, RunRecord
from ..delivery import DeliveryError
from ..intake import IntakeError
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
        result = None
        failure: Exception | None = None
        try:
            self.journal.guard(claim)
            handler = self.registry.admit(claim["step"]["kind"])
            result = await handler(claim)
        except (InputRequired, StepFailure, RunCancelled, AdapterError, RunnerError) as error:
            failure = error
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
