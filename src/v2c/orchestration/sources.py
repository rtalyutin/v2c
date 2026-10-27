"""Stored source restoration and checkpoint provenance."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast, overload

from ..contracts.ports import PipelineSettings, RunnerPort
from ..contracts.workflow import (
    ArtifactPointer,
    BaselineResult,
    CheckpointRef,
    HandoffResult,
    InspectResult,
    JsonObject,
    PackageResult,
    RecipePayload,
    RunClaim,
    StepRecord,
    TransformResult,
    VerifyResult,
)
from ..diagnostics import annotate_exception
from ..engine import Engine
from ..intake import Snapshot
from ..storage import ArtifactStore
from .archive import archive_context, assert_source_unchanged
from .common import InputRequired, PipelineLimits, stable_id
from .journal import OperationJournal


def read_artifact(store: ArtifactStore, ref: ArtifactPointer) -> bytes:
    data = store.get_bytes(ref["key"])
    if hashlib.sha256(data).hexdigest() != ref["sha256"] or len(data) != ref["size"]:
        raise InputRequired("CHECKPOINT_CHECKSUM_MISMATCH")
    return data


class SourceArtifacts:
    def __init__(
        self,
        engine: Engine,
        store: ArtifactStore,
        journal: OperationJournal,
        limits: PipelineLimits,
        settings: PipelineSettings | None,
    ):
        self.engine, self.store, self.journal = engine, store, journal
        self.limits, self.settings = limits, settings

    def source(self, claim: RunClaim) -> Snapshot:
        manifest = self.engine.get_snapshot(claim["run"]["snapshot_id"])["manifest"]
        root = manifest.get("workspace_root") or manifest.get("root")
        if not root:
            raise InputRequired("SOURCE_WORKSPACE_MISSING")
        try:
            snapshot = Snapshot.load(Path(root))
        except FileNotFoundError:
            from ..intake import restore_source_snapshot

            if not manifest.get("archive_key") or not manifest.get("source_git_bundles"):
                raise InputRequired("SOURCE_WORKSPACE_MISSING") from None
            archive = self.store.get_bytes(manifest["archive_key"])
            if hashlib.sha256(archive).hexdigest() != manifest["archive_sha256"]:
                raise InputRequired("SOURCE_ARCHIVE_CHECKSUM_MISMATCH")
            bundles = {
                name: self.artifact_bytes(ref)
                for name, ref in manifest["source_git_bundles"].items()
            }
            destination = (
                Path(getattr(self.settings, "work_root", Path(root).parent))
                / "restored-sources"
                / claim["run"]["snapshot_id"]
            )
            snapshot = restore_source_snapshot(manifest, archive, bundles, destination)
        if snapshot.project_id != claim["run"]["project_id"]:
            raise InputRequired("SOURCE_PROJECT_MISMATCH")
        return snapshot

    def completed(self, run_id: str, kind: str) -> list[StepRecord]:
        return [
            cast(StepRecord, s)
            for s in self.engine.list_steps(run_id)
            if s["kind"] == kind and s["status"] == "SUCCEEDED"
        ]

    @overload
    def required_result(self, run_id: str, kind: Literal["inspect"]) -> InspectResult: ...

    @overload
    def required_result(self, run_id: str, kind: Literal["baseline"]) -> BaselineResult: ...

    @overload
    def required_result(self, run_id: str, kind: Literal["transform"]) -> TransformResult: ...

    @overload
    def required_result(self, run_id: str, kind: Literal["verify"]) -> VerifyResult: ...

    @overload
    def required_result(self, run_id: str, kind: Literal["package"]) -> PackageResult: ...

    @overload
    def required_result(self, run_id: str, kind: Literal["ai_handoff"]) -> HandoffResult: ...

    @overload
    def required_result(self, run_id: str, kind: str) -> Mapping[str, object]: ...

    def required_result(self, run_id: str, kind: str) -> Mapping[str, object]:
        matches = self.completed(run_id, kind)
        if not matches:
            raise InputRequired("PREREQUISITE_MISSING", {"stage": kind})
        return cast(Mapping[str, object], matches[-1]["result"])

    def last_checkpoint(self, claim: RunClaim) -> CheckpointRef:
        previous = [
            s
            for s in self.engine.list_steps(claim["run"]["id"])
            if s["position"] < claim["step"]["position"]
            and s["status"] == "SUCCEEDED"
            and (s.get("result") or {}).get("checkpoint")
        ]
        if not previous:
            raise InputRequired("CHECKPOINT_MISSING")
        return cast(CheckpointRef, previous[-1]["result"]["checkpoint"])

    async def checkpoint(
        self,
        claim: RunClaim,
        runner: RunnerPort,
        workspace: str,
        recipe: RecipePayload,
        *,
        key: str,
        expected_source: bytes,
    ) -> CheckpointRef:
        try:
            return await self._checkpoint(
                claim, runner, workspace, recipe, key=key, expected_source=expected_source
            )
        except Exception as error:
            annotate_exception(error, command_id=stable_id(claim["attempt"]["id"], key))
            raise

    async def _checkpoint(
        self,
        claim: RunClaim,
        runner: RunnerPort,
        workspace: str,
        recipe: RecipePayload,
        *,
        key: str,
        expected_source: bytes,
    ) -> CheckpointRef:
        op_id, row, _ = self.journal.prepare(
            claim, key, {"operation": "checkpoint", "workspace_id": workspace}
        )
        if row["status"] == "SUCCEEDED":
            return cast(CheckpointRef, row["result"])
        archive = assert_source_unchanged(expected_source, await runner.checkpoint(workspace))
        # Exported files remain untrusted. Full safe import runs again before
        # packaging; this bounded context check detects obvious secret leakage.
        archive_context(archive, max_bytes=self.limits.model_max_input_bytes // 2)
        sha = hashlib.sha256(archive).hexdigest()
        ref = self.store.put_bytes(f"runs/{claim['run']['id']}/checkpoints/{sha}.zip", archive)
        source = self.source(claim)
        manifest = {
            "repositories": [r.name for r in source.repositories],
            "plan_revision": claim["run"]["plan_revision"],
            "environment": recipe,
            "files": ref.to_dict(),
            "services": {
                "strategy": recipe["state_strategy"],
                "restore": recipe["restore"],
                "checks": recipe["recovery_checks"],
            },
            "restore_verified": False,
        }
        artifact = self.engine.record_artifact(
            claim["run"]["id"],
            "checkpoint",
            ref.key,
            ref.sha256,
            ref.size,
            manifest,
            True,
            attempt_id=claim["attempt"]["id"],
            generation=claim["attempt"]["generation"],
        )
        self.engine.confirm_checkpoint(
            claim["run"]["id"], artifact["id"], restore_verified=False, **self.journal.fence(claim)
        )
        checkpoint: CheckpointRef = {
            "key": ref.key,
            "sha256": ref.sha256,
            "size": ref.size,
            "artifact_id": artifact["id"],
            "manifest": manifest,
        }
        self.journal.observe(claim, op_id, "SUCCEEDED", cast(JsonObject, checkpoint))
        return checkpoint

    def artifact_bytes(self, ref: ArtifactPointer) -> bytes:
        return read_artifact(self.store, ref)
