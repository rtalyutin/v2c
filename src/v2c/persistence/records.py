"""Legacy internal row serialization; public responses use separate DTOs.

Do not change this representation while moving responsibilities: persisted
hashes and old consumers rely on canonical JSON and explicit NULL fields.
"""

from __future__ import annotations

from ..contracts.records import (
    ProjectRecord as ProjectRecord,
)
from ..contracts.records import (
    Row as Row,
)
from ..contracts.records import (
    SnapshotRecord as SnapshotRecord,
)
from ..contracts.records import (
    digest as digest,
)
from ..contracts.records import (
    identifier as identifier,
)
from ..models import Base


def dto(row: Base) -> Row:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}
