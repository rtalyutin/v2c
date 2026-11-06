"""Provider-independent model values and execution contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ErrorCategory(StrEnum):
    AUTH = "AUTH"
    INVALID_CONFIG = "INVALID_CONFIG"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    RATE_LIMIT = "RATE_LIMIT"
    TRANSPORT_OR_TIMEOUT = "TRANSPORT_OR_TIMEOUT"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CANCELLED = "CANCELLED"


class AdapterError(Exception):
    def __init__(
        self,
        category: ErrorCategory | str,
        message: str,
        *,
        retryable: bool = False,
        delivery_unknown: bool = False,
        usage_unknown: bool = True,
        provider_request_id: str | None = None,
    ):
        super().__init__(message)
        self.category = ErrorCategory(category)
        self.retryable = retryable
        self.delivery_unknown = delivery_unknown
        self.usage_unknown = usage_unknown
        self.provider_request_id = provider_request_id


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    task: str = Field(min_length=1)
    context: str
    response_schema: dict[str, Any] | None = None
    required_capabilities: frozenset[str] = frozenset({"text"})


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost: str | None = None
    currency: str | None = None


class ModelResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    structured: Any = None
    finish_reason: str
    provider_request_id: str | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)


@dataclass(frozen=True)
class ModelDescription:
    """Public, stable metadata; a credential reference is never its resolved value."""

    version: str
    manifest: dict[str, Any]
    max_input_bytes: int
    test_double: bool = False

    def __post_init__(self) -> None:
        if not self.version or self.manifest.get("version") != self.version:
            raise ValueError("model manifest version must match description")
        if type(self.max_input_bytes) is not int or self.max_input_bytes <= 0:
            raise ValueError("model input limit must be positive")


@dataclass(frozen=True)
class ModelReadiness:
    ready: bool
    reason: str = "MODEL_PROFILE_REQUIRED"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedModelRequest:
    """Exact secret-free request bytes. Preparation performs no external I/O."""

    body: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.body)


class ModelPort(Protocol):
    """The complete model consumer contract.

    describe is stable for a run; readiness performs local admission checks.
    prepare is deterministic and secret-free, and measures the complete wire
    payload, including task/schema overhead. generate validates and sends the
    same payload once; the caller owns durable retry/readback policy and usage.
    Borrowed clients remain open. Errors are AdapterError with delivery status.
    """

    def describe(self) -> ModelDescription: ...
    def readiness(self) -> ModelReadiness: ...
    def prepare(self, request: ModelRequest) -> PreparedModelRequest: ...
    async def generate(self, request: ModelRequest) -> ModelResult: ...
