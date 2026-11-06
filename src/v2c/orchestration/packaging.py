"""Build candidate delivery artifacts and read bounded maintainer instructions."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Literal, Protocol, cast, overload

from ..contracts.control import PackagingControl
from ..contracts.storage import ArtifactStore
from ..contracts.workflow import (
    ArtifactPointer,
    InspectResult,
    PackageResult,
    RunClaim,
    StepRecord,
    TransformResult,
    VerifyResult,
)
from ..delivery import DeliveryError, package_delivery
from ..intake import Snapshot, restore_snapshot_archive
from .archive import screened_text
from .handoff import public_receipts


class PackagingSources(Protocol):
    @overload
    def required_result(self, run_id: str, kind: Literal["inspect"]) -> InspectResult: ...
    @overload
    def required_result(self, run_id: str, kind: Literal["verify"]) -> VerifyResult: ...
    @overload
    def required_result(self, run_id: str, kind: Literal["package"]) -> PackageResult: ...
    def source(self, claim: RunClaim) -> Snapshot: ...
    def artifact_bytes(self, ref: ArtifactPointer) -> bytes: ...
    def completed(self, run_id: str, kind: str) -> list[StepRecord]: ...


class PackagingOperations:
    def __init__(
        self,
        engine: PackagingControl,
        store: ArtifactStore,
        sources: PackagingSources,
        work_root: Path | None,
        instruction_max_bytes: int = 65536,
    ):
        self.engine, self.store, self.sources, self.work_root = engine, store, sources, work_root
        self.instruction_max_bytes = instruction_max_bytes

    def build(self, claim: RunClaim) -> PackageResult:
        verified = self.sources.required_result(claim["run"]["id"], "verify")
        inspected = self.sources.required_result(claim["run"]["id"], "inspect")
        source = self.sources.source(claim)
        work_root = self.work_root if self.work_root is not None else source.root.parent
        export_root = work_root / "deliveries" / claim["run"]["id"] / claim["attempt"]["id"]
        export_root.mkdir(parents=True, exist_ok=True)
        restored_path = export_root / "snapshot"
        if restored_path.exists():
            snapshot = Snapshot.load(restored_path)
        else:
            snapshot = restore_snapshot_archive(
                self.sources.artifact_bytes(verified["checkpoint"]),
                restored_path,
                original_snapshot=source,
            )
        recipe = inspected["recipe"]
        transformed = [
            cast(TransformResult, step["result"])
            for step in self.sources.completed(claim["run"]["id"], "transform")
        ]
        project_manifest = {
            "final_digest": verified["final_digest"],
            "environment_recipe": recipe,
            "commands": {
                label: [
                    {"argv": c["argv"], "cwd": c["cwd"], "evidence_id": c["id"]} for c in commands
                ]
                for label, commands in [
                    ("setup", recipe["setup"]),
                    ("test", recipe["tests"]),
                    ("restore", recipe["restore"]),
                ]
            },
            "map": {k: v for k, v in inspected["inspection"].items() if k != "workspace_root"},
            "changes": [{"work_id": s["work_id"], "files": s["files"]} for s in transformed],
            "examples": [
                {"file": f.removeprefix("repositories/"), "description": s["explanation"]}
                for s in transformed
                for f in s["files"]
            ],
            "limitations": recipe["limitations"]
            + ["Human and independent AI handover acceptance remain required."],
        }
        bundle = package_delivery(
            snapshot,
            export_root / "bundle",
            project_manifest=project_manifest,
            verification={
                "status": "CANDIDATE",
                "checks": public_receipts(self.engine.verifications(claim["run"]["id"])),
            },
            store=self.store,
            original_snapshot=source,
        )
        value = bundle.to_dict()
        # The delivery module supplies an immutable object reference; a candidate
        # is downloadable but does not grant READY by itself.
        ref = bundle.artifact.to_dict() if bundle.artifact else None
        if ref is None:
            raise DeliveryError("DELIVERY_ARTIFACT_REQUIRED")
        self.engine.record_artifact(
            claim["run"]["id"],
            "bundle",
            ref["key"],
            ref["sha256"],
            ref["size"],
            {**value, "final_digest": verified["final_digest"], "status": "CANDIDATE"},
            True,
            attempt_id=claim["attempt"]["id"],
            generation=claim["attempt"]["generation"],
        )
        return {
            "bundle": cast(ArtifactPointer, ref),
            "final_digest": verified["final_digest"],
            "delivery": value,
        }

    def instructions(self, claim: RunClaim) -> dict[str, str]:
        package = self.sources.required_result(claim["run"]["id"], "package")
        bundle = self.sources.artifact_bytes(package["bundle"])
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            docs = {}
            remaining = self.instruction_max_bytes
            for name in sorted(archive.namelist()):
                if (
                    "skills/" in name or name.endswith(("README.md", "ARCHITECTURE.md"))
                ) and archive.getinfo(name).file_size <= remaining:
                    body = archive.read(name)
                    text = screened_text(name, body)
                    if text is not None:
                        docs[name] = text
                        remaining -= len(body)
            return docs
