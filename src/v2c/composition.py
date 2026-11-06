"""Explicit construction and registration. Core consumers receive prepared ports."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts.control import ControlPlane
from .contracts.model import ModelPort
from .contracts.ports import PipelineSettings, RunnerFactory, RunnerPort, VMProviderPort
from .contracts.stages import StageSpec
from .contracts.storage import ArtifactRef, ArtifactStore, StorageError
from .orchestration.common import PipelineLimits
from .orchestration.components import StageComponents
from .orchestration.execution import ExecutionOperations
from .orchestration.executor import StageExecutor
from .orchestration.handoff import HandoffSealer
from .orchestration.journal import OperationJournal
from .orchestration.model import ModelOperations
from .orchestration.packaging import PackagingOperations
from .orchestration.registry import StageRegistry
from .orchestration.sources import SourceArtifacts
from .orchestration.stages import StageHandlers


class UnavailableArtifactStore:
    """An absent dependency is an explicit failure, including legacy construction."""

    def put_bytes(self, key: str, data: bytes) -> ArtifactRef:
        raise StorageError("ARTIFACT_STORE_REQUIRED")

    def get_bytes(self, key: str) -> bytes:
        raise StorageError("ARTIFACT_STORE_REQUIRED")

    def exists(self, key: str) -> bool:
        raise StorageError("ARTIFACT_STORE_REQUIRED")


def build_components(
    engine: ControlPlane,
    store: ArtifactStore | None,
    model: ModelPort | None = None,
    runner: RunnerPort | None = None,
    settings: PipelineSettings | None = None,
    *,
    vm_provider: VMProviderPort | None = None,
    runner_factory: RunnerFactory | None = None,
) -> StageComponents:
    # Legacy callers can supply partial settings. This adaptation belongs here,
    # never in a stage, source reader, packaging operation or model consumer.
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
    store = store if store is not None else UnavailableArtifactStore()
    journal = OperationJournal(engine, limits)
    models = ModelOperations(engine, journal, model, limits)
    execution = ExecutionOperations(
        engine,
        journal,
        limits,
        settings.execution_limits if settings is not None else None,
        vm_provider,
        runner_factory,
    )
    work_root = settings.work_root if settings is not None else None
    sources = SourceArtifacts(engine, store, journal, limits, work_root)
    return StageComponents(
        engine,
        engine,
        store,
        journal,
        models,
        execution,
        sources,
        HandoffSealer(engine, store),
        PackagingOperations(engine, store, sources, work_root, limits.model_max_input_bytes // 4),
        runner,
    )


def build_executor(
    stages: StageHandlers, components: StageComponents, extra_stages: Iterable[StageSpec] = ()
) -> StageExecutor:
    # Preserve the original admission policy for the seven released stages.
    registry = StageRegistry(
        [
            StageSpec("inspect", stages._stage_inspect, ("model",)),
            StageSpec("baseline", stages._stage_baseline, ("model",)),
            StageSpec("plan", stages._stage_plan, ("model",)),
            StageSpec("transform", stages._stage_transform, ("model",)),
            StageSpec("verify", stages._stage_verify, ("model",)),
            StageSpec("package", stages._stage_package),
            StageSpec("ai_handoff", stages._stage_ai_handoff, ("model",)),
            *extra_stages,
        ],
        {"model": components.models.ensure_ready},
    )
    return StageExecutor(components.control, components.journal, components.execution, registry)
