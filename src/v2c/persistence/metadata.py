"""Project, snapshot and typed EAV persistence."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Entity,
    EntityParameter,
    EntityParameterValue,
    EntityType,
    Snapshot,
)
from ..workflow.errors import DomainError
from .records import ProjectRecord, Row, SnapshotRecord, digest, dto, identifier
from .unit_of_work import ControlDatabase


class MetadataStore:
    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def create_project(self, owner_id: str, name: str) -> ProjectRecord:
        if not owner_id or not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise DomainError("INVALID_PROJECT")
        with self.database.write() as (s, _):
            entity = Entity(
                id=identifier(),
                owner_id=owner_id,
                entity_type="project",
                created_at=self.database.clock(),
            )
            s.add(entity)
            s.flush()
            s.add(
                EntityParameterValue(
                    entity_id=entity.id,
                    owner_id=owner_id,
                    entity_type="project",
                    parameter_id="project.name",
                    value_type="text",
                    value_text=name,
                    is_null=False,
                    provenance={"source": "operator"},
                )
            )
            return {
                "id": entity.id,
                "owner_id": owner_id,
                "name": name,
                "created_at": entity.created_at,
            }

    def project_row(self, s: Session, entity: Entity) -> ProjectRecord:
        if entity.entity_type != "project":
            raise DomainError("NOT_FOUND")
        value = s.get(EntityParameterValue, (entity.id, "project.name"))
        return {
            "id": entity.id,
            "owner_id": entity.owner_id,
            "name": value.value_text if value else None,
            "created_at": entity.created_at,
        }

    def get_project(self, project_id: str) -> ProjectRecord:
        with Session(self.database.db) as s:
            return self.project_row(s, self.database.get(s, Entity, project_id))

    def list_projects(self, owner_id: str) -> list[ProjectRecord]:
        with Session(self.database.db) as s:
            return [
                self.project_row(s, p)
                for p in s.scalars(
                    select(Entity)
                    .where(Entity.owner_id == owner_id, Entity.entity_type == "project")
                    .order_by(Entity.created_at)
                )
            ]

    def create_entity(
        self, owner_id: str, entity_type: str, values: Row, provenance: Row | None = None
    ) -> Row:
        with self.database.write() as (s, _):
            self.database.get(s, EntityType, entity_type)
            entity = Entity(
                id=identifier(),
                owner_id=owner_id,
                entity_type=entity_type,
                created_at=self.database.clock(),
            )
            s.add(entity)
            s.flush()
            for name, value in values.items():
                self.set_parameter_value(s, entity, name, value, provenance or {})
            return {**dto(entity), "values": values}

    def set_parameter_value(
        self, s: Session, entity: Entity, name: str, value: Any, provenance: Row | None
    ) -> None:
        p = s.execute(
            select(EntityParameter).where(
                EntityParameter.entity_type == entity.entity_type, EntityParameter.name == name
            )
        ).scalar_one_or_none()
        if p is None:
            raise DomainError("UNKNOWN_PARAMETER")
        types: dict[str, type | tuple[type, ...]] = {
            "text": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "json": (dict, list),
            "reference": str,
        }
        if value is not None and (
            not isinstance(value, types[p.value_type])
            or (isinstance(value, bool) and p.value_type in {"integer", "number"})
        ):
            raise DomainError("PARAMETER_TYPE_MISMATCH")
        if value is not None and p.value_type == "reference":
            ref = self.database.get(s, Entity, value)
            if ref.owner_id != entity.owner_id:
                raise DomainError("REFERENCE_OWNER_MISMATCH")
        old = s.get(EntityParameterValue, (entity.id, p.id))
        if old:
            s.delete(old)
            s.flush()
        column = "value_" + p.value_type
        s.add(
            EntityParameterValue(
                entity_id=entity.id,
                owner_id=entity.owner_id,
                entity_type=entity.entity_type,
                parameter_id=p.id,
                value_type=p.value_type,
                is_null=value is None,
                provenance=provenance,
                **({column: value} if value is not None else {}),
            )
        )

    def create_snapshot(
        self, project_id: str, manifest: Row | None, artifact_id: str | None = None
    ) -> SnapshotRecord:
        with self.database.write() as (s, _):
            self.project_row(s, self.database.get(s, Entity, project_id))
            snapshot = Snapshot(
                id=identifier(),
                project_id=project_id,
                digest=digest(manifest),
                manifest=manifest,
                artifact_id=artifact_id,
                created_at=self.database.clock(),
            )
            s.add(snapshot)
            return cast(SnapshotRecord, dto(snapshot))

    def get_snapshot(self, snapshot_id: str) -> SnapshotRecord:
        with Session(self.database.db) as s:
            return cast(SnapshotRecord, dto(self.database.get(s, Snapshot, snapshot_id)))

    def list_snapshots(self, project_id: str) -> list[SnapshotRecord]:
        with Session(self.database.db) as s:
            return [
                cast(SnapshotRecord, dto(x))
                for x in s.scalars(
                    select(Snapshot)
                    .where(Snapshot.project_id == project_id)
                    .order_by(Snapshot.created_at)
                )
            ]

    def define_parameter(self, entity_type: str, name: str, value_type: str) -> Row:
        """Trusted operator metadata configuration; existing definitions are immutable."""
        if (
            value_type not in {"text", "integer", "number", "boolean", "json", "reference"}
            or not name
            or len(name) > 60
        ):
            raise DomainError("INVALID_PARAMETER")
        with self.database.write() as (s, _):
            self.database.get(s, EntityType, entity_type)
            key = f"{entity_type}.{name}"
            old = s.get(EntityParameter, key)
            if old:
                if old.value_type != value_type:
                    raise DomainError("PARAMETER_DEFINITION_CONFLICT")
                return dto(old)
            p = EntityParameter(id=key, entity_type=entity_type, name=name, value_type=value_type)
            s.add(p)
            return dto(p)

    def set_entity_value(
        self, entity_id: str, name: str, value: Any, provenance: Row | None
    ) -> Row:
        with self.database.write() as (s, _):
            entity = self.database.get(s, Entity, entity_id)
            self.set_parameter_value(s, entity, name, value, provenance)
            return {"id": entity_id, "parameter": name, "value": value}

    def get_entity(self, entity_id: str) -> Row:
        with Session(self.database.db) as s:
            entity = self.database.get(s, Entity, entity_id)
            values = {}
            provenance = {}
            for v in s.scalars(
                select(EntityParameterValue).where(EntityParameterValue.entity_id == entity_id)
            ):
                p = self.database.get(s, EntityParameter, v.parameter_id)
                values[p.name] = None if v.is_null else getattr(v, "value_" + v.value_type)
                provenance[p.name] = v.provenance
            return {**dto(entity), "values": values, "provenance": provenance}
