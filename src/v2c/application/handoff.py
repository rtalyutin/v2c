"""Record human acceptance and seal the exact accepted candidate version."""

from __future__ import annotations

from ..contracts.operator import HumanHandoff
from ..contracts.workflow import RunRecord
from ..engine import DomainError, Engine
from ..storage import ArtifactStore


def record_human_handoff(
    engine: Engine,
    store: ArtifactStore,
    run_id: str,
    body: HumanHandoff,
    actor: str,
) -> RunRecord:
    """Caller must authorize the run owner before entering this use case."""
    run = engine.get_run(run_id)
    if run["status"] not in {"AWAITING_HANDOFF", "NEEDS_INPUT"}:
        raise DomainError("RUN_NOT_WAITING_FOR_HANDOFF")
    if run.get("final_digest") != body.final_digest:
        raise DomainError("FINAL_DIGEST_MISMATCH")
    engine.record_verification(
        run_id,
        "human_handoff",
        body.passed,
        {"actor": actor, "notes": body.evidence},
        body.final_digest,
    )
    if not body.passed:
        return engine.get_run(run_id)
    from ..orchestration.handoff import HandoffSealer

    HandoffSealer(engine, store).seal(run_id, body.final_digest)
    return engine.complete_handoff(run_id, body.final_digest)
