"""Compatibility entry point and explicit transformation stage coordinator.

Model dispatch, VM execution/recovery, archive operations, checkpoints and
handoff sealing have separate owners in ``orchestration``. This class retains
legacy method names for callers while stage methods describe the workflow.
"""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Literal, cast, overload

from .adapters.model import AdapterError, ModelPort
from .adapters.runner import RunnerError, VMAllocation
from .contracts.ports import PipelineSettings, RunnerFactory, RunnerPort, VMProviderPort
from .contracts.workflow import (
    ArtifactPointer,
    BaselineResult,
    CheckOutcome,
    CheckpointRef,
    CommandOutcome,
    ExecutionLimits,
    HandoffResult,
    InspectResult,
    JsonObject,
    PackageResult,
    PendingExecution,
    RecipePayload,
    RunClaim,
    RunRecord,
    StageResult,
    StepRecord,
    TransformResult,
    VerifyResult,
)
from .delivery import DeliveryError
from .engine import DomainError, Engine
from .intake import IntakeError, Snapshot, inspect_snapshot, safe_relative_path
from .orchestration.archive import (
    _APPLY_FILES,
    archive_context,
    archive_executables,
    assert_source_unchanged,
    checked_archive_files,
    code_zip,
    patched_archive,
    screened_text,
    snapshot_archive,
)
from .orchestration.common import (
    InputRequired,
    PipelineLimits,
    RunCancelled,
    StepFailure,
    json_bytes,
    stable_id,
)
from .orchestration.execution import ExecutionOperations
from .orchestration.handoff import HandoffSealer, public_receipts
from .orchestration.journal import OperationJournal
from .orchestration.model import ModelOperations
from .orchestration.packaging import PackagingOperations
from .orchestration.schemas import (
    DTO,
    EnvironmentRecipe,
    FileChange,
    Finding,
    PatchProposal,
    PlannedWork,
    ProjectPlan,
    RecipeCommand,
)
from .orchestration.sources import SourceArtifacts
from .storage import ArtifactStore, StorageError

# Existing callers import these proposal types and archive helpers from this
# module. Keep that entry point while their implementation has a single owner.
__all__ = [
    "Pipeline",
    "PipelineLimits",
    "InputRequired",
    "StepFailure",
    "RunCancelled",
    "DTO",
    "RecipeCommand",
    "EnvironmentRecipe",
    "Finding",
    "PlannedWork",
    "ProjectPlan",
    "FileChange",
    "PatchProposal",
    "stable_id",
    "json_bytes",
    "code_zip",
    "archive_executables",
    "checked_archive_files",
    "assert_source_unchanged",
    "screened_text",
    "snapshot_archive",
    "archive_context",
    "_APPLY_FILES",
]

StageHandler = Callable[[RunClaim], Awaitable[StageResult | None]]


