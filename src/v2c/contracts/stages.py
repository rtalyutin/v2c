"""Declared stage extension points; independent of handlers and infrastructure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .workflow import RunClaim, StageResult

StageHandler = Callable[[RunClaim], Awaitable[StageResult | None]]
AdmissionCheck = Callable[[], None]


@dataclass(frozen=True)
class StageSpec:
    kind: str
    handler: StageHandler
    requirements: tuple[str, ...] = ()
