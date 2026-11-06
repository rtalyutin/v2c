"""Durable, fenced workflow. External I/O must happen outside these transactions."""

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts.workflow import RunRecord
from ..models import (
    Approval,
    Plan,
    Run,
    Step,
)

# Backwards-compatible exports; canonical policies live in workflow.states.
from ..workflow.errors import DomainError
from ..workflow.states import RUN_TERMINAL as TERMINAL
from .records import Row, digest, dto, identifier
from .workflow_rows import WorkflowRows


class PlanStore(WorkflowRows):
    def publish_plan(
        self,
        run_id: str,
        plan: Row,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row:
        with self._write() as (s, _):
            run = self._get(s, Run, run_id)
            if run.cancel_requested or run.status in TERMINAL:
                raise DomainError("RUN_NOT_EXECUTABLE")
            if attempt_id is not None:
                current_run, _, _ = self._current(s, attempt_id, generation)
                if current_run.id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            current = s.get(Plan, (run_id, run.plan_revision))
            if current and digest(current.document) == digest(plan):
                return {
                    "revision": current.revision,
                    "snapshot_id": current.snapshot_id,
                    "plan": current.document,
                }
            # A revised plan preserves stable step identities/counters.
            run.plan_revision += 1
            p = Plan(
                run_id=run.id,
                revision=run.plan_revision,
                snapshot_id=run.snapshot_id,
                document=plan,
                created_at=self.clock(),
            )
            s.add(p)
            self._event(s, run, "PLAN_PUBLISHED", {"revision": p.revision})
            return {"revision": p.revision, "snapshot_id": p.snapshot_id, "plan": p.document}

    def get_plan(self, run_id: str) -> Row | None:
        with Session(self.db) as s:
            run = self._get(s, Run, run_id)
            p = s.get(Plan, (run_id, run.plan_revision))
            return (
                {"revision": p.revision, "snapshot_id": p.snapshot_id, "plan": p.document}
                if p
                else None
            )

    def decide_plan(
        self,
        run_id: str,
        project_id: str,
        snapshot_id: str,
        plan_revision: int,
        approved: bool,
        actor: str,
    ) -> RunRecord:
        with self._write() as (s, lock):
            run = self._get(s, Run, run_id)
            if (run.project_id, run.snapshot_id, run.plan_revision) != (
                project_id,
                snapshot_id,
                plan_revision,
            ) or not plan_revision:
                raise DomainError("PLAN_VERSION_MISMATCH")
            if run.cancel_requested or run.status in TERMINAL:
                raise DomainError("RUN_NOT_EXECUTABLE")
            old = s.execute(
                select(Approval).where(
                    Approval.run_id == run_id, Approval.plan_revision == plan_revision
                )
            ).scalar_one_or_none()
            if old:
                if old.approved != approved:
                    raise DomainError("DECISION_CONFLICT")
                return cast(RunRecord, dto(run))
            if run.status != "AWAITING_PLAN_APPROVAL":
                raise DomainError("NOT_AWAITING_PLAN_APPROVAL")
            s.add(
                Approval(
                    id=identifier(),
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    plan_revision=plan_revision,
                    approved=approved,
                    actor=actor,
                    created_at=self.clock(),
                )
            )
            if approved:
                for waiting in s.scalars(
                    select(Step).where(
                        Step.run_id == run_id,
                        Step.kind.in_(["inspect", "plan"]),
                        Step.status == "WAITING",
                    )
                ):
                    waiting.status = "SUCCEEDED"
                plan = self._get(s, Plan, (run_id, plan_revision)).document
                works = plan.get("steps")
                if not isinstance(works, list) or any(
                    not isinstance(work, dict)
                    or not isinstance(work.get("id"), str)
                    or not work["id"]
                    for work in works
                ):
                    raise DomainError("PLAN_WORK_IDENTITY_REQUIRED")
                work_ids = [work["id"] for work in works]
                if len(work_ids) != len(set(work_ids)):
                    raise DomainError("PLAN_WORK_IDENTITY_CONFLICT")
                existing_works = list(
                    s.scalars(
                        select(Step)
                        .where(Step.run_id == run_id, Step.kind == "transform")
                        .order_by(Step.position)
                    )
                )
                if len(works) < len(existing_works):
                    raise DomainError("PLAN_WORK_IDENTITY_CONFLICT")
                # MVP plans may append genuinely new, explicitly approved work.
                # Existing machine identities and their positions are immutable:
                # neither insertion, reorder nor an ID rename creates new budget.
                # Human-readable title changes are harmless and retain counters.
                for index, existing in enumerate(existing_works):
                    if (
                        existing.logical_key != f"transform:{index}"
                        or works[index]["id"] != existing.payload["work_id"]
                    ):
                        raise DomainError("PLAN_WORK_IDENTITY_CONFLICT")
                for index, work in enumerate(works):
                    payload = {
                        "plan_revision": plan_revision,
                        "work_id": work.get("id", str(index)),
                        "plan_step": work,
                    }
                    step = self._add_step(s, run, f"transform:{index}", "transform", payload)
                    if step.status == "RUNNING":
                        raise DomainError("PLAN_WORK_ACTIVE")
                    previous_work = step.payload.get("plan_step", {})

                    def substantive(value: Row) -> Row:
                        return {k: v for k, v in value.items() if k not in {"id", "title"}}

                    changed_work = digest(substantive(previous_work)) != digest(substantive(work))
                    if step.status in {"WAITING", "FAILED", "PENDING"} or changed_work:
                        step.status, step.payload, step.plan_revision = (
                            "PENDING",
                            payload,
                            plan_revision,
                        )
                if not works:
                    self._add_step(
                        s,
                        run,
                        "verify",
                        "verify",
                        {"plan_revision": plan_revision, "no_transform": True},
                    )
                run.status, run.reason = "QUEUED", None
            else:
                run.status, run.reason = "NEEDS_INPUT", "PLAN_REJECTED"
            self._release(lock, run)
            self._event(
                s,
                run,
                "PLAN_DECISION",
                {"revision": plan_revision, "approved": approved, "actor": actor},
            )
            return cast(RunRecord, dto(run))
