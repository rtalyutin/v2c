"""Docker authority held only inside a disposable runner VM."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast

from ..adapters.runner import CommandRequest, CommandResult, RunnerError
from .archive import _path
from .journal import Journal
from .settings import RunnerSettings


class ExecutionBackend(Protocol):
    def run(self, request: CommandRequest, workspace: Path) -> CommandResult: ...
    def readback(self, request: CommandRequest) -> CommandResult | None: ...
    def stop(self, request: CommandRequest) -> bool: ...


class DockerBackend:
    def __init__(self, settings: RunnerSettings, journal: Journal | None = None):
        self.settings = settings
        self.journal = journal
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        fs = os.statvfs(settings.data_dir)
        if fs.f_blocks * fs.f_frsize > settings.workspace_disk_bytes:
            raise RunnerError(
                "INVALID_CONFIG",
                "runner data must reside on a dedicated filesystem bounded by workspace_disk_bytes",
            )
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.RLock()

    def runtime(self, workspace_id: str) -> dict[str, Any] | None:
        journal = getattr(self, "journal", None)
        if journal is None:
            return None
        with journal.connect() as db:
            row = db.execute(
                "SELECT definition_json,state FROM runtime WHERE workspace_id=?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise RunnerError("UNKNOWN", "workspace runtime intent is missing")
        if row["state"] != "READY":
            raise RunnerError("UNKNOWN", "workspace services are not confirmed ready")
        return cast(dict[str, Any], json.loads(row["definition_json"]))

    def _docker(
        self,
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        timeout: float = 10,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [self.settings.docker_binary, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=environment,
        )

    def prepare_runtime(self, runtime: dict[str, Any]) -> None:
        if not runtime["services"]:
            return
        labels = ["--label", "v2c.workspace=" + runtime["workspace_id"]]
        network = runtime["network"]
        network_create = self._docker(["network", "create", "--internal", *labels, network])
        if network_create.returncode:
            inspected = self._docker(["network", "inspect", "--format", "{{json .}}", network])
            try:
                actual = json.loads(inspected.stdout)
                if (
                    inspected.returncode
                    or not actual.get("Internal")
                    or actual.get("Labels", {}).get("v2c.workspace") != runtime["workspace_id"]
                ):
                    raise ValueError("network owner differs")
            except (ValueError, TypeError, AttributeError) as error:
                raise RunnerError(
                    "UNKNOWN", "internal service network creation is unconfirmed"
                ) from error
        service = runtime["services"][0]
        s = self.settings
        environment = os.environ.copy()
        environment.update(
            {
                "POSTGRES_DB": service["database"],
                "POSTGRES_USER": service["user"],
                "POSTGRES_PASSWORD": runtime["password"],
                "PGDATA": "/pgdata",
            }
        )
        flags = [
            part
            for name in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "PGDATA")
            for part in ("--env", name)
        ]
        limits = runtime["service_limits"]
        args = [
            "run",
            "--detach",
            "--name",
            runtime["container"],
            "--pull=never",
            *labels,
            "--network=" + network,
            "--network-alias",
            service["name"],
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            str(limits["pids"]),
            "--memory",
            str(limits["memory_bytes"]),
            "--memory-swap",
            str(limits["memory_bytes"]),
            "--cpus",
            str(limits["cpus"]),
            "--user",
            f"{s.postgres_uid}:{s.postgres_gid}",
            "--tmpfs",
            f"/pgdata:rw,nosuid,nodev,noexec,size={s.postgres_data_bytes},uid={s.postgres_uid},gid={s.postgres_gid}",
            "--tmpfs",
            f"/var/run/postgresql:rw,nosuid,nodev,noexec,size={s.tmpfs_bytes},uid={s.postgres_uid},gid={s.postgres_gid}",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={s.tmpfs_bytes}",
            "--log-driver=none",
            *flags,
            s.postgres_image,
        ]
        created = self._docker(args, environment=environment)
        if created.returncode:
            inspected = self._docker(
                ["inspect", "--format", "{{json .Config.Labels}}", runtime["container"]]
            )
            try:
                actual = json.loads(inspected.stdout)
                if inspected.returncode or actual.get("v2c.workspace") != runtime["workspace_id"]:
                    raise ValueError("service owner differs")
            except (ValueError, TypeError, AttributeError) as error:
                raise RunnerError(
                    "UNKNOWN", "PostgreSQL container creation is unconfirmed"
                ) from error
        deadline = time.monotonic() + cast(int, s.postgres_ready_seconds)
        while time.monotonic() < deadline:
            ready = self._docker(
                [
                    "exec",
                    runtime["container"],
                    "pg_isready",
                    "-q",
                    "-U",
                    service["user"],
                    "-d",
                    service["database"],
                ],
                timeout=max(0.1, min(5, deadline - time.monotonic())),
            )
            if ready.returncode == 0:
                return
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        raise RunnerError("UNAVAILABLE", "PostgreSQL readiness deadline exceeded")

    def destroy_runtime(self, runtime: dict[str, Any], command_ids: list[str]) -> None:
        # Deterministic names were persisted before every Docker call. Removal
        # acceptance alone does not prove cleanup; confirm absence by inspect.
        names = [
            "v2c-" + hashlib.sha256(identifier.encode()).hexdigest()[:32]
            for identifier in command_ids
        ]
        if runtime["services"]:
            names.append(runtime["container"])
        for name in names:
            removed = self._docker(["rm", "--force", name])
            probe = self._docker(["inspect", "--format", "{{.Id}}", name])
            if probe.returncode == 0 or (removed.returncode and not self._daemon_ready()):
                raise RunnerError("UNKNOWN", "container removal is unconfirmed")
        if runtime["services"]:
            removed = self._docker(["network", "rm", runtime["network"]])
            probe = self._docker(["network", "inspect", "--format", "{{.Id}}", runtime["network"]])
            if probe.returncode == 0 or (removed.returncode and not self._daemon_ready()):
                raise RunnerError("UNKNOWN", "internal network removal is unconfirmed")

    def _daemon_ready(self) -> bool:
        return self._docker(["info", "--format", "{{.ServerVersion}}"]).returncode == 0

    def _name(self, request: CommandRequest) -> str:
        return "v2c-" + hashlib.sha256(request.command_id.encode()).hexdigest()[:32]

    def argv(self, request: CommandRequest, workspace: Path) -> list[str]:
        p = _path(request.cwd) if request.cwd != "." else PurePosixPath(".")
        s = self.settings
        runtime = self.runtime(request.workspace_id)
        limits = (
            runtime["limits"]
            if runtime
            else {
                "cpus": s.cpu_limit,
                "memory_bytes": s.memory_bytes,
                "pids": s.pids_limit,
                "max_output_bytes": s.max_output_bytes,
            }
        )
        network = runtime["network"] if runtime and runtime["services"] else "none"
        env_flags = [
            part for name in (runtime or {}).get("environment", {}) for part in ("--env", name)
        ]
        return [
            s.docker_binary,
            "run",
            "--name",
            self._name(request),
            "--pull=never",
            "--network=" + network,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            str(limits["pids"]),
            "--memory",
            str(limits["memory_bytes"]),
            "--memory-swap",
            str(limits["memory_bytes"]),
            "--cpus",
            str(limits["cpus"]),
            "--user",
            f"{s.uid}:{s.gid}",
            *env_flags,
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={s.tmpfs_bytes}",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--workdir",
            "/workspace" + (("/" + str(p)) if str(p) != "." else ""),
            "--log-driver=none",
            "--entrypoint",
            request.argv[0],
            self.settings.image,
            *request.argv[1:],
        ]

    def run(self, request: CommandRequest, workspace: Path) -> CommandResult:
        runtime = self.runtime(request.workspace_id)
        environment = os.environ.copy()
        environment.update((runtime or {}).get("environment", {}))
        process = subprocess.Popen(
            self.argv(request, workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=environment,
        )
        with self._lock:
            self._processes[request.command_id] = process
        output = bytearray()
        truncated = False
        deadline = time.monotonic() + request.timeout_seconds
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            self.stop(request)

        timer = threading.Timer(request.timeout_seconds, expire)
        timer.start()
        try:
            assert process.stdout is not None
            while chunk := process.stdout.read(65536):
                remaining = (
                    (runtime or {})
                    .get("limits", {})
                    .get("max_output_bytes", self.settings.max_output_bytes)
                ) - len(output)
                output.extend(chunk[: max(remaining, 0)])
                if len(chunk) > remaining:
                    truncated = True
            process.wait(timeout=max(1.0, deadline - time.monotonic() + 2))
            readback = self.readback(request)
            if readback is None or not readback.stop_confirmed:
                return CommandResult(
                    command_id=request.command_id,
                    request_hash=request.content_hash(),
                    state="UNKNOWN",
                    reason="CONTAINER_OUTCOME_UNKNOWN",
                    output=output.decode(errors="replace"),
                    output_truncated=truncated,
                )
            state: Literal["SUCCEEDED", "FAILED"] = (
                "FAILED"
                if timed_out.is_set()
                else ("SUCCEEDED" if readback.exit_code == 0 else "FAILED")
            )
            return CommandResult(
                command_id=request.command_id,
                request_hash=request.content_hash(),
                state=state,
                exit_code=readback.exit_code,
                output=output.decode(errors="replace"),
                output_truncated=truncated,
                stop_confirmed=True,
                reason="COMMAND_TIMEOUT" if timed_out.is_set() else None,
            )
        finally:
            timer.cancel()
            with self._lock:
                self._processes.pop(request.command_id, None)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()

    def readback(self, request: CommandRequest) -> CommandResult | None:
        proc = subprocess.run(
            [
                self.settings.docker_binary,
                "inspect",
                "--format",
                "{{json .State}}",
                self._name(request),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if proc.returncode != 0:
            return None
        try:
            value = json.loads(proc.stdout)
            if value.get("Running"):
                return CommandResult(
                    command_id=request.command_id,
                    request_hash=request.content_hash(),
                    state="RUNNING",
                )
            if value.get("Status") not in ("exited", "dead"):
                return None
            code = value["ExitCode"]
            if type(code) is not int:
                return None
            return CommandResult(
                command_id=request.command_id,
                request_hash=request.content_hash(),
                state="SUCCEEDED" if code == 0 else "FAILED",
                exit_code=code,
                stop_confirmed=True,
                output_truncated=True,
                reason="ATTACH_OUTPUT_LOST_AFTER_RESTART",
            )
        except (ValueError, KeyError, TypeError):
            return None

    def stop(self, request: CommandRequest) -> bool:
        name = self._name(request)
        subprocess.run(
            [self.settings.docker_binary, "kill", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        state = self.readback(request)
        if state is not None and state.stop_confirmed:
            return True
        # If Docker never created the container, a delayed docker run client may
        # still create it. Kill the local client and do not assert remote stop.
        with self._lock:
            process = self._processes.get(request.command_id)
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        state = self.readback(request)
        return bool(state and state.stop_confirmed)
