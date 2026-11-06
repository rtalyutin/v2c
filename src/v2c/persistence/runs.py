"""Durable, fenced workflow. External I/O must happen outside these transactions."""

from __future__ import annotations

import json
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts.workflow import RunRecord
from ..models import (
    Event,
    Run,
    Snapshot,
    Step,
)

# Backwards-compatible exports; canonical policies live in workflow.states.
from ..workflow.errors import DomainError
from ..workflow.states import RUN_TERMINAL as TERMINAL
from .records import Row, digest, dto, identifier
from .workflow_rows import WorkflowRows


class RunStore(WorkflowRows):
    def create_run(
        self,
        project_id: str,
        snapshot_id: str,
        idempotency_key: str,
        limits: Row | None = None,
        parent_run_id: str | None = None,
    ) -> RunRecord:
        if not idempotency_key or len(idempotency_key) > 200:
            raise DomainError("INVALID_IDEMPOTENCY_KEY")
        limits = limits or {}
        fingerprint = digest(
            {"snapshot_id": snapshot_id, "limits": limits, "parent_run_id": parent_run_id}
        )
        with self._write() as (s, _):
            snapshot = self._get(s, Snapshot, snapshot_id)
            if snapshot.project_id != project_id:
                raise DomainError("SNAPSHOT_PROJECT_MISMATCH")
            old = s.execute(
                select(Run).where(
                    Run.project_id == project_id, Run.idempotency_key == idempotency_key
                )
            ).scalar_one_or_none()
            if old:
                if old.intent_hash != fingerprint:
                    raise DomainError("IDEMPOTENCY_CONFLICT")
                return cast(RunRecord, dto(old))
            if parent_run_id and self._get(s, Run, parent_run_id).project_id != project_id:
                raise DomainError("PARENT_PROJECT_MISMATCH")
            run = Run(
                id=identifier(),
                project_id=project_id,
                snapshot_id=snapshot_id,
                idempotency_key=idempotency_key,
                intent_hash=fingerprint,
                parent_run_id=parent_run_id,
                limits=limits,
                status="QUEUED",
                plan_revision=0,
                cancel_requested=False,
                created_at=self.clock(),
                updated_at=self.clock(),
            )
            s.add(run)
            s.flush()
            self._add_step(s, run, "inspect", "inspect")
            self._event(s, run, "RUN_CREATED")
            return cast(RunRecord, dto(run))

    def get_run(self, run_id: str) -> RunRecord:
        with Session(self.db) as s:
            return cast(RunRecord, dto(self._get(s, Run, run_id)))

    def list_runs(self, project_id: str) -> list[Row]:
        with Session(self.db) as s:
            return [
                dto(r)
                for r in s.scalars(
                    select(Run).where(Run.project_id == project_id).order_by(Run.created_at)
                )
            ]

    def list_pending_cancel_runs(self) -> list[Row]:
        with Session(self.db) as s:
            return [
                dto(r)
                for r in s.scalars(
                    select(Run).where(Run.cancel_requested.is_(True), Run.status != "CANCELLED")
                )
            ]

    def events(self, run_id: str) -> list[Row]:
        with Session(self.db) as s:
            return [
                dto(e)
                for e in s.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.id))
            ]

    def add_step(
        self,
        run_id: str,
        logical_key: str,
        kind: str,
        payload: Row | None = None,
        position: int | None = None,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row:
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.status in TERMINAL or run.cancel_requested:
                raise DomainError("RUN_NOT_EXECUTABLE")
            if attempt_id is not None:
                current_run, _, _ = self._current(s, attempt_id, generation)
                if current_run.id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            return dto(self._add_step(s, run, logical_key, kind, payload, position))

    def get_step(self, step_id: str) -> Row:
        with Session(self.db) as s:
            return dto(self._get(s, Step, step_id))

    def list_steps(self, run_id: str) -> list[Row]:
        with Session(self.db) as s:
            return [
                dto(x)
                for x in s.scalars(
                    select(Step).where(Step.run_id == run_id).order_by(Step.position, Step.id)
                )
            ]

    def resume_run(self, run_id: str, payload: Row | None = None) -> RunRecord:
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.status != "NEEDS_INPUT" or run.cancel_requested:
                raise DomainError("RUN_NOT_RESUMABLE")
            for step in s.scalars(
                select(Step).where(Step.run_id == run_id, Step.status == "WAITING")
            ):
                step.status = "PENDING"
                if payload:
                    step.payload = {**step.payload, **payload}
            run.status, run.reason = "QUEUED", None
            self._event(s, run, "INPUT_PROVIDED")
            return cast(RunRecord, dto(run))

    def update_step_input(self, run_id: str, logical_key: str, payload: Row) -> Row:
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.cancel_requested or run.status in TERMINAL:
                raise DomainError("RUN_NOT_EXECUTABLE")
            step = s.execute(
                select(Step).where(Step.run_id == run_id, Step.logical_key == logical_key)
            ).scalar_one_or_none()
            if not step or step.status not in {"WAITING", "PENDING"}:
                raise DomainError("STEP_NOT_WAITING")
            step.payload = {**step.payload, **payload}
            return dto(step)

    def supply_input(self, run_id: str, step_id: str, payload: Row) -> RunRecord:
        """Operator supplies a held-out AI task, never a transformer-visible fixture."""
        with self._write() as (s, _):
            run, step = self._get(s, Run, run_id), self._get(s, Step, step_id)
            if step.run_id != run_id:
                raise DomainError("STEP_RUN_MISMATCH")
            if (
                run.cancel_requested
                or run.status != "NEEDS_INPUT"
                or step.status != "WAITING"
                or step.kind != "ai_handoff"
            ):
                raise DomainError("INPUT_NOT_EXPECTED")
            step.payload = {**step.payload, **payload}
            step.status = "PENDING"
            run.status, run.reason = "QUEUED", None
            self._event(s, run, "HELDOUT_INPUT_PROVIDED", {"step_id": step_id})
            return cast(RunRecord, dto(run))

    def bind_model_profile(
        self, run_id: str, profile_manifest: Row, *, attempt_id: str | None, generation: int | None
    ) -> Row:
        """Bind one full model configuration before the run's first dispatch.

        Only a secret reference belongs in this manifest. The resolved key never
        enters workflow state, events, project context or runner payloads.
        """
        if (
            not isinstance(profile_manifest, dict)
            or not isinstance(profile_manifest.get("version"), str)
            or not profile_manifest["version"]
        ):
            raise DomainError("INVALID_MODEL_PROFILE")
        if any(key in profile_manifest for key in {"api_key", "authorization", "secret", "token"}):
            raise DomainError("INVALID_MODEL_PROFILE")
        normalized: Row = dict(profile_manifest)
        capabilities = normalized.get("capabilities")
        if capabilities is not None:
            if not isinstance(capabilities, (list, tuple, set, frozenset)) or any(
                not isinstance(value, str) for value in capabilities
            ):
                raise DomainError("INVALID_MODEL_PROFILE")
            normalized["capabilities"] = sorted(set(capabilities))
        try:
            # Detach caller-owned containers and reject unserializable/NaN data.
            normalized = json.loads(json.dumps(normalized, sort_keys=True, allow_nan=False))
            profile_hash = digest(normalized)
        except (TypeError, ValueError):
            raise DomainError("INVALID_MODEL_PROFILE") from None
        with self._write() as (s, _):
            run, _, _ = self._current(s, attempt_id, generation)
            if run.id != run_id:
                raise DomainError("ATTEMPT_RUN_MISMATCH")
            selected_version = run.limits.get("model_profile_version")
            if selected_version and selected_version != normalized["version"]:
                raise DomainError("MODEL_PROFILE_CHANGED")
            bound = run.limits.get("model_profile")
            if bound is not None:
                if digest(bound) != profile_hash:
                    raise DomainError("MODEL_PROFILE_CHANGED")
                return cast(Row, bound)
            run.limits = {**run.limits, "model_profile": normalized}
            self._event(
                s,
                run,
                "MODEL_PROFILE_BOUND",
                {"profile_hash": profile_hash, "version": normalized["version"]},
            )
            return normalized
