"""Monotonic accounting observations; unknown cost is never zero."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Attempt,
    Run,
    Step,
    UsageEvent,
)
from ..workflow.errors import DomainError
from .records import Row, dto
from .unit_of_work import ControlDatabase


class UsageStore:
    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def record_usage(
        self, run_id: str, request_id: str, provider: str, usage: Row, attempt_id: str | None = None
    ) -> Row:
        with self.database.write() as (s, _):
            self.database.get(s, Run, run_id)
            if attempt_id:
                a = self.database.get(s, Attempt, attempt_id)
                if self.database.get(s, Step, a.step_id).run_id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            old = s.get(UsageEvent, request_id)
            if old:
                if old.run_id != run_id or old.provider != provider or old.attempt_id != attempt_id:
                    raise DomainError("USAGE_ID_CONFLICT")
                # Cost observations can arrive after cancel/recovery. Refine the
                # same request monotonically; never erase known quantities or
                # count a late response as another request. Preserve the before
                # image in the append-only event journal.
                unknown_flags = {"usage_unknown", "delivery_unknown"}

                def merge_observation(before: Row, after: Row) -> Row:
                    merged = dict(before)
                    for key, incoming in after.items():
                        previous = before.get(key)
                        if key in unknown_flags:
                            merged[key] = (
                                bool(previous) and bool(incoming)
                                if previous is not None
                                else bool(incoming)
                            )
                        elif previous is None:
                            merged[key] = incoming
                        elif incoming is None:
                            continue
                        elif isinstance(previous, dict) and isinstance(incoming, dict):
                            merged[key] = merge_observation(previous, incoming)
                        elif previous != incoming:
                            raise DomainError("USAGE_ID_CONFLICT")
                    return merged

                before = dict(old.usage)
                observed = dict(usage)
                if "usage" in observed:
                    observed["delivery_unknown"] = False
                    metrics = observed["usage"]
                    observed["usage_unknown"] = (
                        not isinstance(metrics, dict)
                        or not metrics
                        or any(value is None for value in metrics.values())
                    )
                if "usage" in before:
                    before.setdefault("delivery_unknown", False)
                    metrics = before["usage"]
                    before.setdefault(
                        "usage_unknown",
                        not isinstance(metrics, dict)
                        or not metrics
                        or any(value is None for value in metrics.values()),
                    )
                merged = merge_observation(before, observed)
                if merged != old.usage:
                    prior = old.usage
                    old.usage = merged
                    self.database.event(
                        s,
                        self.database.get(s, Run, run_id),
                        "USAGE_REFINED",
                        {"request_id": request_id, "before": prior, "after": merged},
                    )
                return dto(old)
            u = UsageEvent(
                id=request_id,
                run_id=run_id,
                attempt_id=attempt_id,
                provider=provider,
                usage=usage,
                created_at=self.database.clock(),
            )
            s.add(u)
            return dto(u)

    def usage(self, run_id: str) -> list[Row]:
        with Session(self.database.db) as s:
            return [
                dto(u)
                for u in s.scalars(
                    select(UsageEvent)
                    .where(UsageEvent.run_id == run_id)
                    .order_by(UsageEvent.created_at)
                )
            ]
