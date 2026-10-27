"""Bounded source import and durable snapshot publication.

Authentication/ownership and HTTP headers belong to the caller. The use case
publishes its database pointer only after every immutable source object exists.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import AsyncIterable
from pathlib import Path

from pydantic import ValidationError

from ..adapters.model import EnvSecretResolver
from ..config import Settings
from ..contracts.operator import RepositoryImport
from ..engine import Engine
from ..intake import (
    HTTPSGitFetcher,
    RepositoryInput,
    import_repositories,
    safe_import_archive,
    snapshot_archive_bytes,
    source_git_bundles,
)
from ..persistence.records import SnapshotRecord
from ..storage import ArtifactStore


class ImportRejected(Exception):
    """A safe public error code, without credential-bearing source details."""

    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


async def import_snapshot(
    engine: Engine,
    store: ArtifactStore,
    settings: Settings,
    project_id: str,
    chunks: AsyncIterable[bytes],
    *,
    repositories_json: bool,
) -> SnapshotRecord:
    directory = settings.work_root / project_id / "imports" / str(uuid.uuid4())
    directory.parent.mkdir(parents=True, exist_ok=True)
    received = 0
    with tempfile.NamedTemporaryFile(dir=directory.parent, suffix=".upload") as incoming:
        async for chunk in chunks:
            received += len(chunk)
            if received > settings.max_upload_bytes:
                raise ImportRejected("IMPORT_TOO_LARGE", 413)
            incoming.write(chunk)
        incoming.flush()
        try:
            if repositories_json:
                request = RepositoryImport.model_validate_json(Path(incoming.name).read_bytes())
                repositories = [
                    RepositoryInput(**repository.model_dump())
                    for repository in request.repositories
                ]
                fetcher = HTTPSGitFetcher(settings.git_allowed_hosts, EnvSecretResolver())
                imported = await asyncio.to_thread(
                    import_repositories,
                    repositories,
                    directory,
                    project_id=project_id,
                    fetcher=fetcher,
                )
            else:
                imported = await asyncio.to_thread(
                    safe_import_archive,
                    Path(incoming.name),
                    directory,
                    project_id=project_id,
                )
        except (ValueError, ValidationError) as error:
            raise ImportRejected("INVALID_IMPORT", 422) from error

    manifest = dict(imported.manifest)
    manifest["workspace_root"] = str(imported.root.resolve())
    archive = await asyncio.to_thread(snapshot_archive_bytes, imported)
    key = f"snapshots/{project_id}/{imported.snapshot_id}.zip"
    ref = await asyncio.to_thread(store.put_bytes, key, archive)
    manifest["archive_key"] = ref.key
    manifest["archive_sha256"] = ref.sha256
    manifest["archive_size"] = ref.size
    git_bundles = await asyncio.to_thread(source_git_bundles, imported)
    manifest["source_git_bundles"] = {}
    for name, data in git_bundles.items():
        bundle_ref = await asyncio.to_thread(
            store.put_bytes, f"snapshots/{project_id}/{imported.snapshot_id}/{name}.bundle", data
        )
        manifest["source_git_bundles"][name] = {
            "key": bundle_ref.key,
            "sha256": bundle_ref.sha256,
            "size": bundle_ref.size,
        }
    return engine.create_snapshot(project_id, manifest)
