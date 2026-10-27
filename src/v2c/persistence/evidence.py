"""Artifact and acceptance evidence, including atomic readiness gate."""

from __future__ import annotations

from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..contracts.workflow import RunRecord
from ..models import (
    Artifact,
    Run,
    Step,
    VerificationResult,
)
from ..workflow.errors import DomainError
from ..workflow.states import REQUIRED_VERIFICATIONS
from .records import Row, dto, identifier
from .unit_of_work import ControlDatabase


class EvidenceStore:
    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

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
    ) -> Row:
        if len(sha256) != 64 or size < 0 or not verified:
            raise DomainError("ARTIFACT_NOT_VERIFIED")
        with self.database.write() as (s, _):
            run = self.database.get(s, Run, run_id)
            if attempt_id is not None:
                current_run, _, _ = self.database.current(s, attempt_id, generation)
                if current_run.id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            artifact = Artifact(
                id=identifier(),
                run_id=run_id,
                kind=kind,
                object_key=object_key,
                sha256=sha256,
                size=size,
                manifest=manifest or {},
                verified=True,
                restore_verified=False,
                created_at=self.database.clock(),
            )
            s.add(artifact)
            if kind == "bundle" and manifest and manifest.get("final_digest"):
                if run.final_digest is not None and run.final_digest != manifest["final_digest"]:
                    raise DomainError("FINAL_VERSION_MISMATCH")
                run.final_digest = manifest["final_digest"]
            self.database.event(
                s,
                run,
                "ARTIFACT_PUBLISHED",
                {"artifact_id": artifact.id, "kind": kind, "sha256": sha256},
            )
            return dto(artifact)

    def artifacts(self, run_id: str) -> list[Row]:
        with Session(self.database.db) as s:
            return [
                dto(x)
                for x in s.scalars(
                    select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
                )
            ]

    def confirm_checkpoint(
        self,
        run_id: str,
        artifact_id: str | None,
        restore_verified: bool = False,
        *,
        attempt_id: str | None = None,
        generation: int | None = None,
    ) -> Row:
        with self.database.write() as (s, _):
            run, artifact = (
                self.database.get(s, Run, run_id),
                self.database.get(s, Artifact, artifact_id),
            )
            if attempt_id is not None:
                current_run, _, _ = self.database.current(s, attempt_id, generation)
                if current_run.id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            if artifact.run_id != run_id or artifact.kind != "checkpoint" or not artifact.verified:
                raise DomainError("INVALID_CHECKPOINT")
            required = {"repositories", "plan_revision", "environment", "files", "services"}
            if not required <= artifact.manifest.keys():
                raise DomainError("INCOMPLETE_CHECKPOINT")
            artifact.restore_verified = bool(restore_verified)
            run.checkpoint_id = artifact_id
            self.database.event(
                s,
                run,
                "CHECKPOINT_CONFIRMED",
                {"artifact_id": artifact_id, "restore_verified": restore_verified},
            )
            return dto(artifact)

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
    ) -> Row:
        if not final_digest or not evidence:
            raise DomainError("VERIFICATION_EVIDENCE_REQUIRED")
        with self.database.write() as (s, _):
            run = self.database.get(s, Run, run_id)
            if attempt_id is not None:
                current_run, _, _ = self.database.current(s, attempt_id, generation)
                if current_run.id != run_id:
                    raise DomainError("ATTEMPT_RUN_MISMATCH")
            sequence = (
                s.scalar(
                    select(func.max(VerificationResult.sequence)).where(
                        VerificationResult.run_id == run_id
                    )
                )
                or 0
            ) + 1
            v = VerificationResult(
                id=identifier(),
                run_id=run_id,
                kind=kind,
                passed=passed,
                evidence=evidence,
                final_digest=final_digest,
                sequence=sequence,
                created_at=self.database.clock(),
            )
            s.add(v)
            if (
                run.status == "READY"
                and kind in REQUIRED_VERIFICATIONS
                and final_digest == run.final_digest
            ):
                run.status, run.reason = "AWAITING_HANDOFF", "HANDOFF_RECEIPTS_CHANGED"
            self.database.event(
                s,
                run,
                "VERIFICATION_RECORDED",
                {"kind": kind, "passed": passed, "final_digest": final_digest},
            )
            return dto(v)

    def verifications(self, run_id: str) -> list[Row]:
        with Session(self.database.db) as s:
            return [
                dto(v)
                for v in s.scalars(
                    select(VerificationResult)
                    .where(VerificationResult.run_id == run_id)
                    .order_by(VerificationResult.sequence)
                )
            ]

    def complete_handoff(self, run_id: str, final_digest: str) -> RunRecord:
        with self.database.write() as (s, lock):
            run = self.database.get(s, Run, run_id)
            if run.cancel_requested or run.status not in {"AWAITING_HANDOFF", "READY"}:
                raise DomainError("HANDOFF_NOT_AVAILABLE")
            if s.scalar(
                select(func.count())
                .select_from(Step)
                .where(Step.run_id == run_id, Step.status.in_(["PENDING", "RUNNING", "FAILED"]))
            ):
                raise DomainError("WORK_INCOMPLETE")
            if run.final_digest != final_digest:
                raise DomainError("FINAL_VERSION_MISMATCH")
            latest = {}
            for v in s.scalars(
                select(VerificationResult)
                .where(
                    VerificationResult.run_id == run_id,
                    VerificationResult.final_digest == final_digest,
                )
                .order_by(VerificationResult.sequence)
            ):
                latest[v.kind] = v
            if not REQUIRED_VERIFICATIONS <= {
                k for k, verification in latest.items() if verification.passed
            }:
                raise DomainError("HANDOFF_NOT_VERIFIED")
            expected_receipts = {kind: latest[kind].id for kind in REQUIRED_VERIFICATIONS}
            bundles = list(
                s.scalars(
                    select(Artifact).where(
                        Artifact.run_id == run_id,
                        Artifact.kind == "bundle",
                        Artifact.verified.is_(True),
                    )
                )
            )
            if not any(a.manifest.get("final_digest") == final_digest for a in bundles):
                raise DomainError("HANDOFF_BUNDLE_MISSING")
            if not any(
                a.manifest.get("final_digest") == final_digest
                and a.manifest.get("status") == "VERIFIED"
                and a.manifest.get("sealed_receipts") == expected_receipts
                for a in bundles
            ):
                raise DomainError("HANDOFF_BUNDLE_NOT_SEALED")
            run.status, run.final_digest, run.reason = "READY", final_digest, None
            self.database.release(lock, run)
            self.database.event(s, run, "HANDOFF_READY", {"final_digest": final_digest})
            return cast(RunRecord, dto(run))
