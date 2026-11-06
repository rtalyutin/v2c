"""Prepared stage collaborators. Only the composition root constructs this bundle."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.control import AttemptControl, StageControl
from ..contracts.ports import RunnerPort
from ..contracts.storage import ArtifactStore
from .execution import ExecutionOperations
from .handoff import HandoffSealer
from .journal import OperationJournal
from .model import ModelOperations
from .packaging import PackagingOperations
from .sources import SourceArtifacts


@dataclass(frozen=True)
class StageComponents:
    engine: StageControl
    control: AttemptControl
    store: ArtifactStore
    journal: OperationJournal
    models: ModelOperations
    execution: ExecutionOperations
    sources: SourceArtifacts
    handoff: HandoffSealer
    packaging: PackagingOperations
    runner: RunnerPort | None = None
