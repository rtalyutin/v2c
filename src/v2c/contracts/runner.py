"""Runner v1 wire values and replies, independent of transport implementations."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, NotRequired, Self, TypedDict
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID = r"^[A-Za-z0-9_-]{1,128}$"


class RunnerError(Exception):
    def __init__(self, code: str, message: str, *, delivery_unknown: bool = False):
        super().__init__(message)
        self.code, self.delivery_unknown = code, delivery_unknown


class PostgresService(BaseModel):
    """One synthetic PostgreSQL service; images and resources stay operator-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    kind: Literal["postgres"]
    database: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    user: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    env_var: Literal["DATABASE_URL"] = "DATABASE_URL"


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: str = Field(pattern=_ID)
    attempt_id: str = Field(pattern=_ID)
    generation: int = Field(ge=1)
    archive_b64: str
    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_manifest: dict[str, Any]


class AuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: str = Field(pattern=_ID)
    attempt_id: str = Field(pattern=_ID)
    generation: int = Field(ge=1)


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    command_id: str = Field(pattern=_ID)
    workspace_id: str = Field(pattern=_ID)
    attempt_id: str = Field(pattern=_ID)
    generation: int = Field(ge=1)
    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: str = Field(default=".", min_length=1, max_length=4096)
    timeout_seconds: int = Field(gt=0)

    @field_validator("argv")
    @classmethod
    def bounded_argv(cls, argv: list[str]) -> list[str]:
        if any("\0" in arg for arg in argv) or sum(len(arg.encode()) for arg in argv) > 131072:
            raise ValueError("command arguments exceed limits")
        return argv

    def content_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    command_id: str
    request_hash: str
    state: Literal["ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"]
    exit_code: int | None = None
    output: str = ""
    output_truncated: bool = False
    stop_confirmed: bool = False
    reason: str | None = None

    @model_validator(mode="after")
    def confirmed_terminal(self) -> Self:
        if self.state in {"SUCCEEDED", "FAILED", "CANCELLED"} and not self.stop_confirmed:
            raise ValueError("terminal command result requires confirmed termination")
        if self.state == "SUCCEEDED" and self.exit_code != 0:
            raise ValueError("successful command must have exit code zero")
        return self


class VMAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    vm_id: str = Field(pattern=_ID)
    runner_endpoint: str
    state: Literal["CREATING", "READY", "DESTROYING", "DESTROYED", "UNKNOWN"]
    reason: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,128}$")

    @field_validator("runner_endpoint")
    @classmethod
    def safe_endpoint(cls, value: str) -> str:
        p = urlsplit(value)
        if (
            p.scheme != "https"
            or not p.hostname
            or p.username
            or p.password
            or p.query
            or p.fragment
        ):
            raise ValueError("VM provider must return an HTTPS runner endpoint")
        return value.rstrip("/")


class PrepareReply(TypedDict):
    workspace_id: str
    state: Literal["READY"]
    archive_sha256: NotRequired[str]
    generation: NotRequired[int]


class AuthorizeReply(TypedDict):
    workspace_id: str
    generation: int
    state: NotRequired[str]
