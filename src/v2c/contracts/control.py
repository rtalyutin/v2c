"""Consumer-owned control ports. Each write is a complete atomic scenario.

Implementations own locking, rollback and generation guards; callers never receive
an ORM Session. The Engine compatibility facade structurally supplies these ports.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .records import Row, SnapshotRecord
from .workflow import RunClaim, RunRecord


class RunReader(Protocol):
    def get_run(self, run_id: str) -> RunRecord: ...


class JournalControl(Protocol):
    @property
    def clock(self) -> Callable[[], float]: ...

    def get_run(self, run_id: str) -> RunRecord: ...

    def assert_current(self, attempt_id: str | None, generation: int | None) -> RunClaim: ...

    def get_step(self, step_id: str) -> Row: ...

    def get_command(self, command_id: str) -> Row: ...

    def prepare_command(
        self, attempt_id: str | None, generation: int | None, command_id: str, request: Row
    ) -> Row: ...

    def record_command_result(
        self, command_id: str, generation: int | None, status: str, result: Row | None = None
    ) -> Row: ...


class ModelControl(Protocol):
    def bind_model_profile(
        self, run_id: str, profile_manifest: Row, *, attempt_id: str | None, generation: int | None
    ) -> Row: ...

    def record_usage(
        self, run_id: str, request_id: str, provider: str, usage: Row, attempt_id: str | None = None
    ) -> Row: ...


class ExecutionControl(Protocol):
    @property
    def clock(self) -> Callable[[], float]: ...

    def get_run(self, run_id: str) -> RunRecord: ...

    def commands_for_attempt(self, attempt_id: str | None) -> list[Row]: ...

    def confirm_cancel(self, run_id: str, stop_confirmed: bool = False) -> RunRecord: ...

    def confirm_reconciliation(
        self, attempt_id: str | None, generation: int | None, stop_confirmed: bool = False
    ) -> Row: ...

    def finish_attempt(
        self,
        attempt_id: str | None,
        generation: int | None,
        result: Row | None = None,
        outcome: str = "SUCCEEDED",
        reason: str | None = None,
        retryable: bool = False,
        execution_confirmed: bool = True,
    ) -> RunRecord: ...

    def list_commands(
        self, run_id: str | None = None, attempt_id: str | None = None
    ) -> list[Row]: ...

    def record_usage(
        self, run_id: str, request_id: str, provider: str, usage: Row, attempt_id: str | None = None
    ) -> Row: ...

    def set_waiting(
        self,
        attempt_id: str | None,
        generation: int | None,
        status: str,
        reason: str | None,
        payload: Row | None = None,
    ) -> RunRecord: ...

    def usage(self, run_id: str) -> list[Row]: ...


class SourceControl(Protocol):
    def confirm_checkpoint(
        self,
        run_id: str,
        artifact_id: str | None,
        restore_verified: bool = False,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row: ...

    def get_snapshot(self, snapshot_id: str) -> SnapshotRecord: ...

    def list_steps(self, run_id: str) -> list[Row]: ...

    def record_artifact(
        self,
        run_id: str,
        kind: str,
        object_key: str,
        sha256: str,
        size: int,
        manifest: Row | None = None,
        verified: bool = False,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row: ...


class PackagingControl(Protocol):
    def record_artifact(
        self,
        run_id: str,
        kind: str,
        object_key: str,
        sha256: str,
        size: int,
        manifest: Row | None = None,
        verified: bool = False,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row: ...

    def verifications(self, run_id: str) -> list[Row]: ...


class HandoffControl(Protocol):
    def artifacts(self, run_id: str) -> list[Row]: ...

    def get_run(self, run_id: str) -> RunRecord: ...

    def list_steps(self, run_id: str) -> list[Row]: ...

    def record_artifact(
        self,
        run_id: str,
        kind: str,
        object_key: str,
        sha256: str,
        size: int,
        manifest: Row | None = None,
        verified: bool = False,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row: ...

    def verifications(self, run_id: str) -> list[Row]: ...


class StageControl(Protocol):
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
    ) -> Row: ...

    def publish_plan(
        self,
        run_id: str,
        plan: Row,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row: ...

    def set_waiting(
        self,
        attempt_id: str | None,
        generation: int | None,
        status: str,
        reason: str | None,
        payload: Row | None = None,
    ) -> RunRecord: ...

    def get_plan(self, run_id: str) -> Row | None: ...

    def list_steps(self, run_id: str) -> list[Row]: ...

    def record_verification(
        self,
        run_id: str,
        kind: str,
        passed: bool,
        evidence: Row,
        final_digest: str,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row: ...

    def confirm_checkpoint(
        self,
        run_id: str,
        artifact_id: str | None,
        restore_verified: bool = False,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row: ...

    def get_run(self, run_id: str) -> RunRecord: ...


class AttemptControl(Protocol):
    def get_step(self, step_id: str) -> Row: ...
    def note_pending(self, attempt_id: str, generation: int, reason: str, details: Row) -> None: ...

    def get_run(self, run_id: str) -> RunRecord: ...

    def commands_for_attempt(self, attempt_id: str | None) -> list[Row]: ...

    def set_waiting(
        self,
        attempt_id: str | None,
        generation: int | None,
        status: str,
        reason: str | None,
        payload: Row | None = None,
    ) -> RunRecord: ...

    def finish_attempt(
        self,
        attempt_id: str | None,
        generation: int | None,
        result: Row | None = None,
        outcome: str = "SUCCEEDED",
        reason: str | None = None,
        retryable: bool = False,
        execution_confirmed: bool = True,
    ) -> RunRecord: ...


class WorkerControl(Protocol):
    def list_pending_cancel_runs(self) -> list[Row]: ...

    def claim_next(self, worker_id: str, lease_seconds: int = 60) -> RunClaim | None: ...

    def list_recoverable_runs(self) -> list[Row]: ...

    def recover_expired(
        self,
        run_id: str,
        worker_id: str,
        execution_confirmed: bool = False,
        stop_confirmed: bool = False,
        lease_seconds: int = 60,
    ) -> RunClaim: ...

    def heartbeat(
        self,
        attempt_id: str | None,
        generation: int | None,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> Row: ...

    def get_run(self, run_id: str) -> RunRecord: ...


class ImportControl(Protocol):
    def create_snapshot(
        self, project_id: str, manifest: Row | None, artifact_id: str | None = None
    ) -> SnapshotRecord: ...


class DecisionControl(Protocol):
    def get_run(self, run_id: str) -> RunRecord: ...

    def decide_plan(
        self,
        run_id: str,
        project_id: str,
        snapshot_id: str,
        plan_revision: int,
        approved: bool,
        actor: str,
    ) -> RunRecord: ...


class AcceptanceControl(Protocol):
    def get_run(self, run_id: str) -> RunRecord: ...

    def list_steps(self, run_id: str) -> list[Row]: ...

    def supply_input(self, run_id: str, step_id: str, payload: Row) -> RunRecord: ...


class HumanHandoffControl(Protocol):
    def get_run(self, run_id: str) -> RunRecord: ...

    def record_verification(
        self,
        run_id: str,
        kind: str,
        passed: bool,
        evidence: Row,
        final_digest: str,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row: ...

    def complete_handoff(self, run_id: str, final_digest: str) -> RunRecord: ...


class ControlPlane(
    JournalControl,
    ModelControl,
    ExecutionControl,
    SourceControl,
    PackagingControl,
    HandoffControl,
    StageControl,
    AttemptControl,
    WorkerControl,
    ImportControl,
    DecisionControl,
    AcceptanceControl,
    HumanHandoffControl,
    Protocol,
):
    """Composition-only aggregate; individual consumers use the smaller ports above."""
