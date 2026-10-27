"""Workspace lifecycle and command orchestration inside a runner VM."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import secrets
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from ..adapters.runner import (
    AuthorizationRequest,
    CommandRequest,
    CommandResult,
    PostgresService,
    PrepareRequest,
    RunnerError,
)
from .archive import _path, safe_extract
from .docker import DockerBackend, ExecutionBackend
from .journal import TERMINAL, Journal
from .settings import RunnerSettings


class RunnerService:
    def __init__(self, settings: RunnerSettings, backend: ExecutionBackend | None = None):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces = settings.data_dir / "workspaces"
        self.workspaces.mkdir(exist_ok=True)
        self.journal = Journal(settings.data_dir / "journal.sqlite3")
        self.backend = backend or DockerBackend(settings, self.journal)
        self.lock = self.journal.lock
        self.threads: dict[str, threading.Thread] = {}

    def _runtime_definition(self, request: PrepareRequest) -> dict[str, Any]:
        manifest = request.input_manifest
        recipe = manifest.get("environment_recipe", {})
        raw_services = recipe.get("services", []) if isinstance(recipe, dict) else None
        try:
            if not isinstance(raw_services, list) or len(raw_services) > 1:
                raise ValueError("at most one service")
            services = [PostgresService.model_validate(item) for item in raw_services]
        except (ValueError, TypeError, ValidationError) as error:
            raise RunnerError(
                "UNSUPPORTED", "only one bounded PostgreSQL test service is supported"
            ) from error
        if services:
            if not self.settings.postgres_image:
                raise RunnerError(
                    "UNSUPPORTED", "operator PostgreSQL image/profile is not configured"
                )
            if (
                recipe.get("state_strategy") != "rebuild_from_fixtures"
                or not recipe.get("restore")
                or not recipe.get("recovery_checks")
            ):
                raise RunnerError(
                    "INVALID_INPUT", "PostgreSQL requires fixture restoration and recovery checks"
                )
            if not hasattr(self.backend, "prepare_runtime"):
                raise RunnerError(
                    "UNSUPPORTED", "execution backend does not implement test services"
                )
        profile = manifest.get("execution_limits")
        s = self.settings
        limits = {
            "cpus": s.cpu_limit,
            "memory_bytes": s.memory_bytes,
            "pids": s.pids_limit,
            "max_output_bytes": s.max_output_bytes,
        }
        if profile is None:
            if isinstance(self.backend, DockerBackend):
                raise RunnerError("INVALID_INPUT", "execution limits are mandatory")
        else:
            required = {
                "image",
                "cpus",
                "memory_mb",
                "pids",
                "disk_mb",
                "max_output_bytes",
                "network",
            }
            if (
                not isinstance(profile, dict)
                or not required <= profile.keys()
                or profile["image"] != s.image
                or profile["network"] != "none"
            ):
                raise RunnerError(
                    "INVALID_INPUT", "execution image/network must match the runner profile"
                )
            if any(
                type(profile[key]) not in (int, float)
                or not math.isfinite(profile[key])
                or profile[key] <= 0
                for key in ("cpus", "memory_mb", "pids", "disk_mb", "max_output_bytes")
            ):
                raise RunnerError(
                    "INVALID_INPUT", "execution resource limits must be positive finite values"
                )
            if any(
                type(profile[key]) is not int
                for key in ("memory_mb", "pids", "disk_mb", "max_output_bytes")
            ):
                raise RunnerError("INVALID_INPUT", "integer execution limits are required")
            data_bytes = cast(int, s.postgres_data_bytes) if services else 0
            if s.workspace_disk_bytes + data_bytes > profile["disk_mb"] * 1024 * 1024:
                raise RunnerError(
                    "LIMIT",
                    "dedicated workspace filesystem and PostgreSQL data exceed requested disk limit",
                )
            limits = {
                "cpus": min(s.cpu_limit, profile["cpus"]),
                "memory_bytes": min(s.memory_bytes, profile["memory_mb"] * 1024 * 1024),
                "pids": min(s.pids_limit, profile["pids"]),
                "max_output_bytes": min(s.max_output_bytes, profile["max_output_bytes"]),
            }
        identifier = hashlib.sha256(request.workspace_id.encode()).hexdigest()[:24]
        runtime: dict[str, Any] = {
            "workspace_id": request.workspace_id,
            "services": [item.model_dump() for item in services],
            "network": "v2c-net-" + identifier,
            "container": "v2c-pg-" + identifier,
            "limits": dict(limits),
            "environment": {},
        }
        if services:
            if limits["pids"] < 2 or limits["memory_bytes"] < 2:
                raise RunnerError(
                    "LIMIT", "execution resources cannot accommodate project and service"
                )
            service_limits = {
                "cpus": limits["cpus"] / 2,
                "memory_bytes": limits["memory_bytes"] // 2,
                "pids": limits["pids"] // 2,
            }
            runtime["service_limits"] = service_limits
            runtime["limits"]["cpus"] -= service_limits["cpus"]
            runtime["limits"]["memory_bytes"] -= service_limits["memory_bytes"]
            runtime["limits"]["pids"] -= service_limits["pids"]
            password = secrets.token_urlsafe(32)
            service = services[0]
            runtime["password"] = password
            runtime["environment"][service.env_var] = (
                f"postgresql://{service.user}:{password}@{service.name}:5432/{service.database}"
            )
        return runtime

    def prepare(self, request: PrepareRequest) -> dict[str, Any]:
        with self.lock:
            runtime = self._runtime_definition(request)
            with self.journal.connect() as db:
                existing = db.execute(
                    "SELECT * FROM workspace WHERE id=?", (request.workspace_id,)
                ).fetchone()
                if existing:
                    if existing["archive_hash"] != request.archive_sha256 or existing[
                        "manifest"
                    ] != json.dumps(request.input_manifest, sort_keys=True):
                        raise RunnerError("CONFLICT", "workspace already has different input")
                    if existing["state"] != "READY":
                        raise RunnerError(
                            "UNKNOWN", "workspace preparation is incomplete; recreate VM"
                        )
                    return self.authorize(
                        AuthorizationRequest(
                            workspace_id=request.workspace_id,
                            attempt_id=request.attempt_id,
                            generation=request.generation,
                        )
                    )
                # A runner VM accepts exactly one workspace throughout its life.
                if db.execute("SELECT 1 FROM workspace").fetchone():
                    raise RunnerError("CONFLICT", "runner VM already belongs to another workspace")
                db.execute(
                    "INSERT INTO workspace VALUES (?,?,?,?,?,?)",
                    (
                        request.workspace_id,
                        request.archive_sha256,
                        json.dumps(request.input_manifest, sort_keys=True),
                        request.generation,
                        request.attempt_id,
                        "PREPARING",
                    ),
                )
                db.execute(
                    "INSERT INTO runtime VALUES (?,?,?)",
                    (request.workspace_id, json.dumps(runtime, sort_keys=True), "PREPARING"),
                )
            try:
                archive = base64.b64decode(request.archive_b64, validate=True)
                if hashlib.sha256(archive).hexdigest() != request.archive_sha256:
                    raise RunnerError("INVALID_INPUT", "archive checksum differs")
                destination = self.workspaces / request.workspace_id
                destination.mkdir(mode=0o700)
                safe_extract(archive, destination, self.settings)
                if isinstance(self.backend, DockerBackend):
                    for current, dirs, files in os.walk(destination):
                        os.chown(current, self.settings.uid, self.settings.gid)
                        for name in files:
                            os.chown(Path(current) / name, self.settings.uid, self.settings.gid)
                if hasattr(self.backend, "prepare_runtime"):
                    self.backend.prepare_runtime(runtime)
                with self.journal.connect() as db:
                    db.execute(
                        "UPDATE workspace SET state=? WHERE id=?", ("READY", request.workspace_id)
                    )
                    db.execute(
                        "UPDATE runtime SET state=? WHERE workspace_id=?",
                        ("READY", request.workspace_id),
                    )
                return {
                    "workspace_id": request.workspace_id,
                    "state": "READY",
                    "archive_sha256": request.archive_sha256,
                }
            except Exception:
                with self.journal.connect() as db:
                    db.execute(
                        "UPDATE workspace SET state=? WHERE id=?", ("UNKNOWN", request.workspace_id)
                    )
                    db.execute(
                        "UPDATE runtime SET state=? WHERE workspace_id=?",
                        ("UNKNOWN", request.workspace_id),
                    )
                raise

    def authorize(self, request: AuthorizationRequest) -> dict[str, Any]:
        with self.lock, self.journal.connect() as db:
            row = self.journal.workspace(request.workspace_id)
            if request.generation < row["generation"] or (
                request.generation == row["generation"] and request.attempt_id != row["attempt_id"]
            ):
                raise RunnerError("STALE_GENERATION", "attempt authorization is stale")
            db.execute(
                "UPDATE workspace SET generation=?,attempt_id=? WHERE id=?",
                (request.generation, request.attempt_id, request.workspace_id),
            )
            return {
                "workspace_id": request.workspace_id,
                "state": "READY",
                "generation": request.generation,
            }

    def execute(self, request: CommandRequest) -> CommandResult:
        if request.timeout_seconds > self.settings.max_command_seconds:
            raise RunnerError("LIMIT", "command timeout exceeds runner profile")
        _path(request.cwd) if request.cwd != "." else None
        with self.lock:
            result, accepted = self.journal.admit(request)
            if not accepted:
                return result

            def execute_once() -> None:
                with self.lock:
                    current = self.journal.get(request.command_id)
                    if current.state == "CANCELLED":
                        return
                    # Authorization may have advanced between admission and start.
                    row = self.journal.workspace(request.workspace_id)
                    if (
                        row["generation"] != request.generation
                        or row["attempt_id"] != request.attempt_id
                    ):
                        self.journal.save(
                            current.model_copy(
                                update={
                                    "state": "CANCELLED",
                                    "stop_confirmed": True,
                                    "reason": "AUTHORIZATION_REVOKED_BEFORE_START",
                                }
                            )
                        )
                        return
                    self.journal.save(current.model_copy(update={"state": "RUNNING"}))
                try:
                    final = self.backend.run(request, self.workspaces / request.workspace_id)
                    self.journal.save(final)
                except Exception:
                    self.journal.save(
                        current.model_copy(
                            update={
                                "state": "UNKNOWN",
                                "reason": "EXECUTOR_FAILURE_REQUIRES_READBACK",
                            }
                        )
                    )

            thread = threading.Thread(target=execute_once, daemon=True)
            self.threads[request.command_id] = thread
            thread.start()
            return result

    def status(self, command_id: str) -> CommandResult:
        with self.lock:
            result = self.journal.get(command_id)
            if result.state not in TERMINAL and not (
                self.threads.get(command_id) and self.threads[command_id].is_alive()
            ):
                readback = self.backend.readback(self.journal.request(command_id))
                if readback is not None:
                    self.journal.save(readback)
                    result = readback
            return result

    def cancel(self, command_id: str) -> CommandResult:
        with self.lock:
            result = self.journal.get(command_id)
            if result.state in TERMINAL:
                return result
            request = self.journal.request(command_id)
            if result.state == "ACCEPTED":
                final = result.model_copy(update={"state": "CANCELLED", "stop_confirmed": True})
                self.journal.save(final)
                return final
        confirmed = self.backend.stop(request)
        with self.lock:
            final = self.journal.get(command_id)
            if final.state in TERMINAL:
                return final
            final = final.model_copy(
                update={
                    "state": "CANCELLED" if confirmed else "UNKNOWN",
                    "stop_confirmed": confirmed,
                    "reason": None if confirmed else "STOP_NOT_CONFIRMED",
                }
            )
            self.journal.save(final)
            return final

    def _idle(self, workspace_id: str) -> None:
        self.journal.workspace(workspace_id)
        with self.journal.connect() as db:
            rows = db.execute(
                "SELECT result_json FROM command WHERE workspace_id=?", (workspace_id,)
            ).fetchall()
        if any(
            CommandResult.model_validate_json(row["result_json"]).state not in TERMINAL
            for row in rows
        ):
            raise RunnerError("CONFLICT", "workspace has unconfirmed command outcomes")

    def checkpoint(self, workspace_id: str) -> bytes:
        with self.lock:
            self._idle(workspace_id)
            root = self.workspaces / workspace_id
            stream = io.BytesIO()
            count = size = 0
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
                for current, dirs, files in os.walk(root, followlinks=False):
                    if any((Path(current) / name).is_symlink() for name in dirs):
                        raise RunnerError("INVALID_INPUT", "checkpoint contains directory link")
                    for name in files:
                        path = Path(current) / name
                        if path.is_symlink() or not path.is_file():
                            raise RunnerError(
                                "INVALID_INPUT", "checkpoint contains nonregular file"
                            )
                        count += 1
                        size += path.stat().st_size
                        if (
                            count > self.settings.max_files
                            or size > self.settings.max_expanded_bytes
                        ):
                            raise RunnerError("LIMIT", "checkpoint exceeds limits")
                        z.write(path, path.relative_to(root).as_posix())
                        if stream.tell() > self.settings.max_archive_bytes:
                            raise RunnerError("LIMIT", "checkpoint compressed bytes exceed limit")
            if stream.tell() > self.settings.max_archive_bytes:
                raise RunnerError("LIMIT", "checkpoint compressed bytes exceed limit")
            return stream.getvalue()

    def destroy_workspace(self, workspace_id: str) -> dict[str, Any]:
        with self.lock:
            with self.journal.connect() as db:
                row = db.execute("SELECT * FROM workspace WHERE id=?", (workspace_id,)).fetchone()
                if row and row["state"] == "DESTROYED":
                    return {
                        "workspace_id": workspace_id,
                        "state": "DESTROYED",
                        "vm_destroyed": False,
                    }
            self._idle(workspace_id)
            with self.journal.connect() as db:
                runtime = db.execute(
                    "SELECT definition_json FROM runtime WHERE workspace_id=?", (workspace_id,)
                ).fetchone()
                command_ids = [
                    row["id"]
                    for row in db.execute(
                        "SELECT id FROM command WHERE workspace_id=?", (workspace_id,)
                    )
                ]
            if runtime and hasattr(self.backend, "destroy_runtime"):
                self.backend.destroy_runtime(json.loads(runtime["definition_json"]), command_ids)
            shutil.rmtree(self.workspaces / workspace_id)
            with self.journal.connect() as db:
                db.execute("UPDATE workspace SET state=? WHERE id=?", ("DESTROYED", workspace_id))
            # This confirms workspace removal only. VM destruction belongs to provider.
            return {"workspace_id": workspace_id, "state": "DESTROYED", "vm_destroyed": False}
