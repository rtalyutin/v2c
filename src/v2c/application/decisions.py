"""Transport-independent plan and acceptance input scenarios."""

from __future__ import annotations

from ..contracts.control import AcceptanceControl, DecisionControl
from ..contracts.operator import Acceptance, Decision
from ..contracts.workflow import RunRecord
from ..workflow.errors import DomainError


def decide_plan(control: DecisionControl, run_id: str, decision: Decision, actor: str) -> RunRecord:
    run = control.get_run(run_id)
    return control.decide_plan(
        run_id,
        run["project_id"],
        decision.snapshot_id,
        decision.plan_revision,
        decision.approved,
        actor,
    )


def supply_acceptance(control: AcceptanceControl, run_id: str, acceptance: Acceptance) -> RunRecord:
    run = control.get_run(run_id)
    if run["status"] not in {"NEEDS_INPUT", "AWAITING_HANDOFF"}:
        raise DomainError("RUN_NOT_WAITING_FOR_ACCEPTANCE")
    target = next((s for s in control.list_steps(run_id) if s["kind"] == "ai_handoff"), None)
    if target is None:
        raise DomainError("CANDIDATE_NOT_AVAILABLE")
    payload = acceptance.model_dump()
    payload["checks"] = [
        {"id": f"acceptance_{i + 1}", **check} for i, check in enumerate(payload["checks"])
    ]
    return control.supply_input(run_id, target["id"], payload)