class Pipeline:
    def __init__(
        self,
        engine: Engine,
        store: ArtifactStore,
        model: ModelPort | None = None,
        runner: RunnerPort | None = None,
        settings: PipelineSettings | None = None,
        *,
        vm_provider: VMProviderPort | None = None,
        runner_factory: RunnerFactory | None = None,
    ):
        self.engine, self.store, self.runner, self.settings = engine, store, runner, settings
        limits = PipelineLimits(
            **{
                name: getattr(settings, name)
                for name in (
                    "command_timeout_seconds",
                    "provision_timeout_seconds",
                    "provision_poll_seconds",
                    "run_timeout_seconds",
                    "model_max_input_bytes",
                )
                if settings is not None and hasattr(settings, name)
            }
        )
        self.journal = OperationJournal(engine, limits)
        self.models = ModelOperations(engine, self.journal, model, limits)
        self.execution = ExecutionOperations(
            engine,
            self.journal,
            limits,
            settings,
            self.models.ensure_ready,
            vm_provider,
            runner_factory,
        )
        self.sources = SourceArtifacts(engine, store, self.journal, limits, settings)
        self.handoff = HandoffSealer(engine, store)
        self.packaging = PackagingOperations(engine, store, self.sources, settings)
        self.stages: Mapping[str, StageHandler] = MappingProxyType(
            {
                "inspect": self._stage_inspect,
                "baseline": self._stage_baseline,
                "plan": self._stage_plan,
                "transform": self._stage_transform,
                "verify": self._stage_verify,
                "package": self._stage_package,
                "ai_handoff": self._stage_ai_handoff,
            }
        )

    @property
    def model(self) -> ModelPort | None:
        return self.models.model

    @model.setter
    def model(self, value: ModelPort | None) -> None:
        self.models.model = value

    @property
    def limits(self) -> PipelineLimits:
        return self.journal.limits

    @limits.setter
    def limits(self, value: PipelineLimits) -> None:
        self.journal.limits = self.models.limits = self.execution.limits = self.sources.limits = (
            value
        )

    @property
    def vm_provider(self) -> VMProviderPort | None:
        return self.execution.vm_provider

    @vm_provider.setter
    def vm_provider(self, value: VMProviderPort | None) -> None:
        self.execution.vm_provider = value

    @property
    def runner_factory(self) -> RunnerFactory | None:
        return self.execution.runner_factory

    @runner_factory.setter
    def runner_factory(self, value: RunnerFactory | None) -> None:
        self.execution.runner_factory = value

    @property
    def _active(self) -> dict[str, tuple[VMAllocation, RunnerPort | None]]:
        return self.execution.active

    def _ensure_model_ready(self) -> None:
        self.models.ensure_ready()

    def _guard(self, claim: RunClaim, *, cleanup: bool = False) -> None:
        self.journal.guard(claim, cleanup=cleanup)

    _fence = staticmethod(OperationJournal.fence)
    _patched_archive = staticmethod(patched_archive)
    _public_receipts = staticmethod(public_receipts)

    def _source(self, claim: RunClaim) -> Snapshot:
        return self.sources.source(claim)

    def _completed(self, run_id: str, kind: str) -> list[StepRecord]:
        return self.sources.completed(run_id, kind)

    @overload
    def _required_result(self, run_id: str, kind: Literal["inspect"]) -> InspectResult: ...

    @overload
    def _required_result(self, run_id: str, kind: Literal["baseline"]) -> BaselineResult: ...

    @overload
    def _required_result(self, run_id: str, kind: Literal["transform"]) -> TransformResult: ...

    @overload
    def _required_result(self, run_id: str, kind: Literal["verify"]) -> VerifyResult: ...

    @overload
    def _required_result(self, run_id: str, kind: Literal["package"]) -> PackageResult: ...

    @overload
    def _required_result(self, run_id: str, kind: Literal["ai_handoff"]) -> HandoffResult: ...

    @overload
    def _required_result(self, run_id: str, kind: str) -> Mapping[str, object]: ...

    def _required_result(self, run_id: str, kind: str) -> Mapping[str, object]:
        return self.sources.required_result(run_id, kind)

    def _artifact_bytes(self, ref: ArtifactPointer) -> bytes:
        return self.sources.artifact_bytes(ref)

    def _last_checkpoint(self, claim: RunClaim) -> CheckpointRef:
        return self.sources.last_checkpoint(claim)

    def _fit_model_context(
        self, request_id: str, task: str, context: JsonObject, schema: JsonObject
    ) -> tuple[JsonObject, JsonObject]:
        return self.models.fit_context(request_id, task, context, schema)

    async def _model(
        self, claim: RunClaim, key: str, task: str, context: JsonObject, schema: type[DTO]
    ) -> JsonObject:
        return await self.models.generate(claim, key, task, context, schema)

    def _execution_profile(self, claim: RunClaim) -> ExecutionLimits:
        return self.execution.profile(claim)

    async def _allocate(self, claim: RunClaim, purpose: str) -> tuple[VMAllocation, RunnerPort]:
        return await self.execution.allocate(claim, purpose)

    async def _prepare(
        self, claim: RunClaim, runner: RunnerPort, archive: bytes, recipe: RecipePayload
    ) -> str:
        return await self.execution.prepare(claim, runner, archive, recipe)

    async def _command(
        self, claim: RunClaim, runner: RunnerPort, workspace: str, command: RecipeCommand, key: str
    ) -> CommandOutcome:
        return await self.execution.command(claim, runner, workspace, command, key)

    async def _commands(
        self,
        claim: RunClaim,
        runner: RunnerPort,
        workspace: str,
        commands: list[RecipeCommand],
        prefix: str,
        *,
        require_success: bool = True,
    ) -> list[CheckOutcome]:
        return await self.execution.commands(
            claim, runner, workspace, commands, prefix, require_success=require_success
        )

    async def _destroy(self, claim: RunClaim, allocation: VMAllocation) -> None:
        await self.execution.destroy(claim, allocation)

    async def _checkpoint(
        self,
        claim: RunClaim,
        runner: RunnerPort,
        workspace: str,
        recipe: RecipePayload,
        *,
        key: str,
        expected_source: bytes,
    ) -> CheckpointRef:
        return await self.sources.checkpoint(
            claim, runner, workspace, recipe, key=key, expected_source=expected_source
        )

    def seal_handoff_bundle(self, run_id: str, final_digest: str) -> JsonObject:
        return self.handoff.seal(run_id, final_digest)

    async def cancel_run(self, run_id: str) -> bool:
        return await self.execution.cancel_run(run_id)

    async def reconcile(self, claim: RunClaim) -> bool:
        return await self.execution.reconcile(claim)

    async def execute(self, claim: RunClaim) -> RunRecord | PendingExecution:
        """Execute one leased step; waiting never means verified completion."""
        handler = self.stages.get(claim["step"]["kind"])
        if handler is None:
            raise DomainError("UNKNOWN_STEP_KIND")
        result = None
        failure: Exception | None = None
        try:
            self._guard(claim)
            if claim["step"]["kind"] != "package":
                self._ensure_model_ready()
            result = await handler(claim)
        except (InputRequired, StepFailure, RunCancelled, AdapterError, RunnerError) as error:
            failure = error
        except (IntakeError, DeliveryError, StorageError) as error:
            failure = InputRequired(str(error))
        finally:
            active = self._active.get(claim["attempt"]["id"])
            if active is not None:
                try:
                    await self._destroy(claim, active[0])
                except (InputRequired, RunnerError):
                    failure = InputRequired(
                        "VM_STOP_UNCONFIRMED",
                        {"original_reason": str(failure) if failure else None},
                    )
        if (
            isinstance(failure, RunCancelled)
            or self.engine.get_run(claim["run"]["id"])["cancel_requested"]
        ):
            await self.cancel_run(claim["run"]["id"])
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
        self._guard(claim)
        return self.engine.finish_attempt(
            claim["attempt"]["id"], claim["attempt"]["generation"], cast(JsonObject, result)
        )

    async def _stage_inspect(self, claim: RunClaim) -> InspectResult:
        snapshot = self._source(claim)
        inspection = inspect_snapshot(snapshot)
        archive = snapshot_archive(snapshot)
        ref = self.store.put_bytes(
            f"runs/{claim['run']['id']}/source/{hashlib.sha256(archive).hexdigest()}.zip", archive
        )
        context = {
            "map": {k: v for k, v in inspection.items() if k not in {"workspace_root", "source"}},
            "source": archive_context(archive, max_bytes=self.limits.model_max_input_bytes // 2),
        }
        recipe = await self._model(
            claim,
            "recipe",
            "Infer a reproducible environment recipe from this unfamiliar project. Preserve its language and architecture. "
            "All commands run in an isolated fixed operator image with Python3, no network and synthetic data only. "
            "An explicitly declared single PostgreSQL service can be provided on a private internal network; "
            "its synthetic connection URL is available only as DATABASE_URL in project commands. "
            "Give exact argv and relative cwd under repositories/<name>. Never put secrets in commands. "
            "Tests and recovery_checks must assert real behavior and required schema/fixture/service state. "
            "For a stateless project state_strategy is stateless; otherwise restore must rebuild from migrations and synthetic fixtures. "
            "Setup must be repeat-safe; restore must deterministically reset and rebuild the synthetic test state from current code migrations and fixtures. "
            "List unavailable integrations in limitations; never replace them with a guessed success.",
            context,
            EnvironmentRecipe,
        )
        self._validate_recipe(recipe)
        self.engine.add_step(claim["run"]["id"], "baseline", "baseline", **self._fence(claim))
        return {
            "inspection": inspection,
            "source_archive": cast(ArtifactPointer, ref.to_dict()),
            "recipe": cast(RecipePayload, recipe),
        }

    def _validate_recipe(self, recipe: JsonObject) -> None:
        parsed = EnvironmentRecipe.model_validate(recipe)
        if parsed.services and parsed.state_strategy != "rebuild_from_fixtures":
            raise InputRequired("TEST_STATE_RESTORE_RECIPE_REQUIRED")
        ids = [c.id for c in parsed.tests]
        if len(ids) != len(set(ids)):
            raise StepFailure("INVALID_RESPONSE", {"reason": "duplicate test ids"})
        if parsed.state_strategy == "rebuild_from_fixtures" and not parsed.restore:
            raise InputRequired("TEST_STATE_RESTORE_RECIPE_REQUIRED")
        for command in parsed.setup + parsed.restore + parsed.tests + parsed.recovery_checks:
            if command.timeout_seconds > self.limits.command_timeout_seconds:
                raise InputRequired("RECIPE_COMMAND_LIMIT_EXCEEDED", {"command_id": command.id})

    async def _stage_baseline(self, claim: RunClaim) -> BaselineResult:
        inspected = self._required_result(claim["run"]["id"], "inspect")
        recipe = EnvironmentRecipe.model_validate(inspected["recipe"])
        _, runner = await self._allocate(claim, "baseline")
        workspace = await self._prepare(
            claim, runner, self._artifact_bytes(inspected["source_archive"]), recipe.to_payload()
        )
        setup = await self._commands(
            claim, runner, workspace, recipe.setup + recipe.restore, "baseline_setup"
        )
        recovery = await self._commands(
            claim, runner, workspace, recipe.recovery_checks, "baseline_recovery"
        )
        checks = await self._commands(
            claim, runner, workspace, recipe.tests, "baseline_test", require_success=False
        )
        checkpoint = await self._checkpoint(
            claim,
            runner,
            workspace,
            recipe.to_payload(),
            key="baseline_checkpoint",
            expected_source=self._artifact_bytes(inspected["source_archive"]),
        )
        self.engine.add_step(claim["run"]["id"], "plan", "plan", **self._fence(claim))
        return {
            "setup": setup,
            "recovery": recovery,
            "checks": checks,
            "checkpoint": checkpoint,
            "known_failures": [r["check_id"] for r in checks if r.get("exit_code") != 0],
        }

    async def _stage_plan(self, claim: RunClaim) -> None:
        inspected = self._required_result(claim["run"]["id"], "inspect")
        baseline = self._required_result(claim["run"]["id"], "baseline")
        plan = await self._model(
            claim,
            "plan",
            "Diagnose maintainability obstacles evidenced in this project, then propose small ordered changes. "
            "Keep current language/framework/data model unless a specific evidenced obstacle requires replacement. "
            "Each step must give exact workspace file paths (including new files), its reason, preserved behavior and check ids from the frozen recipe. "
            "Return an empty steps list when the existing project has no evidenced obstacle requiring changes. "
            "No held-out acceptance tasks are available to you. Architectural preservation is valid; documentation and tests can be the needed improvement.",
            {
                "inspection": {
                    k: v for k, v in inspected["inspection"].items() if k != "workspace_root"
                },
                "baseline": baseline,
                "recipe": inspected["recipe"],
                "source": archive_context(
                    self._artifact_bytes(baseline["checkpoint"]),
                    max_bytes=self.limits.model_max_input_bytes // 2,
                ),
            },
            ProjectPlan,
        )
        valid_checks = {c["id"] for c in inspected["recipe"]["tests"]}
        for work in plan["steps"]:
            for path in work["files"]:
                safe_relative_path(path)
                if not any(path.startswith(r.path + "/") for r in self._source(claim).repositories):
                    raise StepFailure(
                        "INVALID_RESPONSE", {"reason": "work outside known repositories"}
                    )
            if not set(work["checks"]) <= valid_checks:
                raise StepFailure("INVALID_RESPONSE", {"reason": "unknown check ids"})
        published = self.engine.publish_plan(claim["run"]["id"], plan, **self._fence(claim))
        self.engine.set_waiting(
            claim["attempt"]["id"],
            claim["attempt"]["generation"],
            "AWAITING_PLAN_APPROVAL",
            "PLAN_DECISION_REQUIRED",
            {"plan_revision": published["revision"], "snapshot_id": published["snapshot_id"]},
        )
        return None

    def _checked_patch(
        self, proposal: JsonObject, allowed: list[str], archive: bytes
    ) -> PatchProposal:
        patch = PatchProposal.model_validate(proposal)
        if len({f.path for f in patch.files}) != len(patch.files):
            raise StepFailure("PATCH_DUPLICATE_PATH")
        total = 0
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            names = set(source.namelist())
            for file in patch.files:
                if file.path not in allowed:
                    raise InputRequired("PATCH_OUTSIDE_APPROVED_SCOPE", {"path": file.path})
                old = (
                    hashlib.sha256(source.read(file.path)).hexdigest()
                    if file.path in names
                    else None
                )
                if old != file.old_sha256:
                    raise StepFailure("PATCH_BASE_MISMATCH", {"path": file.path})
                if file.content_b64 is not None:
                    try:
                        body = base64.b64decode(file.content_b64, validate=True)
                    except ValueError as error:
                        raise StepFailure("PATCH_ENCODING_INVALID") from error
                    screened_text(file.path, body)
                    total += len(body)
                elif old is None:
                    raise StepFailure("PATCH_DELETE_MISSING_FILE")
        if total > self.limits.max_patch_bytes:
            raise InputRequired("PATCH_SIZE_LIMIT")
        return patch

    async def _apply_patch(
        self, claim: RunClaim, runner: RunnerPort, workspace: str, patch: PatchProposal, key: str
    ) -> CheckOutcome:
        encoded = base64.b64encode(json_bytes([f.model_dump() for f in patch.files])).decode()
        command = RecipeCommand(
            id="apply_files",
            argv=["python3", "-c", _APPLY_FILES, encoded],
            cwd=".",
            timeout_seconds=self.limits.command_timeout_seconds,
        )
        return (await self._commands(claim, runner, workspace, [command], key))[0]

    async def _stage_transform(self, claim: RunClaim) -> TransformResult:
        inspected = self._required_result(claim["run"]["id"], "inspect")
        recipe = EnvironmentRecipe.model_validate(inspected["recipe"])
        work = claim["step"]["payload"].get("plan_step")
        if work is None:
            plan = self.engine.get_plan(claim["run"]["id"])
            work = cast(JsonObject, plan)["plan"]["steps"][0]
        work = PlannedWork.model_validate(work)
        source_checkpoint = self._last_checkpoint(claim)
        archive = self._artifact_bytes(source_checkpoint)
        # The context contains neither held-out task nor expected acceptance.
        proposal = await self._model(
            claim,
            "patch",
            "Implement only the approved work item. Return complete replacement file bytes as base64; "
            "use old_sha256 from source, null for a new file, content_b64 null for deletion. "
            "Do not change tests or files outside the approved exact paths. Preserve the listed behavior. "
            "The controller applies and checks your proposal in a disposable project container.",
            {
                "work": work.model_dump(),
                "source": archive_context(
                    archive, max_bytes=self.limits.model_max_input_bytes // 2, selected=work.files
                ),
                "previous_failure": claim["step"].get("result"),
            },
            PatchProposal,
        )
        patch = self._checked_patch(proposal, work.files, archive)
        _, runner = await self._allocate(claim, "transform")
        workspace = await self._prepare(claim, runner, archive, recipe.to_payload())
        await self._commands(
            claim, runner, workspace, recipe.setup + recipe.restore, "restore_setup"
        )
        restored = await self._commands(
            claim, runner, workspace, recipe.recovery_checks, "restore_verify"
        )
        await self._apply_patch(claim, runner, workspace, patch, "patch")
        await self._commands(
            claim, runner, workspace, recipe.setup + recipe.restore, "post_patch_restore"
        )
        updated_state = await self._commands(
            claim, runner, workspace, recipe.recovery_checks, "post_patch_recovery"
        )
        checks = await self._commands(claim, runner, workspace, recipe.tests, "transform_test")
        checkpoint = await self._checkpoint(
            claim,
            runner,
            workspace,
            recipe.to_payload(),
            key="transform_checkpoint",
            expected_source=self._patched_archive(archive, patch),
        )
        following = [
            s
            for s in self.engine.list_steps(claim["run"]["id"])
            if s["kind"] == "transform" and s["position"] > claim["step"]["position"]
        ]
        if not following:
            self.engine.add_step(claim["run"]["id"], "verify", "verify", **self._fence(claim))
        return {
            "work_id": work.id,
            "explanation": patch.explanation,
            "files": [f.path for f in patch.files],
            "recovery": restored,
            "post_patch_recovery": updated_state,
            "checks": checks,
            "checkpoint": checkpoint,
        }

    async def _stage_verify(self, claim: RunClaim) -> VerifyResult:
        inspected = self._required_result(claim["run"]["id"], "inspect")
        recipe = EnvironmentRecipe.model_validate(inspected["recipe"])
        checkpoint = self._last_checkpoint(claim)
        archive = self._artifact_bytes(checkpoint)
        final_digest = hashlib.sha256(archive).hexdigest()
        allocation, runner = await self._allocate(claim, "final_verification")
        workspace = await self._prepare(claim, runner, archive, recipe.to_payload())
        await self._commands(
            claim, runner, workspace, recipe.setup + recipe.restore, "final_restore"
        )
        recovery = await self._commands(
            claim, runner, workspace, recipe.recovery_checks, "final_recovery"
        )
        checks = await self._commands(claim, runner, workspace, recipe.tests, "final_regression")
        assert_source_unchanged(archive, await runner.checkpoint(workspace))
        evidence = {
            "vm_id": allocation.vm_id,
            "checkpoint_sha256": checkpoint["sha256"],
            "checks": checks,
            "test_double": bool(getattr(runner, "is_test", False)),
        }
        self.engine.record_verification(
            claim["run"]["id"],
            "final_regression",
            True,
            evidence,
            final_digest,
            attempt_id=claim["attempt"]["id"],
            generation=claim["attempt"]["generation"],
        )
        self.engine.record_verification(
            claim["run"]["id"],
            "recovery",
            True,
            {**evidence, "checks": recovery, "strategy": recipe.state_strategy},
            final_digest,
            attempt_id=claim["attempt"]["id"],
            generation=claim["attempt"]["generation"],
        )
        self.engine.confirm_checkpoint(
            claim["run"]["id"],
            checkpoint["artifact_id"],
            restore_verified=True,
            **self._fence(claim),
        )
        self.engine.add_step(claim["run"]["id"], "package", "package", **self._fence(claim))
        return {
            "checkpoint": checkpoint,
            "final_digest": final_digest,
            "checks": checks,
            "recovery": recovery,
            "verification_vm_id": allocation.vm_id,
        }

    async def _stage_package(self, claim: RunClaim) -> PackageResult:
        result = self.packaging.build(claim)
        self.engine.add_step(claim["run"]["id"], "ai_handoff", "ai_handoff", **self._fence(claim))
        return result

    async def _stage_ai_handoff(self, claim: RunClaim) -> HandoffResult:
        verified = self._required_result(claim["run"]["id"], "verify")
        payload = claim["step"]["payload"]
        if not payload.get("task") or not payload.get("checks"):
            raise InputRequired(
                "HELD_OUT_TASK_REQUIRED", {"final_digest": verified["final_digest"]}
            )
        checks = [RecipeCommand.model_validate(c) for c in payload["checks"]]
        archive = self._artifact_bytes(verified["checkpoint"])
        source = self._source(claim)
        allowed = []
        with zipfile.ZipFile(io.BytesIO(archive)) as files:
            allowed = files.namelist()
        proposal = await self._model(
            claim,
            "independent_handoff",
            "You are a new maintainer. Use only the delivered project and its project instructions to perform this held-out change. "
            "Return replacement file bytes as base64 using existing hashes. No prior transformation conversation or hidden checks are available.",
            {
                "task": payload["task"],
                "source": archive_context(
                    archive, max_bytes=self.limits.model_max_input_bytes // 2
                ),
                "instructions": self._delivery_instructions(claim),
            },
            PatchProposal,
        )
        # New file paths can be explicitly allowed by the owner for this task.
        for path in payload.get("allowed_new_files", []):
            safe_relative_path(path)
            if not any(path.startswith(r.path + "/") for r in source.repositories):
                raise InputRequired("HELD_OUT_PATH_INVALID")
            allowed.append(path)
        patch = self._checked_patch(proposal, allowed, archive)
        recipe = EnvironmentRecipe.model_validate(
            self._required_result(claim["run"]["id"], "inspect")["recipe"]
        )
        allocation, runner = await self._allocate(claim, "ai_handoff")
        workspace = await self._prepare(claim, runner, archive, recipe.to_payload())
        await self._commands(
            claim, runner, workspace, recipe.setup + recipe.restore, "handoff_restore"
        )
        await self._commands(claim, runner, workspace, recipe.recovery_checks, "handoff_recovery")
        await self._apply_patch(claim, runner, workspace, patch, "handoff_patch")
        await self._commands(
            claim, runner, workspace, recipe.setup + recipe.restore, "handoff_post_patch_restore"
        )
        await self._commands(
            claim, runner, workspace, recipe.recovery_checks, "handoff_post_patch_recovery"
        )
        regression = await self._commands(
            claim, runner, workspace, recipe.tests, "handoff_regression"
        )
        acceptance = await self._commands(claim, runner, workspace, checks, "hidden_acceptance")
        assert_source_unchanged(
            self._patched_archive(archive, patch), await runner.checkpoint(workspace)
        )
        evidence = {
            "vm_id": allocation.vm_id,
            "checks": acceptance,
            "regression": regression,
            "request_id": stable_id(claim["attempt"]["id"], "model_independent_handoff"),
            "task_digest": hashlib.sha256(payload["task"].encode()).hexdigest(),
            "test_double": bool(
                getattr(runner, "is_test", False) or getattr(self.model, "is_test", False)
            ),
        }
        self.engine.record_verification(
            claim["run"]["id"],
            "ai_handoff",
            True,
            evidence,
            verified["final_digest"],
            attempt_id=claim["attempt"]["id"],
            generation=claim["attempt"]["generation"],
        )
        # The held-out modification is an acceptance exercise, not silently a
        # new version of the product handed to its owner.
        return {
            "final_digest": verified["final_digest"],
            "acceptance": acceptance,
            "human_handoff": "AWAITING_HANDOFF",
        }

    def _delivery_instructions(self, claim: RunClaim) -> dict[str, str]:
        return self.packaging.instructions(claim)
