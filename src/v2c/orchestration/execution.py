"""Disposable VM and runner effects, readback, cleanup and recovery."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from collections.abc import Callable
from typing import cast

from ..adapters.runner import CommandRequest, PrepareRequest, RunnerError, VMAllocation
from ..contracts.ports import PipelineSettings, RunnerFactory, RunnerPort, VMProviderPort
from ..contracts.workflow import (
    CheckOutcome,
    CommandOutcome,
    ExecutionLimits,
    RecipePayload,
    RunClaim,
)
from ..diagnostics import annotate_exception
from ..engine import Engine
from .common import InputRequired, PipelineLimits, StepFailure, stable_id
from .journal import OperationJournal
from .schemas import RecipeCommand


class ExecutionOperations:
    def __init__(
        self,
        engine: Engine,
        journal: OperationJournal,
        limits: PipelineLimits,
        settings: PipelineSettings | None,
        ensure_model_ready: Callable[[], None],
        provider: VMProviderPort | None,
        runner_factory: RunnerFactory | None,
    ):
        self.engine, self.journal, self.limits = engine, journal, limits
        self.settings, self.ensure_model_ready = settings, ensure_model_ready
        self.vm_provider, self.runner_factory = provider, runner_factory
        self.active: dict[str, tuple[VMAllocation, RunnerPort | None]] = {}

    def profile(self, claim: RunClaim) -> ExecutionLimits:
        profile = claim["run"]["limits"].get("execution_limits") or getattr(
            self.settings, "execution_limits", None
        )
        required = {"cpus", "memory_mb", "pids", "disk_mb", "max_output_bytes", "network", "image"}
        if (
            not isinstance(profile, dict)
            or not required <= profile.keys()
            or not profile.get("image")
        ):
            raise InputRequired("EXECUTION_PROFILE_REQUIRED")
        if any(
            not isinstance(profile[k], (int, float))
            or isinstance(profile[k], bool)
            or profile[k] <= 0
            for k in ("cpus", "memory_mb", "pids", "disk_mb", "max_output_bytes")
        ):
            raise InputRequired("EXECUTION_PROFILE_INVALID")
        if profile["network"] != "none":
            raise InputRequired("EXECUTION_NETWORK_POLICY_UNSUPPORTED")
        if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", profile["image"]):
            raise InputRequired("EXECUTION_IMAGE_DIGEST_REQUIRED")
        if self.vm_provider is None or self.runner_factory is None:
            raise InputRequired("VM_PROVIDER_REQUIRED")
        return cast(ExecutionLimits, profile)

    async def allocate(self, claim: RunClaim, purpose: str) -> tuple[VMAllocation, RunnerPort]:
        try:
            return await self._allocate(claim, purpose)
        except Exception as error:
            annotate_exception(error, allocation_id=stable_id(claim["attempt"]["id"], purpose))
            raise

    async def _allocate(self, claim: RunClaim, purpose: str) -> tuple[VMAllocation, RunnerPort]:
        self.ensure_model_ready()
        profile = self.profile(claim)
        assert self.vm_provider is not None and self.runner_factory is not None
        allocation_id = stable_id(claim["attempt"]["id"], purpose)
        op_id, row, existed = self.journal.prepare(
            claim,
            "vm_" + purpose,
            {
                "operation": "allocate_vm",
                "allocation_id": allocation_id,
                "purpose": purpose,
                "execution_limits": profile,
            },
        )
        if row["status"] == "SUCCEEDED":
            from ..adapters.runner import VMAllocation

            allocation = VMAllocation.model_validate(row["result"])
        else:
            allocation = await self.vm_provider.create(allocation_id, purpose)
            # A returned CREATING allocation already owns a possible billable
            # resource. Keep its identity durable and available to cleanup before
            # readiness, including a cold-bootstrap timeout.
            self.active[claim["attempt"]["id"]] = (allocation, None)
            self.journal.observe(claim, op_id, "ACCEPTED", allocation.model_dump())
            self.journal.guard(claim)
            run = self.engine.get_run(claim["run"]["id"])
            remaining_run = max(
                0,
                run["limits"].get("run_timeout_seconds", self.limits.run_timeout_seconds)
                - (self.engine.clock() - run["created_at"]),
            )
            deadline = time.monotonic() + min(self.limits.provision_timeout_seconds, remaining_run)
            while allocation.state == "CREATING" and time.monotonic() < deadline:
                self.journal.guard(claim)
                await asyncio.sleep(
                    min(self.limits.provision_poll_seconds, max(0, deadline - time.monotonic()))
                )
                self.journal.guard(claim)
                allocation = await self.vm_provider.status(allocation.vm_id)
                self.active[claim["attempt"]["id"]] = (allocation, None)
                self.journal.observe(claim, op_id, "ACCEPTED", allocation.model_dump())
            if allocation.state != "READY":
                raise InputRequired(
                    allocation.reason or "VM_ALLOCATION_NOT_READY",
                    {
                        "allocation_id": allocation.vm_id,
                        "state": allocation.state,
                        "gateway_reason": allocation.reason,
                    },
                )
            self.journal.observe(claim, op_id, "SUCCEEDED", allocation.model_dump())
            self.active[claim["attempt"]["id"]] = (allocation, None)
            # Every phase/attempt has a distinct allocation. In particular the
            # final verifier cannot be routed back to the transformation VM.
            for prior in self.engine.list_commands(run_id=claim["run"]["id"]):
                if (
                    prior["id"] != op_id
                    and prior["request"].get("operation") == "allocate_vm"
                    and (prior.get("result") or {}).get("vm_id") == allocation.vm_id
                ):
                    raise InputRequired("FRESH_VM_REQUIRED")
        runner = self.runner_factory(allocation)
        self.active[claim["attempt"]["id"]] = (allocation, runner)
        return allocation, runner

    async def prepare(
        self, claim: RunClaim, runner: RunnerPort, archive: bytes, recipe: RecipePayload
    ) -> str:
        try:
            return await self._prepare(claim, runner, archive, recipe)
        except Exception as error:
            annotate_exception(error, command_id=stable_id(claim["attempt"]["id"], "prepare"))
            raise

    async def _prepare(
        self, claim: RunClaim, runner: RunnerPort, archive: bytes, recipe: RecipePayload
    ) -> str:
        workspace = stable_id(claim["attempt"]["id"], "workspace")
        request = PrepareRequest(
            workspace_id=workspace,
            attempt_id=claim["attempt"]["id"],
            generation=claim["attempt"]["generation"],
            archive_b64=base64.b64encode(archive).decode(),
            archive_sha256=hashlib.sha256(archive).hexdigest(),
            input_manifest={"execution_limits": self.profile(claim), "environment_recipe": recipe},
        )
        op_id, row, existed = self.journal.prepare(
            claim,
            "prepare",
            {**request.model_dump(exclude={"archive_b64"}), "operation": "prepare"},
        )
        if row["status"] != "SUCCEEDED":
            # The runner's workspace creation is idempotent by the exact input.
            # Re-delivery retains the original generation and archive identity.
            if existed:
                request = request.model_copy(update={"generation": row["request"]["generation"]})
            result = await runner.prepare(request)
            self.journal.observe(claim, op_id, "SUCCEEDED", result)
        self.journal.guard(claim)
        await runner.authorize(workspace, claim["attempt"]["id"], claim["attempt"]["generation"])
        return workspace

    async def command(
        self, claim: RunClaim, runner: RunnerPort, workspace: str, command: RecipeCommand, key: str
    ) -> CommandOutcome:
        try:
            return await self._command(claim, runner, workspace, command, key)
        except Exception as error:
            annotate_exception(error, command_id=stable_id(claim["attempt"]["id"], key))
            raise

    async def _command(
        self, claim: RunClaim, runner: RunnerPort, workspace: str, command: RecipeCommand, key: str
    ) -> CommandOutcome:
        request = CommandRequest(
            command_id=stable_id(claim["attempt"]["id"], key),
            workspace_id=workspace,
            attempt_id=claim["attempt"]["id"],
            generation=claim["attempt"]["generation"],
            argv=command.argv,
            cwd=command.cwd,
            timeout_seconds=min(command.timeout_seconds, self.limits.command_timeout_seconds),
        )
        op_id, row, existed = self.journal.prepare(claim, key, request.model_dump())
        if row["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return cast(CommandOutcome, row["result"])
        if row["status"] == "STOPPED":
            raise StepFailure("PREVIOUS_EXECUTION_STOPPED", retryable=True)
        if existed:
            try:
                result = await runner.status(op_id)
            except RunnerError as error:
                raise InputRequired("COMMAND_OUTCOME_UNKNOWN", {"command_id": op_id}) from error
        else:
            try:
                result = await runner.execute(request)
            except RunnerError as error:
                # Same durable command ID, never a second execution request.
                if not error.delivery_unknown:
                    self.journal.observe(
                        claim,
                        op_id,
                        "FAILED",
                        {"reason": error.code, "exit_code": None, "state": "FAILED"},
                    )
                    raise InputRequired(error.code) from error
                try:
                    result = await runner.status(op_id)
                except RunnerError as lookup_error:
                    raise InputRequired(
                        "COMMAND_OUTCOME_UNKNOWN", {"command_id": op_id}
                    ) from lookup_error
        deadline = time.monotonic() + request.timeout_seconds + 10
        while result.state in {"ACCEPTED", "RUNNING"}:
            self.journal.guard(claim)
            self.journal.observe(claim, op_id, result.state, result.model_dump())
            if time.monotonic() > deadline:
                try:
                    result = await runner.cancel(op_id)
                except RunnerError as error:
                    raise InputRequired(
                        "COMMAND_STOP_UNCONFIRMED", {"command_id": op_id}
                    ) from error
                if not result.stop_confirmed:
                    raise InputRequired("COMMAND_STOP_UNCONFIRMED", {"command_id": op_id})
                break
            await asyncio.sleep(self.limits.poll_seconds)
            result = await runner.status(op_id)
        if result.request_hash != CommandRequest.model_validate(row["request"]).content_hash():
            raise InputRequired("COMMAND_RESULT_IDENTITY_MISMATCH", {"command_id": op_id})
        if result.state == "UNKNOWN" or (result.state == "CANCELLED" and not result.stop_confirmed):
            raise InputRequired("COMMAND_OUTCOME_UNKNOWN", {"command_id": op_id})
        self.journal.observe(claim, op_id, result.state, result.model_dump())
        return cast(CommandOutcome, result.model_dump())

    async def commands(
        self,
        claim: RunClaim,
        runner: RunnerPort,
        workspace: str,
        commands: list[RecipeCommand],
        prefix: str,
        *,
        require_success: bool = True,
    ) -> list[CheckOutcome]:
        results: list[CheckOutcome] = []
        for number, command in enumerate(commands):
            result = await self.command(
                claim, runner, workspace, command, f"{prefix}_{number}_{command.id}"
            )
            results.append({"check_id": command.id, **result})
            if require_success and (result["state"] != "SUCCEEDED" or result["exit_code"] != 0):
                raise StepFailure("CHECK_FAILED", {"checks": results})
        return results

    async def destroy(self, claim: RunClaim, allocation: VMAllocation) -> None:
        try:
            await self._destroy(claim, allocation)
        except Exception as error:
            annotate_exception(error, allocation_id=allocation.vm_id)
            raise

    async def _destroy(self, claim: RunClaim, allocation: VMAllocation) -> None:
        # VM destruction is the authority for termination, including a hostile
        # guest or a runner whose command journal has been lost.
        operation_id = stable_id(claim["attempt"]["id"], "destroy_" + allocation.vm_id)
        run = self.engine.get_run(claim["run"]["id"])
        if not run["cancel_requested"]:
            op_id, row, _ = self.journal.prepare(
                claim,
                "destroy_" + allocation.vm_id,
                {"operation": "destroy_vm", "vm_id": allocation.vm_id},
                cleanup=True,
            )
            if row["status"] == "SUCCEEDED":
                self.active.pop(claim["attempt"]["id"], None)
                return
        else:
            op_id = operation_id
        assert self.vm_provider is not None
        result = await self.vm_provider.destroy(allocation.vm_id)
        deadline = time.monotonic() + self.limits.provision_timeout_seconds
        while result.state != "DESTROYED" and time.monotonic() < deadline:
            await asyncio.sleep(self.limits.provision_poll_seconds)
            result = await self.vm_provider.status(allocation.vm_id)
        if result.state != "DESTROYED":
            raise InputRequired("VM_STOP_UNCONFIRMED", {"vm_id": allocation.vm_id})
        if not run["cancel_requested"]:
            self.journal.observe(claim, op_id, "SUCCEEDED", result.model_dump())
            for command in self.engine.commands_for_attempt(claim["attempt"]["id"]):
                if command["status"] not in {"SUCCEEDED", "FAILED", "CANCELLED", "STOPPED"}:
                    self.journal.observe(
                        claim,
                        command["id"],
                        "STOPPED",
                        {"stop_confirmed": True, "outcome_before_stop": "UNKNOWN"},
                    )
        self.active.pop(claim["attempt"]["id"], None)

    async def cancel_run(self, run_id: str) -> bool:
        """A cancel request becomes CANCELLED only after provider stop readback."""
        run = self.engine.get_run(run_id)
        if not run["cancel_requested"]:
            return False
        allocations: dict[str, str] = {}
        try:
            rows = self.engine.list_commands(run_id=run_id)
            known_usage = {row["id"] for row in self.engine.usage(run_id)}
            for row in rows:
                if (
                    row["request"].get("operation") == "model"
                    and row["request"]["request_id"] not in known_usage
                ):
                    self.engine.record_usage(
                        run_id,
                        row["request"]["request_id"],
                        row["request"]["profile_version"],
                        {
                            "usage_unknown": True,
                            "delivery_unknown": True,
                            "reason": "CANCELLED_DURING_MODEL_REQUEST",
                        },
                        row["attempt_id"],
                    )
                if row["request"].get("operation") != "allocate_vm":
                    continue
                result = row.get("result") or {}
                if result.get("vm_id"):
                    allocations[result["vm_id"]] = result.get("runner_endpoint", "")
                elif self.vm_provider is not None:
                    # Lost create response: the same idempotency identity returns
                    # the accepted allocation, not a fresh arbitrary VM.
                    value = await self.vm_provider.create(
                        row["request"]["allocation_id"], row["request"]["purpose"]
                    )
                    allocations[value.vm_id] = value.runner_endpoint
            if allocations and self.vm_provider is None:
                return False
            for vm_id in allocations:
                assert self.vm_provider is not None
                value = await self.vm_provider.destroy(vm_id)
                deadline = time.monotonic() + self.limits.provision_timeout_seconds
                while value.state != "DESTROYED" and time.monotonic() < deadline:
                    await asyncio.sleep(self.limits.provision_poll_seconds)
                    value = await self.vm_provider.status(vm_id)
                if value.state != "DESTROYED":
                    return False
        except RunnerError:
            return False
        self.engine.confirm_cancel(run_id, stop_confirmed=True)
        return True

    async def reconcile(self, claim: RunClaim) -> bool:
        """Fence the old generation, then read effects before allowing new work."""
        rows = self.engine.commands_for_attempt(claim["attempt"]["id"])
        allocations = [r for r in rows if r["request"].get("operation") == "allocate_vm"]
        unknown_models = [
            r
            for r in rows
            if r["request"].get("operation") == "model"
            and r["status"] not in {"SUCCEEDED", "FAILED", "CANCELLED", "STOPPED"}
        ]
        if unknown_models:
            # A dead worker cannot cancel or read back a generic model API.
            # Preserve unknown delivery/cost, and require an explicit decision;
            # replaying the request would silently make another paid call.
            existing_usage = {u["id"] for u in self.engine.usage(claim["run"]["id"])}
            for operation in unknown_models:
                request_id = operation["request"]["request_id"]
                if request_id not in existing_usage:
                    self.engine.record_usage(
                        claim["run"]["id"],
                        request_id,
                        operation["request"]["profile_version"],
                        {
                            "usage_unknown": True,
                            "delivery_unknown": True,
                            "reason": "WORKER_LOST_MODEL_RESPONSE",
                        },
                        claim["attempt"]["id"],
                    )
                self.journal.observe(
                    claim,
                    operation["id"],
                    "STOPPED",
                    {
                        "provider_outcome": "UNKNOWN",
                        "usage_unknown": request_id not in existing_usage,
                        "local_dispatch_stopped": True,
                    },
                )
            # The pipeline performs model proposals before allocating a VM in
            # each such stage. Fail closed if a future stage changes that rule.
            if allocations:
                return False
            self.engine.confirm_reconciliation(
                claim["attempt"]["id"], claim["attempt"]["generation"]
            )
            self.engine.set_waiting(
                claim["attempt"]["id"],
                claim["attempt"]["generation"],
                "NEEDS_INPUT",
                "MODEL_OUTCOME_UNKNOWN",
                {"request_ids": [r["request"]["request_id"] for r in unknown_models]},
            )
            return False
        if not allocations:
            self.engine.confirm_reconciliation(
                claim["attempt"]["id"], claim["attempt"]["generation"]
            )
            return True
        from ..adapters.runner import VMAllocation

        assert self.vm_provider is not None and self.runner_factory is not None
        for row in allocations:
            if row["status"] == "SUCCEEDED":
                allocation = VMAllocation.model_validate(row["result"])
            else:
                allocation = await self.vm_provider.create(
                    row["request"]["allocation_id"], row["request"]["purpose"]
                )
                self.journal.observe(claim, row["id"], "SUCCEEDED", allocation.model_dump())
            runner = self.runner_factory(allocation)
            workspace = stable_id(claim["attempt"]["id"], "workspace")
            try:
                await runner.authorize(
                    workspace, claim["attempt"]["id"], claim["attempt"]["generation"]
                )
                if any(
                    r["request"].get("operation") in {"prepare", "checkpoint"}
                    and r["status"] not in {"SUCCEEDED", "FAILED", "CANCELLED", "STOPPED"}
                    for r in rows
                ):
                    # Lost metadata/file-transfer response has no independent
                    # readback contract. Stop its VM, then restore a confirmed
                    # checkpoint rather than guessing whether files are complete.
                    raise RunnerError("UNKNOWN", "file operation requires restoration")
                for command in rows:
                    if not command["request"].get("argv") or command["status"] in {
                        "SUCCEEDED",
                        "FAILED",
                        "CANCELLED",
                        "STOPPED",
                    }:
                        continue
                    value = await runner.status(command["id"])
                    deadline = time.monotonic() + self.limits.command_timeout_seconds
                    while value.state in {"ACCEPTED", "RUNNING"} and time.monotonic() < deadline:
                        await asyncio.sleep(self.limits.poll_seconds)
                        value = await runner.status(command["id"])
                    if value.state in {"UNKNOWN", "ACCEPTED", "RUNNING"}:
                        raise RunnerError("UNKNOWN", "previous command did not terminate")
                    if (
                        value.request_hash
                        != CommandRequest.model_validate(command["request"]).content_hash()
                    ):
                        raise RunnerError("INVALID_RESPONSE", "previous command identity mismatch")
                    self.journal.observe(claim, command["id"], value.state, value.model_dump())
            except RunnerError:
                stopped = await self.vm_provider.destroy(allocation.vm_id)
                deadline = time.monotonic() + self.limits.provision_timeout_seconds
                while stopped.state != "DESTROYED" and time.monotonic() < deadline:
                    await asyncio.sleep(self.limits.provision_poll_seconds)
                    stopped = await self.vm_provider.status(allocation.vm_id)
                if stopped.state != "DESTROYED":
                    return False
                self.engine.confirm_reconciliation(
                    claim["attempt"]["id"], claim["attempt"]["generation"], stop_confirmed=True
                )
                self.engine.finish_attempt(
                    claim["attempt"]["id"],
                    claim["attempt"]["generation"],
                    {"restore_from_checkpoint_required": True},
                    outcome="FAILED",
                    reason="PREVIOUS_EXECUTION_UNKNOWN",
                    retryable=True,
                    execution_confirmed=True,
                )
                return False
        self.engine.confirm_reconciliation(claim["attempt"]["id"], claim["attempt"]["generation"])
        return True
