"""Fencing and write-before-dispatch identity for control-plane operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from ..contracts.workflow import AttemptFence, CommandRecord, JsonObject, RunClaim
from ..engine import DomainError, Engine, digest
from .common import InputRequired, PipelineLimits, RunCancelled, stable_id


class OperationJournal:
    def __init__(self, engine: Engine, limits: PipelineLimits):
        self.engine, self.limits = engine, limits

    def guard(self, claim: RunClaim, *, cleanup: bool = False) -> None:
        run = self.engine.get_run(claim["run"]["id"])
        if run["cancel_requested"]:
            raise RunCancelled()
        self.engine.assert_current(claim["attempt"]["id"], claim["attempt"]["generation"])
        step = self.engine.get_step(claim["step"]["id"])
        if step["generation"] != claim["attempt"]["generation"] or step["status"] != "RUNNING":
            raise DomainError("STALE_GENERATION")
        if not cleanup and self.engine.clock() - run["created_at"] > run["limits"].get(
            "run_timeout_seconds", self.limits.run_timeout_seconds
        ):
            raise InputRequired("RUN_TIME_LIMIT_REACHED")

    @staticmethod
    def fence(claim: RunClaim) -> AttemptFence:
        return {"attempt_id": claim["attempt"]["id"], "generation": claim["attempt"]["generation"]}

    def prepare(
        self, claim: RunClaim, key: str, request: JsonObject, *, cleanup: bool = False
    ) -> tuple[str, CommandRecord, bool]:
        self.guard(claim, cleanup=cleanup)
        operation_id = stable_id(claim["attempt"]["id"], key)
        try:
            old = self.engine.get_command(operation_id)
        except DomainError as error:
            if error.code != "NOT_FOUND":
                raise
            old = None
        if old is not None:
            # Generation belongs to the original dispatch. Readback is allowed,
            # while any newly dispatched operation uses the current generation.
            expected = {k: v for k, v in request.items() if k != "generation"}
            previous = {k: v for k, v in old["request"].items() if k != "generation"}
            if digest(expected) != digest(previous):
                raise DomainError("COMMAND_ID_CONFLICT")
            return operation_id, cast(CommandRecord, old), True
        row = self.engine.prepare_command(
            claim["attempt"]["id"], claim["attempt"]["generation"], operation_id, request
        )
        return operation_id, cast(CommandRecord, row), False

    def observe(
        self, claim: RunClaim, operation_id: str, status: str, result: Mapping[str, object]
    ) -> JsonObject:
        return self.engine.record_command_result(
            operation_id, claim["attempt"]["generation"], status, dict(result)
        )
