"""Immutable artifact value and port, independent of local/S3 implementations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol


class StorageError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactRef:
    key: str
    sha256: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArtifactStore(Protocol):
    def put_bytes(self, key: str, data: bytes) -> ArtifactRef: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
