"""Legacy internal row serialization; public responses use separate DTOs.

Do not change this representation while moving responsibilities: persisted
hashes and old consumers rely on canonical JSON and explicit NULL fields.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, TypedDict

from ..models import Base

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


def dto(row: Base) -> Row:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}
