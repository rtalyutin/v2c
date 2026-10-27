"""Seal receipt metadata without changing accepted code or executing a project."""

from __future__ import annotations

import hashlib
import json

from ..contracts.workflow import JsonObject
from ..engine import REQUIRED_VERIFICATIONS, DomainError, Engine, digest
from ..storage import ArtifactStore
from .archive import archive_executables, checked_archive_files, code_zip
from .common import json_bytes
from .sources import read_artifact


def public_receipts(rows: list[JsonObject]) -> list[JsonObject]:
    """Evidence references travel; hidden assertions and answers do not."""
    return [
        {
            **{
                key: row[key]
                for key in ("id", "kind", "passed", "final_digest", "sequence")
                if key in row
            },
            "evidence_sha256": digest(row["evidence"]),
            "test_double": bool(row["evidence"].get("test_double", False)),
            **({"actor": row["evidence"]["actor"]} if row["evidence"].get("actor") else {}),
        }
        for row in rows
    ]


class HandoffSealer:
    def __init__(self, engine: Engine, store: ArtifactStore):
        self.engine, self.store = engine, store

    def seal(self, run_id: str, final_digest: str) -> JsonObject:
        """Trusted operator finalization: code and skills stay byte-for-byte fixed.

        This synchronously seals already recorded acceptance. It performs no
        model/runner operation and never invents human or AI acceptance. READY is
        a subsequent Engine transition guarded by these exact receipt identities.
        """
        run = self.engine.get_run(run_id)
        if run["cancel_requested"] or run["status"] not in {"AWAITING_HANDOFF", "READY"}:
            raise DomainError("HANDOFF_NOT_AVAILABLE")
        if run.get("final_digest") != final_digest:
            raise DomainError("FINAL_VERSION_MISMATCH")
        if any(
            s["status"] in {"PENDING", "RUNNING", "FAILED"} for s in self.engine.list_steps(run_id)
        ):
            raise DomainError("WORK_INCOMPLETE")
        latest: dict[str, JsonObject] = {}
        for row in self.engine.verifications(run_id):
            if row["final_digest"] == final_digest:
                latest[row["kind"]] = row
        if not REQUIRED_VERIFICATIONS <= {kind for kind, row in latest.items() if row["passed"]}:
            raise DomainError("HANDOFF_NOT_VERIFIED")
        sealed_receipts = {kind: latest[kind]["id"] for kind in sorted(REQUIRED_VERIFICATIONS)}
        candidates = [
            a
            for a in self.engine.artifacts(run_id)
            if a["kind"] == "bundle"
            and a["verified"]
            and a["manifest"].get("final_digest") == final_digest
        ]
        for existing in reversed(candidates):
            if (
                existing["manifest"].get("status") == "VERIFIED"
                and existing["manifest"].get("sealed_receipts") == sealed_receipts
            ):
                return existing
        if not candidates:
            raise DomainError("HANDOFF_BUNDLE_MISSING")
        candidate = candidates[-1]
        archive = read_artifact(
            self.store,
            {
                "key": candidate["object_key"],
                "sha256": candidate["sha256"],
                "size": candidate["size"],
            },
        )
        files = checked_archive_files(archive, max_file_bytes=64 * 1024 * 1024)
        executable_paths = archive_executables(archive)
        if "manifest.json" not in files:
            raise DomainError("HANDOFF_MANIFEST_MISSING")
        manifest = json.loads(files["manifest.json"])
        expected_paths = {entry["path"] for entry in manifest["files"]}
        if expected_paths != set(files) - {"manifest.json"}:
            raise DomainError("HANDOFF_MANIFEST_MISMATCH")
        for entry in manifest["files"]:
            body = files[entry["path"]]
            if len(body) != entry["size"] or hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise DomainError("HANDOFF_MANIFEST_MISMATCH")
            if "executable" in entry and entry["executable"] != (entry["path"] in executable_paths):
                raise DomainError("HANDOFF_MANIFEST_MISMATCH")
        receipts = public_receipts([latest[kind] for kind in sorted(REQUIRED_VERIFICATIONS)])
        receipt_document = {
            "version": 1,
            "status": "VERIFIED",
            "final_digest": final_digest,
            "sealed_receipts": sealed_receipts,
            "receipts": receipts,
        }
        files["docs/handoff-receipts.json"] = json_bytes(receipt_document) + b"\n"
        verification = json.loads(files.get("docs/verification.json", b"{}"))
        verification.update(
            status="VERIFIED",
            checks=receipts,
            sealed_receipts=sealed_receipts,
            final_digest=final_digest,
        )
        pending_text = "Human and independent AI handover acceptance remain required."
        verification["limitations"] = [
            v for v in verification.get("limitations", []) if v != pending_text
        ]
        files["docs/verification.json"] = json_bytes(verification) + b"\n"
        manifest.update(
            status="VERIFIED", final_digest=final_digest, sealed_receipts=sealed_receipts
        )
        manifest["limitations"] = [v for v in manifest.get("limitations", []) if v != pending_text]
        manifest["files"] = [
            {
                "path": path,
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "executable": path in executable_paths,
            }
            for path, body in sorted(files.items())
            if path != "manifest.json"
        ]
        files["manifest.json"] = json_bytes(manifest) + b"\n"
        # Only metadata changes: a sealing error must not rewrite accepted code,
        # Git bundles, project instructions or the evidence used to verify them.
        sealed_bytes = code_zip(files, executable_paths=executable_paths)
        ref = self.store.put_bytes(
            f"runs/{run_id}/bundles/{hashlib.sha256(sealed_bytes).hexdigest()}.zip", sealed_bytes
        )
        return self.engine.record_artifact(
            run_id,
            "bundle",
            ref.key,
            ref.sha256,
            ref.size,
            {
                "status": "VERIFIED",
                "final_digest": final_digest,
                "sealed_receipts": sealed_receipts,
                "sealed_receipts_digest": digest(receipt_document),
                "candidate_artifact_id": candidate["id"],
                "manifest": manifest,
            },
            True,
        )
