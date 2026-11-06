"""Compatibility API for a composed pipeline; canonical handlers and executor are separate."""

from __future__ import annotations

from collections.abc import Iterable

from .composition import build_components, build_executor
from .contracts.control import ControlPlane
from .contracts.model import ModelPort
from .contracts.ports import PipelineSettings, RunnerFactory, RunnerPort, VMProviderPort
from .contracts.stages import StageSpec
from .contracts.storage import ArtifactStore
from .contracts.workflow import PendingExecution, RunClaim, RunRecord
from .orchestration.archive import (
    _APPLY_FILES,
    archive_context,
    archive_executables,
    assert_source_unchanged,
    checked_archive_files,
    code_zip,
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
from .orchestration.components import StageComponents
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
from .orchestration.stages import StageHandlers

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


class Pipeline(StageHandlers):
    """Legacy construction delegates to the composition root; production injects components."""

    def __init__(
        self,
        engine: ControlPlane | None = None,
        store: ArtifactStore | None = None,
        model: ModelPort | None = None,
        runner: RunnerPort | None = None,
        settings: PipelineSettings | None = None,
        *,
        vm_provider: VMProviderPort | None = None,
        runner_factory: RunnerFactory | None = None,
        components: StageComponents | None = None,
        extra_stages: Iterable[StageSpec] = (),
    ) -> None:
        if components is None:
            if engine is None:
                raise ValueError("control plane is required")
            components = build_components(
                engine,
                store,
                model,
                runner,
                settings,
                vm_provider=vm_provider,
                runner_factory=runner_factory,
            )
        self.settings = (
            settings  # Legacy construction attribute; operation owners receive narrow values.
        )
        super().__init__(components)
        self.executor = build_executor(self, components, extra_stages)
        self.stages = self.executor.registry.handlers

    async def execute(self, claim: RunClaim) -> RunRecord | PendingExecution:
        return await self.executor.execute(claim)
