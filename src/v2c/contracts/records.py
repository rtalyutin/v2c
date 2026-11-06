"""Neutral record identities and canonical JSON. Preserve persisted byte semantics."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, TypedDict

Row = dict[str, Any]


class ProjectRecord(TypedDict):
    id: str
    owner_id: str
    name: str | None
    created_at: float


class SnapshotRecord(TypedDict):
    id: str
    project_id: str
    digest: str
    manifest: Row
    artifact_id: str | None
    created_at: float


def identifier() -> str:
    return str(uuid.uuid4())


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
