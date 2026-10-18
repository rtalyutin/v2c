"""Disposable-VM runner service; never deploy on the control VM.

A durable SQLite journal admits each command before Docker sees it. Process
restart makes nonterminal entries UNKNOWN, never eligible for automatic replay.
The VM firewall must additionally deny control/metadata/private networks. Docker
has no project network by default. This service holds Docker authority only on
its own disposable VM, never exposes the socket to project containers.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import math
from pathlib import Path, PurePosixPath
import secrets
import shutil
import sqlite3
import ssl
import subprocess
import threading
import time
import zipfile
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .adapters.runner import AuthorizationRequest, CommandRequest, CommandResult, PrepareRequest, PostgresService, RunnerError

TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELLED'}


class RunnerSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    data_dir: Path
    bearer_secret: str = Field(min_length=16, repr=False)
    image: str = Field(pattern=r'^.+@sha256:[a-f0-9]{64}$')
    cpu_limit: float = Field(gt=0, allow_inf_nan=False)
    memory_bytes: int = Field(gt=0)
    pids_limit: int = Field(gt=0)
    tmpfs_bytes: int = Field(gt=0)
    workspace_disk_bytes: int = Field(gt=0)
    max_archive_bytes: int = Field(gt=0)
    max_expanded_bytes: int = Field(gt=0)
    max_files: int = Field(gt=0)
    max_command_seconds: int = Field(gt=0)
    max_output_bytes: int = Field(gt=0)
    uid: int = Field(ge=1)
    gid: int = Field(ge=1)
    docker_binary: str = '/usr/bin/docker'
    postgres_image: str | None = None
    postgres_data_bytes: int | None = Field(default=None, gt=0)
    postgres_ready_seconds: int | None = Field(default=None, gt=0)
    postgres_uid: int | None = Field(default=None, ge=1)
    postgres_gid: int | None = Field(default=None, ge=1)

    @field_validator('postgres_image')
    @classmethod
    def pinned_postgres(cls, value: str | None) -> str | None:
        if not value: return None
        import re
        if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}', value):
            raise ValueError('PostgreSQL image must be pinned to a digest')
        return value

    @model_validator(mode='after')
    def postgres_configuration(self):
        if self.postgres_image and any(getattr(self, field) is None for field in
                ('postgres_data_bytes', 'postgres_ready_seconds', 'postgres_uid', 'postgres_gid')):
            raise ValueError('PostgreSQL requires explicit data/readiness/nonroot UID/GID limits')
        return self

    @classmethod
    def from_env(cls) -> 'RunnerSettings':
        config_file = os.environ.get('V2C_RUNNER_SETTINGS_FILE')
        if config_file:
            return cls.model_validate_json(Path(config_file).read_text())
        values = {name: os.environ['V2C_RUNNER_' + name.upper()]
                  for name in cls.model_fields if 'V2C_RUNNER_' + name.upper() in os.environ}
        return cls.model_validate(values)


def _path(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if '\\' in name or '\0' in name or p.is_absolute() or '..' in p.parts or not p.parts:
        raise RunnerError('INVALID_INPUT', 'unsafe archive/workspace path')
    return p


def safe_extract(archive: bytes, destination: Path, settings: RunnerSettings) -> None:
    if len(archive) > settings.max_archive_bytes:
        raise RunnerError('LIMIT', 'archive exceeds compressed limit')
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            infos = z.infolist()
            if len(infos) > settings.max_files or sum(i.file_size for i in infos) > settings.max_expanded_bytes:
                raise RunnerError('LIMIT', 'archive exceeds expanded limits')
            seen: set[str] = set()
            for info in infos:
                p = _path(info.filename)
                if str(p) in seen: raise RunnerError('INVALID_INPUT', 'duplicate archive path')
                seen.add(str(p))
                mode = info.external_attr >> 16
                # Reject links, devices and all non-file/directory Unix modes.
                kind = mode & 0o170000
                if kind not in (0, 0o100000, 0o040000):
                    raise RunnerError('INVALID_INPUT', 'archive contains a nonregular entry')
                target = destination.joinpath(*p.parts)
                if info.is_dir(): target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as source, target.open('xb') as output:
                        copied = 0
                        while chunk := source.read(min(65536, settings.max_expanded_bytes + 1)):
                            copied += len(chunk)
                            if copied > info.file_size: raise RunnerError('LIMIT', 'archive size mismatch')
                            output.write(chunk)
                    target.chmod(0o755 if mode & 0o111 else 0o644)
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        raise RunnerError('INVALID_INPUT', 'invalid project archive') from error


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS workspace (
                    id TEXT PRIMARY KEY, archive_hash TEXT NOT NULL, manifest TEXT NOT NULL,
                    generation INTEGER NOT NULL, attempt_id TEXT NOT NULL, state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime (
                    workspace_id TEXT PRIMARY KEY, definition_json TEXT NOT NULL, state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS command (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL, result_json TEXT NOT NULL
                );
            ''')
            # No execution replay after an uncertain cut, including accepted-before-start.
            for row in db.execute('SELECT id,result_json FROM command').fetchall():
                result = CommandResult.model_validate_json(row['result_json'])
                if result.state not in TERMINAL:
                    result = result.model_copy(update={'state': 'UNKNOWN', 'reason': 'RUNNER_RESTART_REQUIRES_READBACK'})
                    db.execute('UPDATE command SET result_json=? WHERE id=?', (result.model_dump_json(), row['id']))

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db

    def get(self, command_id: str) -> CommandResult:
        with self.connect() as db:
            row = db.execute('SELECT result_json FROM command WHERE id=?', (command_id,)).fetchone()
        if not row: raise RunnerError('NOT_FOUND', 'command journal has no record; absence is not proof of nonexecution')
        return CommandResult.model_validate_json(row['result_json'])

    def save(self, result: CommandResult) -> None:
        with self.lock, self.connect() as db:
            # Cancel and runner-readback terminal results cannot be overwritten by
            # an older attachment thread that finishes later.
            row = db.execute('SELECT result_json FROM command WHERE id=?', (result.command_id,)).fetchone()
            previous = CommandResult.model_validate_json(row['result_json']) if row else None
            if previous and previous.state in TERMINAL: return
            db.execute('UPDATE command SET result_json=? WHERE id=?', (result.model_dump_json(), result.command_id))

    def request(self, command_id: str) -> CommandRequest:
        with self.connect() as db:
            row = db.execute('SELECT request_json FROM command WHERE id=?', (command_id,)).fetchone()
        if not row: raise RunnerError('NOT_FOUND', 'unknown command')
        return CommandRequest.model_validate_json(row['request_json'])

    def workspace(self, workspace_id: str) -> sqlite3.Row:
        with self.connect() as db:
            row = db.execute('SELECT * FROM workspace WHERE id=?', (workspace_id,)).fetchone()
        if not row or row['state'] != 'READY': raise RunnerError('NOT_FOUND', 'workspace is not ready')
        return row

    def admit(self, request: CommandRequest) -> tuple[CommandResult, bool]:
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT request_hash,result_json FROM command WHERE id=?', (request.command_id,)).fetchone()
            if existing:
                if existing['request_hash'] != request.content_hash(): raise RunnerError('CONFLICT', 'command ID has a different request hash')
                return CommandResult.model_validate_json(existing['result_json']), False
            row = db.execute('SELECT * FROM workspace WHERE id=?', (request.workspace_id,)).fetchone()
            if not row or row['state'] != 'READY': raise RunnerError('NOT_FOUND', 'workspace is not ready')
            if row['attempt_id'] != request.attempt_id or row['generation'] != request.generation:
                raise RunnerError('STALE_GENERATION', 'attempt authorization is stale')
            for other in db.execute('SELECT result_json FROM command WHERE workspace_id=?', (request.workspace_id,)):
                if CommandResult.model_validate_json(other['result_json']).state not in TERMINAL:
                    raise RunnerError('CONFLICT', 'previous command has not confirmed termination')
            result = CommandResult(command_id=request.command_id, request_hash=request.content_hash(), state='ACCEPTED')
            db.execute('INSERT INTO command VALUES (?,?,?,?,?)', (request.command_id, request.workspace_id, request.content_hash(), request.model_dump_json(), result.model_dump_json()))
            return result, True


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
            raise RunnerError('INVALID_CONFIG', 'runner data must reside on a dedicated filesystem bounded by workspace_disk_bytes')
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.RLock()

    def runtime(self, workspace_id: str) -> dict[str, Any] | None:
        journal = getattr(self, 'journal', None)
        if journal is None: return None
        with journal.connect() as db:
            row = db.execute('SELECT definition_json,state FROM runtime WHERE workspace_id=?', (workspace_id,)).fetchone()
        if row is None: raise RunnerError('UNKNOWN', 'workspace runtime intent is missing')
        if row['state'] != 'READY': raise RunnerError('UNKNOWN', 'workspace services are not confirmed ready')
        return json.loads(row['definition_json'])

    def _docker(self, arguments: list[str], *, environment: dict[str, str] | None = None,
                timeout: float = 10) -> subprocess.CompletedProcess:
        return subprocess.run([self.settings.docker_binary, *arguments], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout, env=environment)

    def prepare_runtime(self, runtime: dict[str, Any]) -> None:
        if not runtime['services']: return
        labels = ['--label', 'v2c.workspace=' + runtime['workspace_id']]
        network = runtime['network']
        network_create = self._docker(['network', 'create', '--internal', *labels, network])
        if network_create.returncode:
            inspected = self._docker(['network', 'inspect', '--format', '{{json .}}', network])
            try:
                actual = json.loads(inspected.stdout)
                if inspected.returncode or not actual.get('Internal') or actual.get('Labels', {}).get('v2c.workspace') != runtime['workspace_id']:
                    raise ValueError('network owner differs')
            except (ValueError, TypeError, AttributeError) as error:
                raise RunnerError('UNKNOWN', 'internal service network creation is unconfirmed') from error
        service = runtime['services'][0]
        s = self.settings
        environment = os.environ.copy()
        environment.update({'POSTGRES_DB': service['database'], 'POSTGRES_USER': service['user'],
                            'POSTGRES_PASSWORD': runtime['password'], 'PGDATA': '/pgdata'})
        flags = [part for name in ('POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'PGDATA') for part in ('--env', name)]
        limits = runtime['service_limits']
        args = ['run', '--detach', '--name', runtime['container'], '--pull=never', *labels,
                '--network=' + network, '--network-alias', service['name'], '--read-only', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', '--pids-limit', str(limits['pids']),
                '--memory', str(limits['memory_bytes']), '--memory-swap', str(limits['memory_bytes']),
                '--cpus', str(limits['cpus']), '--user', f'{s.postgres_uid}:{s.postgres_gid}',
                '--tmpfs', f'/pgdata:rw,nosuid,nodev,noexec,size={s.postgres_data_bytes},uid={s.postgres_uid},gid={s.postgres_gid}',
                '--tmpfs', f'/var/run/postgresql:rw,nosuid,nodev,noexec,size={s.tmpfs_bytes},uid={s.postgres_uid},gid={s.postgres_gid}',
                '--tmpfs', f'/tmp:rw,nosuid,nodev,noexec,size={s.tmpfs_bytes}',
                '--log-driver=none', *flags, s.postgres_image]
        created = self._docker(args, environment=environment)
        if created.returncode:
            inspected = self._docker(['inspect', '--format', '{{json .Config.Labels}}', runtime['container']])
            try:
                actual = json.loads(inspected.stdout)
                if inspected.returncode or actual.get('v2c.workspace') != runtime['workspace_id']:
                    raise ValueError('service owner differs')
            except (ValueError, TypeError, AttributeError) as error:
                raise RunnerError('UNKNOWN', 'PostgreSQL container creation is unconfirmed') from error
        deadline = time.monotonic() + s.postgres_ready_seconds
        while time.monotonic() < deadline:
            ready = self._docker(['exec', runtime['container'], 'pg_isready', '-q', '-U', service['user'], '-d', service['database']],
                                 timeout=max(0.1, min(5, deadline - time.monotonic())))
            if ready.returncode == 0: return
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        raise RunnerError('UNAVAILABLE', 'PostgreSQL readiness deadline exceeded')

    def destroy_runtime(self, runtime: dict[str, Any], command_ids: list[str]) -> None:
        # Deterministic names were persisted before every Docker call. Removal
        # acceptance alone does not prove cleanup; confirm absence by inspect.
        names = ['v2c-' + hashlib.sha256(identifier.encode()).hexdigest()[:32] for identifier in command_ids]
        if runtime['services']: names.append(runtime['container'])
        for name in names:
            removed = self._docker(['rm', '--force', name])
            probe = self._docker(['inspect', '--format', '{{.Id}}', name])
            if probe.returncode == 0 or (removed.returncode and not self._daemon_ready()):
                raise RunnerError('UNKNOWN', 'container removal is unconfirmed')
        if runtime['services']:
            removed = self._docker(['network', 'rm', runtime['network']])
            probe = self._docker(['network', 'inspect', '--format', '{{.Id}}', runtime['network']])
            if probe.returncode == 0 or (removed.returncode and not self._daemon_ready()):
                raise RunnerError('UNKNOWN', 'internal network removal is unconfirmed')

    def _daemon_ready(self) -> bool:
        return self._docker(['info', '--format', '{{.ServerVersion}}']).returncode == 0

    def _name(self, request: CommandRequest) -> str:
        return 'v2c-' + hashlib.sha256(request.command_id.encode()).hexdigest()[:32]

    def argv(self, request: CommandRequest, workspace: Path) -> list[str]:
        p = _path(request.cwd) if request.cwd != '.' else PurePosixPath('.')
        s = self.settings
        runtime = self.runtime(request.workspace_id)
        limits = runtime['limits'] if runtime else {'cpus': s.cpu_limit, 'memory_bytes': s.memory_bytes,
                'pids': s.pids_limit, 'max_output_bytes': s.max_output_bytes}
        network = runtime['network'] if runtime and runtime['services'] else 'none'
        env_flags = [part for name in (runtime or {}).get('environment', {}) for part in ('--env', name)]
        return [s.docker_binary, 'run', '--name', self._name(request), '--pull=never',
                '--network=' + network, '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--pids-limit', str(limits['pids']), '--memory', str(limits['memory_bytes']), '--memory-swap', str(limits['memory_bytes']),
                '--cpus', str(limits['cpus']), '--user', f'{s.uid}:{s.gid}', *env_flags,
                '--tmpfs', f'/tmp:rw,nosuid,nodev,noexec,size={s.tmpfs_bytes}',
                '--mount', f'type=bind,src={workspace},dst=/workspace',
                '--workdir', '/workspace' + (('/' + str(p)) if str(p) != '.' else ''),
                '--log-driver=none', '--entrypoint', request.argv[0], self.settings.image, *request.argv[1:]]

    def run(self, request: CommandRequest, workspace: Path) -> CommandResult:
        runtime = self.runtime(request.workspace_id)
        environment = os.environ.copy()
        environment.update((runtime or {}).get('environment', {}))
        process = subprocess.Popen(self.argv(request, workspace), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True, env=environment)
        with self._lock: self._processes[request.command_id] = process
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
                remaining = ((runtime or {}).get('limits', {}).get('max_output_bytes', self.settings.max_output_bytes)) - len(output)
                output.extend(chunk[:max(remaining, 0)])
                if len(chunk) > remaining: truncated = True
            code = process.wait(timeout=max(1.0, deadline - time.monotonic() + 2))
            readback = self.readback(request)
            if readback is None or not readback.stop_confirmed:
                return CommandResult(command_id=request.command_id, request_hash=request.content_hash(), state='UNKNOWN',
                                     reason='CONTAINER_OUTCOME_UNKNOWN', output=output.decode(errors='replace'), output_truncated=truncated)
            state = 'FAILED' if timed_out.is_set() else ('SUCCEEDED' if readback.exit_code == 0 else 'FAILED')
            return CommandResult(command_id=request.command_id, request_hash=request.content_hash(), state=state,
                                 exit_code=readback.exit_code, output=output.decode(errors='replace'), output_truncated=truncated,
                                 stop_confirmed=True, reason='COMMAND_TIMEOUT' if timed_out.is_set() else None)
        finally:
            timer.cancel()
            with self._lock: self._processes.pop(request.command_id, None)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout: process.stdout.close()

    def readback(self, request: CommandRequest) -> CommandResult | None:
        proc = subprocess.run([self.settings.docker_binary, 'inspect', '--format', '{{json .State}}', self._name(request)],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        if proc.returncode != 0: return None
        try:
            value = json.loads(proc.stdout)
            if value.get('Running'):
                return CommandResult(command_id=request.command_id, request_hash=request.content_hash(), state='RUNNING')
            if value.get('Status') not in ('exited', 'dead'):
                return None
            code = value['ExitCode']
            if type(code) is not int: return None
            return CommandResult(command_id=request.command_id, request_hash=request.content_hash(),
                                 state='SUCCEEDED' if code == 0 else 'FAILED', exit_code=code, stop_confirmed=True,
                                 output_truncated=True, reason='ATTACH_OUTPUT_LOST_AFTER_RESTART')
        except (ValueError, KeyError, TypeError): return None

    def stop(self, request: CommandRequest) -> bool:
        name = self._name(request)
        subprocess.run([self.settings.docker_binary, 'kill', name], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=10)
        state = self.readback(request)
        if state is not None and state.stop_confirmed: return True
        # If Docker never created the container, a delayed docker run client may
        # still create it. Kill the local client and do not assert remote stop.
        with self._lock: process = self._processes.get(request.command_id)
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        state = self.readback(request)
        return bool(state and state.stop_confirmed)


class RunnerService:
    def __init__(self, settings: RunnerSettings, backend: ExecutionBackend | None = None):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces = settings.data_dir / 'workspaces'
        self.workspaces.mkdir(exist_ok=True)
        self.journal = Journal(settings.data_dir / 'journal.sqlite3')
        self.backend = backend or DockerBackend(settings, self.journal)
        self.lock = self.journal.lock
        self.threads: dict[str, threading.Thread] = {}

    def _runtime_definition(self, request: PrepareRequest) -> dict[str, Any]:
        manifest = request.input_manifest
        recipe = manifest.get('environment_recipe', {})
        raw_services = recipe.get('services', []) if isinstance(recipe, dict) else None
        try:
            if not isinstance(raw_services, list) or len(raw_services) > 1: raise ValueError('at most one service')
            services = [PostgresService.model_validate(item) for item in raw_services]
        except (ValueError, TypeError, ValidationError) as error:
            raise RunnerError('UNSUPPORTED', 'only one bounded PostgreSQL test service is supported') from error
        if services:
            if not self.settings.postgres_image:
                raise RunnerError('UNSUPPORTED', 'operator PostgreSQL image/profile is not configured')
            if recipe.get('state_strategy') != 'rebuild_from_fixtures' or not recipe.get('restore') or not recipe.get('recovery_checks'):
                raise RunnerError('INVALID_INPUT', 'PostgreSQL requires fixture restoration and recovery checks')
            if not hasattr(self.backend, 'prepare_runtime'):
                raise RunnerError('UNSUPPORTED', 'execution backend does not implement test services')
        profile = manifest.get('execution_limits')
        s = self.settings
        limits = {'cpus': s.cpu_limit, 'memory_bytes': s.memory_bytes, 'pids': s.pids_limit, 'max_output_bytes': s.max_output_bytes}
        if profile is None:
            if isinstance(self.backend, DockerBackend):
                raise RunnerError('INVALID_INPUT', 'execution limits are mandatory')
        else:
            required = {'image', 'cpus', 'memory_mb', 'pids', 'disk_mb', 'max_output_bytes', 'network'}
            if not isinstance(profile, dict) or not required <= profile.keys() or profile['image'] != s.image or profile['network'] != 'none':
                raise RunnerError('INVALID_INPUT', 'execution image/network must match the runner profile')
            if any(type(profile[key]) not in (int, float) or not math.isfinite(profile[key]) or profile[key] <= 0
                   for key in ('cpus', 'memory_mb', 'pids', 'disk_mb', 'max_output_bytes')):
                raise RunnerError('INVALID_INPUT', 'execution resource limits must be positive finite values')
            if any(type(profile[key]) is not int for key in ('memory_mb', 'pids', 'disk_mb', 'max_output_bytes')):
                raise RunnerError('INVALID_INPUT', 'integer execution limits are required')
            data_bytes = s.postgres_data_bytes if services else 0
            if s.workspace_disk_bytes + data_bytes > profile['disk_mb'] * 1024 * 1024:
                raise RunnerError('LIMIT', 'dedicated workspace filesystem and PostgreSQL data exceed requested disk limit')
            limits = {'cpus': min(s.cpu_limit, profile['cpus']), 'memory_bytes': min(s.memory_bytes, profile['memory_mb'] * 1024 * 1024),
                      'pids': min(s.pids_limit, profile['pids']), 'max_output_bytes': min(s.max_output_bytes, profile['max_output_bytes'])}
        identifier = hashlib.sha256(request.workspace_id.encode()).hexdigest()[:24]
        runtime = {'workspace_id': request.workspace_id, 'services': [item.model_dump() for item in services],
                   'network': 'v2c-net-' + identifier, 'container': 'v2c-pg-' + identifier,
                   'limits': dict(limits), 'environment': {}}
        if services:
            if limits['pids'] < 2 or limits['memory_bytes'] < 2:
                raise RunnerError('LIMIT', 'execution resources cannot accommodate project and service')
            service_limits = {'cpus': limits['cpus'] / 2, 'memory_bytes': limits['memory_bytes'] // 2, 'pids': limits['pids'] // 2}
            runtime['service_limits'] = service_limits
            runtime['limits']['cpus'] -= service_limits['cpus']
            runtime['limits']['memory_bytes'] -= service_limits['memory_bytes']
            runtime['limits']['pids'] -= service_limits['pids']
            password = secrets.token_urlsafe(32)
            service = services[0]
            runtime['password'] = password
            runtime['environment'][service.env_var] = f'postgresql://{service.user}:{password}@{service.name}:5432/{service.database}'
        return runtime

    def prepare(self, request: PrepareRequest) -> dict[str, Any]:
        with self.lock:
            runtime = self._runtime_definition(request)
            with self.journal.connect() as db:
                existing = db.execute('SELECT * FROM workspace WHERE id=?', (request.workspace_id,)).fetchone()
                if existing:
                    if existing['archive_hash'] != request.archive_sha256 or existing['manifest'] != json.dumps(request.input_manifest, sort_keys=True):
                        raise RunnerError('CONFLICT', 'workspace already has different input')
                    if existing['state'] != 'READY': raise RunnerError('UNKNOWN', 'workspace preparation is incomplete; recreate VM')
                    return self.authorize(AuthorizationRequest(workspace_id=request.workspace_id, attempt_id=request.attempt_id, generation=request.generation))
                # A runner VM accepts exactly one workspace throughout its life.
                if db.execute('SELECT 1 FROM workspace').fetchone(): raise RunnerError('CONFLICT', 'runner VM already belongs to another workspace')
                db.execute('INSERT INTO workspace VALUES (?,?,?,?,?,?)', (request.workspace_id, request.archive_sha256,
                    json.dumps(request.input_manifest, sort_keys=True), request.generation, request.attempt_id, 'PREPARING'))
                db.execute('INSERT INTO runtime VALUES (?,?,?)', (request.workspace_id, json.dumps(runtime, sort_keys=True), 'PREPARING'))
            try:
                archive = base64.b64decode(request.archive_b64, validate=True)
                if hashlib.sha256(archive).hexdigest() != request.archive_sha256: raise RunnerError('INVALID_INPUT', 'archive checksum differs')
                destination = self.workspaces / request.workspace_id
                destination.mkdir(mode=0o700)
                safe_extract(archive, destination, self.settings)
                if isinstance(self.backend, DockerBackend):
                    for current, dirs, files in os.walk(destination):
                        os.chown(current, self.settings.uid, self.settings.gid)
                        for name in files: os.chown(Path(current) / name, self.settings.uid, self.settings.gid)
                if hasattr(self.backend, 'prepare_runtime'): self.backend.prepare_runtime(runtime)
                with self.journal.connect() as db:
                    db.execute('UPDATE workspace SET state=? WHERE id=?', ('READY', request.workspace_id))
                    db.execute('UPDATE runtime SET state=? WHERE workspace_id=?', ('READY', request.workspace_id))
                return {'workspace_id': request.workspace_id, 'state': 'READY', 'archive_sha256': request.archive_sha256}
            except Exception:
                with self.journal.connect() as db:
                    db.execute('UPDATE workspace SET state=? WHERE id=?', ('UNKNOWN', request.workspace_id))
                    db.execute('UPDATE runtime SET state=? WHERE workspace_id=?', ('UNKNOWN', request.workspace_id))
                raise

    def authorize(self, request: AuthorizationRequest) -> dict[str, Any]:
        with self.lock, self.journal.connect() as db:
            row = self.journal.workspace(request.workspace_id)
            if request.generation < row['generation'] or (request.generation == row['generation'] and request.attempt_id != row['attempt_id']):
                raise RunnerError('STALE_GENERATION', 'attempt authorization is stale')
            db.execute('UPDATE workspace SET generation=?,attempt_id=? WHERE id=?', (request.generation, request.attempt_id, request.workspace_id))
            return {'workspace_id': request.workspace_id, 'state': 'READY', 'generation': request.generation}

    def execute(self, request: CommandRequest) -> CommandResult:
        if request.timeout_seconds > self.settings.max_command_seconds: raise RunnerError('LIMIT', 'command timeout exceeds runner profile')
        _path(request.cwd) if request.cwd != '.' else None
        with self.lock:
            result, accepted = self.journal.admit(request)
            if not accepted: return result
            def execute_once() -> None:
                with self.lock:
                    current = self.journal.get(request.command_id)
                    if current.state == 'CANCELLED': return
                    # Authorization may have advanced between admission and start.
                    row = self.journal.workspace(request.workspace_id)
                    if row['generation'] != request.generation or row['attempt_id'] != request.attempt_id:
                        self.journal.save(current.model_copy(update={'state': 'CANCELLED', 'stop_confirmed': True, 'reason': 'AUTHORIZATION_REVOKED_BEFORE_START'}))
                        return
                    self.journal.save(current.model_copy(update={'state': 'RUNNING'}))
                try:
                    final = self.backend.run(request, self.workspaces / request.workspace_id)
                    self.journal.save(final)
                except Exception:
                    self.journal.save(current.model_copy(update={'state': 'UNKNOWN', 'reason': 'EXECUTOR_FAILURE_REQUIRES_READBACK'}))
            thread = threading.Thread(target=execute_once, daemon=True)
            self.threads[request.command_id] = thread
            thread.start()
            return result

    def status(self, command_id: str) -> CommandResult:
        with self.lock:
            result = self.journal.get(command_id)
            if result.state not in TERMINAL and not (self.threads.get(command_id) and self.threads[command_id].is_alive()):
                readback = self.backend.readback(self.journal.request(command_id))
                if readback is not None:
                    self.journal.save(readback)
                    result = readback
            return result

    def cancel(self, command_id: str) -> CommandResult:
        with self.lock:
            result = self.journal.get(command_id)
            if result.state in TERMINAL: return result
            request = self.journal.request(command_id)
            if result.state == 'ACCEPTED':
                final = result.model_copy(update={'state': 'CANCELLED', 'stop_confirmed': True})
                self.journal.save(final)
                return final
        confirmed = self.backend.stop(request)
        with self.lock:
            final = self.journal.get(command_id)
            if final.state in TERMINAL: return final
            final = final.model_copy(update={'state': 'CANCELLED' if confirmed else 'UNKNOWN', 'stop_confirmed': confirmed,
                                            'reason': None if confirmed else 'STOP_NOT_CONFIRMED'})
            self.journal.save(final)
            return final

    def _idle(self, workspace_id: str) -> None:
        self.journal.workspace(workspace_id)
        with self.journal.connect() as db:
            rows = db.execute('SELECT result_json FROM command WHERE workspace_id=?', (workspace_id,)).fetchall()
        if any(CommandResult.model_validate_json(row['result_json']).state not in TERMINAL for row in rows):
            raise RunnerError('CONFLICT', 'workspace has unconfirmed command outcomes')

    def checkpoint(self, workspace_id: str) -> bytes:
        with self.lock:
            self._idle(workspace_id)
            root = self.workspaces / workspace_id
            stream = io.BytesIO()
            count = size = 0
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
                for current, dirs, files in os.walk(root, followlinks=False):
                    if any((Path(current) / name).is_symlink() for name in dirs): raise RunnerError('INVALID_INPUT', 'checkpoint contains directory link')
                    for name in files:
                        path = Path(current) / name
                        if path.is_symlink() or not path.is_file(): raise RunnerError('INVALID_INPUT', 'checkpoint contains nonregular file')
                        count += 1
                        size += path.stat().st_size
                        if count > self.settings.max_files or size > self.settings.max_expanded_bytes: raise RunnerError('LIMIT', 'checkpoint exceeds limits')
                        z.write(path, path.relative_to(root).as_posix())
                        if stream.tell() > self.settings.max_archive_bytes: raise RunnerError('LIMIT', 'checkpoint compressed bytes exceed limit')
            if stream.tell() > self.settings.max_archive_bytes: raise RunnerError('LIMIT', 'checkpoint compressed bytes exceed limit')
            return stream.getvalue()

    def destroy_workspace(self, workspace_id: str) -> dict[str, Any]:
        with self.lock:
            with self.journal.connect() as db:
                row = db.execute('SELECT * FROM workspace WHERE id=?', (workspace_id,)).fetchone()
                if row and row['state'] == 'DESTROYED':
                    return {'workspace_id': workspace_id, 'state': 'DESTROYED', 'vm_destroyed': False}
            self._idle(workspace_id)
            with self.journal.connect() as db:
                runtime = db.execute('SELECT definition_json FROM runtime WHERE workspace_id=?', (workspace_id,)).fetchone()
                command_ids = [row['id'] for row in db.execute('SELECT id FROM command WHERE workspace_id=?', (workspace_id,))]
            if runtime and hasattr(self.backend, 'destroy_runtime'):
                self.backend.destroy_runtime(json.loads(runtime['definition_json']), command_ids)
            shutil.rmtree(self.workspaces / workspace_id)
            with self.journal.connect() as db: db.execute('UPDATE workspace SET state=? WHERE id=?', ('DESTROYED', workspace_id))
            # This confirms workspace removal only. VM destruction belongs to provider.
            return {'workspace_id': workspace_id, 'state': 'DESTROYED', 'vm_destroyed': False}


def create_app(settings: RunnerSettings, backend: ExecutionBackend | None = None) -> FastAPI:
    service = RunnerService(settings, backend)
    app = FastAPI(title='v2c isolated runner', version='1', docs_url=None, redoc_url=None)
    app.state.service = service

    async def auth(request: Request) -> None:
        supplied = request.headers.get('Authorization', '')
        if not secrets.compare_digest(supplied, 'Bearer ' + settings.bearer_secret): raise HTTPException(401, 'runner authorization denied')

    @app.middleware('http')
    async def bound_body(request: Request, call_next: Callable) -> Response:
        limit = ((settings.max_archive_bytes + 2) // 3) * 4 + 1048576
        if request.method in ('POST', 'PUT'):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > limit: return JSONResponse({'detail': 'body exceeds runner profile'}, status_code=413)
            request._body = bytes(body)
        return await call_next(request)

    @app.exception_handler(RunnerError)
    async def runner_error(request: Request, error: RunnerError) -> JSONResponse:
        code = {'NOT_FOUND': 404, 'INVALID_INPUT': 422, 'LIMIT': 413, 'AUTH': 403, 'INVALID_CONFIG': 503, 'UNSUPPORTED': 422, 'UNAVAILABLE': 503}.get(error.code, 409)
        return JSONResponse({'code': error.code, 'detail': str(error)}, status_code=code)

    dependencies = [Depends(auth)]
    @app.get('/health', dependencies=dependencies)
    def health() -> dict[str, str]: return {'status': 'ok', 'protocol': 'v1'}
    @app.post('/v1/workspaces', dependencies=dependencies)
    def prepare(request: PrepareRequest) -> dict[str, Any]: return service.prepare(request)
    @app.put('/v1/authorization', dependencies=dependencies)
    def authorize(request: AuthorizationRequest) -> dict[str, Any]: return service.authorize(request)
    @app.post('/v1/commands', dependencies=dependencies, response_model=CommandResult, status_code=202)
    def execute(request: CommandRequest) -> CommandResult: return service.execute(request)
    @app.get('/v1/commands/{command_id}', dependencies=dependencies, response_model=CommandResult)
    def status(command_id: str) -> CommandResult: return service.status(command_id)
    @app.post('/v1/commands/{command_id}/cancel', dependencies=dependencies, response_model=CommandResult)
    def cancel(command_id: str) -> CommandResult: return service.cancel(command_id)
    @app.get('/v1/workspaces/{workspace_id}/checkpoint', dependencies=dependencies)
    def checkpoint(workspace_id: str) -> Response: return Response(service.checkpoint(workspace_id), media_type='application/zip')
    @app.delete('/v1/workspaces/{workspace_id}', dependencies=dependencies)
    def destroy(workspace_id: str) -> dict[str, Any]: return service.destroy_workspace(workspace_id)
    return app


def main() -> None:
    """Start with mandatory mutual TLS. No unsafe HTTP launch switch."""
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', help='operator-only JSON RunnerSettings file; otherwise V2C_RUNNER_ environment')
    parser.add_argument('--tls-cert', required=True)
    parser.add_argument('--tls-key', required=True)
    parser.add_argument('--client-ca', required=True)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8443)
    args = parser.parse_args()
    settings = RunnerSettings.model_validate_json(Path(args.config).read_text()) if args.config else RunnerSettings.from_env()
    uvicorn.run(create_app(settings), host=args.host, port=args.port, ssl_certfile=args.tls_cert,
                ssl_keyfile=args.tls_key, ssl_ca_certs=args.client_ca, ssl_cert_reqs=ssl.CERT_REQUIRED)


if __name__ == '__main__': main()
