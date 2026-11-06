"""Structured model proposals. Validation never grants execution authority."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .commands import command_arguments, command_directory
from .runner import PostgresService
from .source import safe_relative_path
from .workflow import RecipePayload


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecipeCommand(DTO):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: str = "."
    timeout_seconds: int = Field(gt=0)

    @field_validator("argv")
    @classmethod
    def bounded_arguments(cls, values: list[str]) -> list[str]:
        return command_arguments(values)

    @field_validator("cwd")
    @classmethod
    def relative_directory(cls, value: str) -> str:
        return command_directory(value)


class HandoffInput(DTO):
    """Decode persisted owner input before any source/model/VM effect."""

    task: str = Field(min_length=1, max_length=20000)
    checks: list[RecipeCommand] = Field(min_length=1, max_length=20)
    allowed_new_files: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("allowed_new_files")
    @classmethod
    def relative_files(cls, values: list[str]) -> list[str]:
        for value in values:
            safe_relative_path(value)
        return values


class EnvironmentRecipe(DTO):
    runtime: str = Field(min_length=1, max_length=500)
    setup: list[RecipeCommand]
    restore: list[RecipeCommand]
    tests: list[RecipeCommand] = Field(min_length=1)
    recovery_checks: list[RecipeCommand] = Field(min_length=1)
    state_strategy: Literal["stateless", "rebuild_from_fixtures"]
    services: list[PostgresService] = Field(max_length=1)
    limitations: list[str]

    def to_payload(self) -> RecipePayload:
        """Typed view of the existing validated dump; identical bytes/fields."""
        return cast(RecipePayload, self.model_dump())


class Finding(DTO):
    id: str = Field(min_length=1, max_length=100)
    criterion: str
    reason: str
    evidence_files: list[str]


class PlannedWork(DTO):
    id: str = Field(min_length=1, max_length=100)
    title: str
    files: list[str] = Field(min_length=1, max_length=100)
    reason: str
    preserve: list[str] = Field(min_length=1)
    checks: list[str] = Field(min_length=1)


class ProjectPlan(DTO):
    summary: str
    findings: list[Finding]
    steps: list[PlannedWork] = Field(min_length=0, max_length=30)
    limitations: list[str]


class FileChange(DTO):
    path: str
    old_sha256: str | None
    content_b64: str | None

    @field_validator("path")
    @classmethod
    def checked_path(cls, value: str) -> str:
        return safe_relative_path(value)


class PatchProposal(DTO):
    explanation: str
    files: list[FileChange] = Field(min_length=1, max_length=100)
