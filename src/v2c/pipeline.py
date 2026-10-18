"""Project-specific transformation workflow.

Only the runner executes project code. Model responses are proposals, and every
effect is journaled before dispatch. Test doubles exercise this controller; they
do not qualify a real project, model provider or VM isolation.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .adapters.model import AdapterError, ErrorCategory, ModelPort, ModelRequest
from .adapters.runner import CommandRequest, CommandResult, PostgresService, PrepareRequest, RunnerError
from .engine import DomainError, Engine, REQUIRED_VERIFICATIONS, digest
from .delivery import DeliveryError, contains_secret
from .intake import IntakeError, Snapshot, inspect_snapshot, safe_relative_path, snapshot_archive_bytes
from .storage import ArtifactStore, StorageError


class InputRequired(Exception):
    def __init__(self, reason: str, details: dict | None = None):
        super().__init__(reason)
        self.reason, self.details = reason, details or {}


class StepFailure(Exception):
    def __init__(self, reason: str, evidence: dict | None = None, *, retryable: bool = True):
        super().__init__(reason)
        self.reason, self.evidence, self.retryable = reason, evidence or {}, retryable


class RunCancelled(Exception):
    pass


class DTO(BaseModel):
    model_config = ConfigDict(extra='forbid')


class RecipeCommand(DTO):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: str = '.'
    timeout_seconds: int = Field(gt=0)

    @field_validator('argv')
    @classmethod
    def bounded_arguments(cls, values: list[str]) -> list[str]:
        if any('\x00' in value for value in values) or sum(len(v.encode()) for v in values) > 100_000:
            raise ValueError('invalid command arguments')
        return values

    @field_validator('cwd')
    @classmethod
    def relative_directory(cls, value: str) -> str:
        if value != '.':
            safe_relative_path(value)
        return value


class EnvironmentRecipe(DTO):
    runtime: str = Field(min_length=1, max_length=500)
    setup: list[RecipeCommand]
    restore: list[RecipeCommand]
    tests: list[RecipeCommand] = Field(min_length=1)
    recovery_checks: list[RecipeCommand] = Field(min_length=1)
    state_strategy: Literal['stateless', 'rebuild_from_fixtures']
    services: list[PostgresService] = Field(max_length=1)
    limitations: list[str]


class Finding(DTO):
    id: str = Field(min_length=1, max_length=100)
    criterion: str
    reason: str
    evidence_files: list[str]


class PlannedWork(DTO):
    id: str = Field(min_length=1, max_length=100)
    title: str
    files: list[str] = Field(min_length=1, max_length=100)
    reason: str
    preserve: list[str] = Field(min_length=1)
    checks: list[str] = Field(min_length=1)


class ProjectPlan(DTO):
    summary: str
    findings: list[Finding]
    steps: list[PlannedWork] = Field(min_length=0, max_length=30)
    limitations: list[str]


class FileChange(DTO):
    path: str
    old_sha256: str | None
    content_b64: str | None

    @field_validator('path')
    @classmethod
    def checked_path(cls, value: str) -> str:
        return safe_relative_path(value)


class PatchProposal(DTO):
    explanation: str
    files: list[FileChange] = Field(min_length=1, max_length=100)


@dataclass(frozen=True)
class PipelineLimits:
    command_timeout_seconds: int = 120
    run_timeout_seconds: int = 3600
    model_max_input_bytes: int = 262144
    max_patch_bytes: int = 65536
    poll_seconds: float = 0.2


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'v2c:' + ':'.join(parts)))


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def code_zip(files: dict[str, bytes], *, executable_paths: set[str] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, body in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if name in (executable_paths or set()) else 0o100644) << 16
            archive.writestr(info, body)
    return output.getvalue()


def archive_executables(data: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {safe_relative_path(item.filename) for item in archive.infolist()
                if not item.is_dir() and (item.external_attr >> 16) & 0o111}


def checked_archive_files(data: bytes, *, max_file_bytes: int = 16 * 1024 * 1024) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            name = safe_relative_path(item.filename)
            if name in files:
                raise InputRequired('CHECKPOINT_DUPLICATE_PATH')
            total += item.file_size
            if item.file_size > max_file_bytes or total > 256 * 1024 * 1024 or len(files) >= 20000:
                raise InputRequired('CHECKPOINT_SIZE_LIMIT')
            files[name] = archive.read(item)
    return files


def assert_source_unchanged(expected: bytes, observed: bytes) -> bytes:
    """Tests may create outputs, but cannot silently replace the candidate code."""
    source, actual = checked_archive_files(expected), checked_archive_files(observed)
    changed = [path for path, body in source.items() if actual.get(path) != body]
    if changed:
        raise StepFailure('SOURCE_MUTATED_BY_EXECUTION', {'files': changed})
    expected_modes = archive_executables(expected)
    actual_modes = archive_executables(observed) & source.keys()
    if expected_modes != actual_modes:
        raise StepFailure('SOURCE_MODE_MUTATED_BY_EXECUTION', {'files': sorted(expected_modes ^ actual_modes)})
    # Reproducible dependencies/build/test outputs are recreated by the recipe;
    # only explicitly accepted source files become the delivered code version.
    return code_zip({path: actual[path] for path in source}, executable_paths=expected_modes)


_SECRET = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bsk-[A-Za-z0-9_-]{20,}\b')


def screened_text(path: str, data: bytes) -> str | None:
    """Conservative context screen; deliberately not a security certification."""
    name = PurePosixPath(path).name.casefold()
    if name == '.env' or (name.startswith('.env.') and not name.endswith(('example', 'sample', 'template'))):
        raise InputRequired('SOURCE_SECRET_REVIEW_REQUIRED', {'path': path})
    if b'\x00' in data:
        return None
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return None
    if _SECRET.search(text) or contains_secret(data):
        raise InputRequired('SOURCE_SECRET_REVIEW_REQUIRED', {'path': path})
    return text


def snapshot_archive(snapshot: Snapshot) -> bytes:
    """Export inspected code, without host paths or Git implementation files."""
    inspect_snapshot(snapshot)
    for repo in snapshot.repositories:
        for item in repo.files:
            path = repo.path + '/' + item['path']
            screened_text(path, (snapshot.root / path).read_bytes())
    return snapshot_archive_bytes(snapshot)


def archive_context(data: bytes, *, max_bytes: int, selected: list[str] | None = None) -> dict:
    """Bounded selected source, with explicit omitted paths instead of silent RAG."""
    result: dict[str, Any] = {'files': [], 'omitted_files': []}
    remaining = max_bytes
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = sorted(item.filename for item in archive.infolist() if not item.is_dir())
        if selected is not None:
            priority = [name for name in names if name in selected]
            names = priority + [name for name in names if name not in priority]
        for name in names:
            safe_relative_path(name)
            info = archive.getinfo(name)
            if info.file_size > remaining or info.file_size > max_bytes:
                result['omitted_files'].append(name)
                continue
            body = archive.read(name)
            text = screened_text(name, body)
            if text is None:
                result['omitted_files'].append(name)
                continue
            encoded_size = len(json_bytes({'path': name, 'text': text}))
            if encoded_size > remaining:
                result['omitted_files'].append(name)
                continue
            result['files'].append({'path': name, 'sha256': hashlib.sha256(body).hexdigest(), 'text': text})
            remaining -= encoded_size
    return result


# This program is passed to Python *inside the project container*. The control
# process never imports or evaluates source files or generated project programs.
_APPLY_FILES = r'''
import base64, hashlib, json, pathlib, sys
root = pathlib.Path.cwd().resolve()
items = json.loads(base64.b64decode(sys.argv[1]))
for item in items:
    path = root / item['path']
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('unsafe patch path')
    current = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    if current != item['old_sha256']:
        raise ValueError('patch base mismatch')
for item in items:
    path = root / item['path']
    if item['content_b64'] is None:
        path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(item['content_b64'], validate=True))
print('v2c file operation completed')
'''


class Pipeline:
    def __init__(self, engine: Engine, store: ArtifactStore, model: ModelPort | None = None,
                 runner: Any = None, settings: Any = None, *, vm_provider: Any = None,
                 runner_factory: Callable[[Any], Any] | None = None):
        self.engine, self.store, self.model = engine, store, model
        self.runner, self.settings = runner, settings
        self.vm_provider, self.runner_factory = vm_provider, runner_factory
        self.limits = PipelineLimits(**{name: getattr(settings, name) for name in
                                      ('command_timeout_seconds', 'run_timeout_seconds', 'model_max_input_bytes')
                                      if settings is not None and hasattr(settings, name)})
        self._active: dict[str, tuple[Any, Any]] = {}

    def _guard(self, claim: dict) -> None:
        run = self.engine.get_run(claim['run']['id'])
        if run['cancel_requested']:
            raise RunCancelled()
        self.engine.assert_current(claim['attempt']['id'], claim['attempt']['generation'])
        step = self.engine.get_step(claim['step']['id'])
        if step['generation'] != claim['attempt']['generation'] or step['status'] != 'RUNNING':
            raise DomainError('STALE_GENERATION')
        if self.engine.clock() - run['created_at'] > run['limits'].get('run_timeout_seconds', self.limits.run_timeout_seconds):
            raise InputRequired('RUN_TIME_LIMIT_REACHED')

    @staticmethod
    def _fence(claim: dict) -> dict:
        return {'attempt_id': claim['attempt']['id'], 'generation': claim['attempt']['generation']}

    def _source(self, claim: dict) -> Snapshot:
        manifest = self.engine.get_snapshot(claim['run']['snapshot_id'])['manifest']
        root = manifest.get('workspace_root') or manifest.get('root')
        if not root:
            raise InputRequired('SOURCE_WORKSPACE_MISSING')
        try:
            snapshot = Snapshot.load(Path(root))
        except FileNotFoundError:
            from .intake import restore_source_snapshot
            if not manifest.get('archive_key') or not manifest.get('source_git_bundles'):
                raise InputRequired('SOURCE_WORKSPACE_MISSING') from None
            archive = self.store.get_bytes(manifest['archive_key'])
            if hashlib.sha256(archive).hexdigest() != manifest['archive_sha256']:
                raise InputRequired('SOURCE_ARCHIVE_CHECKSUM_MISMATCH')
            bundles = {name: self._artifact_bytes(ref) for name, ref in manifest['source_git_bundles'].items()}
            destination = Path(getattr(self.settings, 'work_root', Path(root).parent)) / 'restored-sources' / claim['run']['snapshot_id']
            snapshot = restore_source_snapshot(manifest, archive, bundles, destination)
        if snapshot.project_id != claim['run']['project_id']:
            raise InputRequired('SOURCE_PROJECT_MISMATCH')
        return snapshot

    def _completed(self, run_id: str, kind: str) -> list[dict]:
        return [s for s in self.engine.list_steps(run_id) if s['kind'] == kind and s['status'] == 'SUCCEEDED']

    def _required_result(self, run_id: str, kind: str) -> dict:
        matches = self._completed(run_id, kind)
        if not matches:
            raise InputRequired('PREREQUISITE_MISSING', {'stage': kind})
        return matches[-1]['result']

    def _artifact_bytes(self, ref: dict) -> bytes:
        data = self.store.get_bytes(ref['key'])
        if hashlib.sha256(data).hexdigest() != ref['sha256'] or len(data) != ref['size']:
            raise InputRequired('CHECKPOINT_CHECKSUM_MISMATCH')
        return data

    def _last_checkpoint(self, claim: dict) -> dict:
        previous = [s for s in self.engine.list_steps(claim['run']['id'])
                    if s['position'] < claim['step']['position'] and s['status'] == 'SUCCEEDED'
                    and (s.get('result') or {}).get('checkpoint')]
        if not previous:
            raise InputRequired('CHECKPOINT_MISSING')
        return previous[-1]['result']['checkpoint']

    def _operation(self, claim: dict, key: str, request: dict) -> tuple[str, dict, bool]:
        self._guard(claim)
        operation_id = stable_id(claim['attempt']['id'], key)
        try:
            old = self.engine.get_command(operation_id)
        except DomainError as error:
            if error.code != 'NOT_FOUND':
                raise
            old = None
        if old is not None:
            # Generation belongs to the original dispatch. Readback is allowed,
            # while any newly dispatched operation uses the current generation.
            expected = {k: v for k, v in request.items() if k != 'generation'}
            previous = {k: v for k, v in old['request'].items() if k != 'generation'}
            if digest(expected) != digest(previous):
                raise DomainError('COMMAND_ID_CONFLICT')
            return operation_id, old, True
        row = self.engine.prepare_command(claim['attempt']['id'], claim['attempt']['generation'], operation_id, request)
        return operation_id, row, False

    def _observed(self, claim: dict, operation_id: str, status: str, result: dict) -> dict:
        return self.engine.record_command_result(operation_id, claim['attempt']['generation'], status, result)

    async def _model(self, claim: dict, key: str, task: str, context: dict, schema: type[DTO]) -> dict:
        if self.model is None:
            raise InputRequired('MODEL_PROFILE_REQUIRED')
        request_id = stable_id(claim['attempt']['id'], 'model_' + key)
        context_json = json_bytes(context).decode()
        if _SECRET.search(context_json):
            raise InputRequired('MODEL_CONTEXT_SECRET_REVIEW_REQUIRED')
        if len(context_json.encode()) > self.limits.model_max_input_bytes:
            raise InputRequired('MODEL_CONTEXT_LIMIT', {'stage': key})
        profile = getattr(self.model, 'profile', None)
        profile_version = getattr(profile, 'version', 'test' if getattr(self.model, 'is_test', False) else 'unknown')
        if profile is not None and hasattr(profile, 'model_dump'):
            profile_manifest = profile.model_dump(mode='json')
            if 'capabilities' in profile_manifest:
                profile_manifest['capabilities'] = sorted(profile_manifest['capabilities'])
        elif getattr(self.model, 'is_test', False):
            profile_manifest = {'version': 'test', 'adapter_type': 'test-double',
                                'implementation': type(self.model).__module__ + '.' + type(self.model).__qualname__}
        else:
            raise InputRequired('MODEL_PROFILE_REQUIRED')
        try:
            profile_manifest = self.engine.bind_model_profile(claim['run']['id'], profile_manifest, **self._fence(claim))
        except DomainError as error:
            if error.code not in {'MODEL_PROFILE_CHANGED', 'INVALID_MODEL_PROFILE'}:
                raise
            raise InputRequired(error.code) from error
        op_id, row, existed = self._operation(claim, 'model_' + key, {'operation': 'model', 'request_id': request_id,
                'task_hash': hashlib.sha256(task.encode()).hexdigest(), 'context_hash': digest(context),
                'schema_hash': digest(schema.model_json_schema()), 'profile_version': profile_version,
                'profile_manifest': profile_manifest, 'profile_hash': digest(profile_manifest)})
        if row['status'] == 'SUCCEEDED':
            return schema.model_validate(row['result']['structured']).model_dump()
        if existed:
            self._observed(claim, op_id, 'STOPPED', {'provider_outcome': 'UNKNOWN', 'usage_unknown': True})
            self.engine.record_usage(claim['run']['id'], request_id, profile_version,
                                     {'usage_unknown': True, 'delivery_unknown': True}, claim['attempt']['id'])
            raise InputRequired('MODEL_OUTCOME_UNKNOWN', {'request_id': request_id})
        try:
            value = await self.model.generate(ModelRequest(request_id=request_id, task=task, context=context_json,
                    response_schema=schema.model_json_schema(), required_capabilities=frozenset({'text', 'structured'})))
            # Cost is an observed fact even when response validation, cancellation
            # or lease fencing later rejects publication of its proposal.
            usage = value.usage.model_dump() if hasattr(value.usage, 'model_dump') else value.usage
            self.engine.record_usage(claim['run']['id'], request_id, profile_version,
                                     {'usage': usage, 'provider_request_id': value.provider_request_id,
                                      'profile_version': profile_version, 'delivery_unknown': False,
                                      'usage_unknown': all(usage.get(k) is None for k in ('input_tokens', 'output_tokens', 'total_tokens')),
                                      'test_adapter': bool(getattr(self.model, 'is_test', False))},
                                     claim['attempt']['id'])
            self._guard(claim)
            structured = schema.model_validate(value.structured).model_dump()
            self._observed(claim, op_id, 'SUCCEEDED', {'structured': structured, 'usage': usage})
            return structured
        except AdapterError as error:
            self.engine.record_usage(claim['run']['id'], request_id, profile_version,
                                     {'usage_unknown': error.usage_unknown, 'delivery_unknown': error.delivery_unknown,
                                      'provider_request_id': error.provider_request_id, 'category': error.category.value},
                                     claim['attempt']['id'])
            self._observed(claim, op_id, 'FAILED', {'category': error.category.value, 'delivery_unknown': error.delivery_unknown})
            if error.category in {ErrorCategory.AUTH, ErrorCategory.INVALID_CONFIG, ErrorCategory.UNSUPPORTED_CAPABILITY, ErrorCategory.CANCELLED} or error.delivery_unknown:
                raise InputRequired(error.category.value, {'usage_unknown': error.usage_unknown}) from error
            raise StepFailure(error.category.value, retryable=error.retryable or error.category == ErrorCategory.INVALID_RESPONSE) from error
        except ValidationError as error:
            self._observed(claim, op_id, 'FAILED', {'category': 'INVALID_RESPONSE'})
            raise StepFailure('INVALID_RESPONSE') from error

    def _execution_profile(self, claim: dict) -> dict:
        profile = claim['run']['limits'].get('execution_limits') or getattr(self.settings, 'execution_limits', None)
        required = {'cpus', 'memory_mb', 'pids', 'disk_mb', 'max_output_bytes', 'network', 'image'}
        if not isinstance(profile, dict) or not required <= profile.keys() or not profile.get('image'):
            raise InputRequired('EXECUTION_PROFILE_REQUIRED')
        if any(not isinstance(profile[k], (int, float)) or isinstance(profile[k], bool) or profile[k] <= 0
               for k in ('cpus', 'memory_mb', 'pids', 'disk_mb', 'max_output_bytes')):
            raise InputRequired('EXECUTION_PROFILE_INVALID')
        if profile['network'] != 'none':
            raise InputRequired('EXECUTION_NETWORK_POLICY_UNSUPPORTED')
        if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}', profile['image']):
            raise InputRequired('EXECUTION_IMAGE_DIGEST_REQUIRED')
        if self.vm_provider is None or self.runner_factory is None:
            raise InputRequired('VM_PROVIDER_REQUIRED')
        return profile

    async def _allocate(self, claim: dict, purpose: str) -> tuple[Any, Any]:
        profile = self._execution_profile(claim)
        allocation_id = stable_id(claim['attempt']['id'], purpose)
        op_id, row, existed = self._operation(claim, 'vm_' + purpose,
            {'operation': 'allocate_vm', 'allocation_id': allocation_id, 'purpose': purpose, 'execution_limits': profile})
        if row['status'] == 'SUCCEEDED':
            from .adapters.runner import VMAllocation
            allocation = VMAllocation.model_validate(row['result'])
        else:
            allocation = await self.vm_provider.create(allocation_id, purpose)
            deadline = time.monotonic() + self.limits.command_timeout_seconds
            while allocation.state == 'CREATING' and time.monotonic() < deadline:
                self._guard(claim)
                await asyncio.sleep(self.limits.poll_seconds)
                allocation = await self.vm_provider.status(allocation.vm_id)
            if allocation.state != 'READY':
                raise InputRequired('VM_ALLOCATION_NOT_READY')
            self._observed(claim, op_id, 'SUCCEEDED', allocation.model_dump())
            self._active[claim['attempt']['id']] = (allocation, None)
            # Every phase/attempt has a distinct allocation. In particular the
            # final verifier cannot be routed back to the transformation VM.
            for prior in self.engine.list_commands(run_id=claim['run']['id']):
                if prior['id'] != op_id and prior['request'].get('operation') == 'allocate_vm' and (prior.get('result') or {}).get('vm_id') == allocation.vm_id:
                    raise InputRequired('FRESH_VM_REQUIRED')
        runner = self.runner_factory(allocation)
        self._active[claim['attempt']['id']] = (allocation, runner)
        return allocation, runner

    async def _prepare(self, claim: dict, runner: Any, archive: bytes, recipe: dict) -> str:
        workspace = stable_id(claim['attempt']['id'], 'workspace')
        request = PrepareRequest(workspace_id=workspace, attempt_id=claim['attempt']['id'], generation=claim['attempt']['generation'],
                                 archive_b64=base64.b64encode(archive).decode(), archive_sha256=hashlib.sha256(archive).hexdigest(),
                                 input_manifest={'execution_limits': self._execution_profile(claim), 'environment_recipe': recipe})
        op_id, row, existed = self._operation(claim, 'prepare', {**request.model_dump(exclude={'archive_b64'}), 'operation': 'prepare'})
        if row['status'] != 'SUCCEEDED':
            # The runner's workspace creation is idempotent by the exact input.
            # Re-delivery retains the original generation and archive identity.
            if existed:
                request = request.model_copy(update={'generation': row['request']['generation']})
            result = await runner.prepare(request)
            self._observed(claim, op_id, 'SUCCEEDED', result)
        self._guard(claim)
        await runner.authorize(workspace, claim['attempt']['id'], claim['attempt']['generation'])
        return workspace

    async def _command(self, claim: dict, runner: Any, workspace: str, command: RecipeCommand,
                       key: str) -> dict:
        request = CommandRequest(command_id=stable_id(claim['attempt']['id'], key), workspace_id=workspace,
                                 attempt_id=claim['attempt']['id'], generation=claim['attempt']['generation'],
                                 argv=command.argv, cwd=command.cwd,
                                 timeout_seconds=min(command.timeout_seconds, self.limits.command_timeout_seconds))
        op_id, row, existed = self._operation(claim, key, request.model_dump())
        if row['status'] in {'SUCCEEDED', 'FAILED', 'CANCELLED'}:
            return row['result']
        if row['status'] == 'STOPPED':
            raise StepFailure('PREVIOUS_EXECUTION_STOPPED', retryable=True)
        if existed:
            try:
                result = await runner.status(op_id)
            except RunnerError as error:
                raise InputRequired('COMMAND_OUTCOME_UNKNOWN', {'command_id': op_id}) from error
        else:
            try:
                result = await runner.execute(request)
            except RunnerError as error:
                # Same durable command ID, never a second execution request.
                if not error.delivery_unknown:
                    self._observed(claim, op_id, 'FAILED', {'reason': error.code, 'exit_code': None, 'state': 'FAILED'})
                    raise InputRequired(error.code) from error
                try:
                    result = await runner.status(op_id)
                except RunnerError as lookup_error:
                    raise InputRequired('COMMAND_OUTCOME_UNKNOWN', {'command_id': op_id}) from lookup_error
        deadline = time.monotonic() + request.timeout_seconds + 10
        while result.state in {'ACCEPTED', 'RUNNING'}:
            self._guard(claim)
            self._observed(claim, op_id, result.state, result.model_dump())
            if time.monotonic() > deadline:
                try:
                    result = await runner.cancel(op_id)
                except RunnerError as error:
                    raise InputRequired('COMMAND_STOP_UNCONFIRMED', {'command_id': op_id}) from error
                if not result.stop_confirmed:
                    raise InputRequired('COMMAND_STOP_UNCONFIRMED', {'command_id': op_id})
                break
            await asyncio.sleep(self.limits.poll_seconds)
            result = await runner.status(op_id)
        if result.request_hash != CommandRequest.model_validate(row['request']).content_hash():
            raise InputRequired('COMMAND_RESULT_IDENTITY_MISMATCH', {'command_id': op_id})
        if result.state == 'UNKNOWN' or (result.state == 'CANCELLED' and not result.stop_confirmed):
            raise InputRequired('COMMAND_OUTCOME_UNKNOWN', {'command_id': op_id})
        self._observed(claim, op_id, result.state, result.model_dump())
        return result.model_dump()

    async def _commands(self, claim: dict, runner: Any, workspace: str, commands: list[RecipeCommand],
                        prefix: str, *, require_success: bool = True) -> list[dict]:
        results = []
        for number, command in enumerate(commands):
            result = await self._command(claim, runner, workspace, command, f'{prefix}_{number}_{command.id}')
            results.append({'check_id': command.id, **result})
            if require_success and (result['state'] != 'SUCCEEDED' or result['exit_code'] != 0):
                raise StepFailure('CHECK_FAILED', {'checks': results})
        return results

    async def _destroy(self, claim: dict, allocation: Any) -> None:
        # VM destruction is the authority for termination, including a hostile
        # guest or a runner whose command journal has been lost.
        operation_id = stable_id(claim['attempt']['id'], 'destroy_' + allocation.vm_id)
        run = self.engine.get_run(claim['run']['id'])
        if not run['cancel_requested']:
            op_id, row, _ = self._operation(claim, 'destroy_' + allocation.vm_id,
                                          {'operation': 'destroy_vm', 'vm_id': allocation.vm_id})
            if row['status'] == 'SUCCEEDED':
                self._active.pop(claim['attempt']['id'], None)
                return
        else:
            op_id = operation_id
        result = await self.vm_provider.destroy(allocation.vm_id)
        deadline = time.monotonic() + self.limits.command_timeout_seconds
        while result.state != 'DESTROYED' and time.monotonic() < deadline:
            await asyncio.sleep(self.limits.poll_seconds)
            result = await self.vm_provider.status(allocation.vm_id)
        if result.state != 'DESTROYED':
            raise InputRequired('VM_STOP_UNCONFIRMED', {'vm_id': allocation.vm_id})
        if not run['cancel_requested']:
            self._observed(claim, op_id, 'SUCCEEDED', result.model_dump())
            for command in self.engine.commands_for_attempt(claim['attempt']['id']):
                if command['status'] not in {'SUCCEEDED', 'FAILED', 'CANCELLED', 'STOPPED'}:
                    self._observed(claim, command['id'], 'STOPPED', {'stop_confirmed': True, 'outcome_before_stop': 'UNKNOWN'})
        self._active.pop(claim['attempt']['id'], None)

    async def _checkpoint(self, claim: dict, runner: Any, workspace: str, recipe: dict,
                          *, key: str, expected_source: bytes) -> dict:
        op_id, row, _ = self._operation(claim, key, {'operation': 'checkpoint', 'workspace_id': workspace})
        if row['status'] == 'SUCCEEDED':
            return row['result']
        archive = assert_source_unchanged(expected_source, await runner.checkpoint(workspace))
        # Exported files remain untrusted. Full safe import runs again before
        # packaging; this bounded context check detects obvious secret leakage.
        archive_context(archive, max_bytes=self.limits.model_max_input_bytes // 2)
        sha = hashlib.sha256(archive).hexdigest()
        ref = self.store.put_bytes(f"runs/{claim['run']['id']}/checkpoints/{sha}.zip", archive)
        source = self._source(claim)
        manifest = {'repositories': [r.name for r in source.repositories], 'plan_revision': claim['run']['plan_revision'],
                    'environment': recipe, 'files': ref.to_dict(),
                    'services': {'strategy': recipe['state_strategy'], 'restore': recipe['restore'],
                                 'checks': recipe['recovery_checks']}, 'restore_verified': False}
        artifact = self.engine.record_artifact(claim['run']['id'], 'checkpoint', ref.key, ref.sha256, ref.size,
                                              manifest, True, attempt_id=claim['attempt']['id'], generation=claim['attempt']['generation'])
        self.engine.confirm_checkpoint(claim['run']['id'], artifact['id'], restore_verified=False, **self._fence(claim))
        checkpoint = {**ref.to_dict(), 'artifact_id': artifact['id'], 'manifest': manifest}
        self._observed(claim, op_id, 'SUCCEEDED', checkpoint)
        return checkpoint

    async def execute(self, claim: dict) -> dict:
        """Execute one leased step; waiting never means verified completion."""
        handler = getattr(self, '_stage_' + claim['step']['kind'], None)
        if handler is None:
            raise DomainError('UNKNOWN_STEP_KIND')
        result = None
        failure: Exception | None = None
        try:
            self._guard(claim)
            result = await handler(claim)
        except (InputRequired, StepFailure, RunCancelled, AdapterError, RunnerError) as error:
            failure = error
        except (IntakeError, DeliveryError, StorageError) as error:
            failure = InputRequired(str(error))
        finally:
            active = self._active.get(claim['attempt']['id'])
            if active is not None:
                try:
                    await self._destroy(claim, active[0])
                except (InputRequired, RunnerError) as cleanup_error:
                    failure = InputRequired('VM_STOP_UNCONFIRMED', {'original_reason': str(failure) if failure else None})
        if isinstance(failure, RunCancelled) or self.engine.get_run(claim['run']['id'])['cancel_requested']:
            await self.cancel_run(claim['run']['id'])
            return self.engine.get_run(claim['run']['id'])
        if isinstance(failure, InputRequired):
            # Unknown remote effects stay ACTIVE until a stop is confirmed; no
            # fresh attempt is licensed merely by a timeout or expired lease.
            unresolved = [c for c in self.engine.commands_for_attempt(claim['attempt']['id'])
                          if c['status'] not in {'SUCCEEDED', 'FAILED', 'CANCELLED', 'STOPPED'}]
            if unresolved:
                return {'status': 'RUNNING', 'reason': failure.reason, 'stop_confirmed': False}
            return self.engine.set_waiting(claim['attempt']['id'], claim['attempt']['generation'],
                                           'NEEDS_INPUT', failure.reason, failure.details)
        if failure is not None:
            if isinstance(failure, RunnerError):
                return self.engine.set_waiting(claim['attempt']['id'], claim['attempt']['generation'],
                                               'NEEDS_INPUT', failure.code, {})
            if isinstance(failure, StepFailure):
                return self.engine.finish_attempt(claim['attempt']['id'], claim['attempt']['generation'], failure.evidence,
                        outcome='FAILED', reason=failure.reason, retryable=failure.retryable, execution_confirmed=True)
            raise failure
        if result is None:  # A stage published a deliberate waiting state.
            return self.engine.get_run(claim['run']['id'])
        self._guard(claim)
        return self.engine.finish_attempt(claim['attempt']['id'], claim['attempt']['generation'], result)

    async def _stage_inspect(self, claim: dict) -> dict:
        snapshot = self._source(claim)
        inspection = inspect_snapshot(snapshot)
        archive = snapshot_archive(snapshot)
        ref = self.store.put_bytes(f"runs/{claim['run']['id']}/source/{hashlib.sha256(archive).hexdigest()}.zip", archive)
        context = {'map': {k: v for k, v in inspection.items() if k not in {'workspace_root', 'source'}},
                   'source': archive_context(archive, max_bytes=self.limits.model_max_input_bytes // 2)}
        recipe = await self._model(claim, 'recipe',
            'Infer a reproducible environment recipe from this unfamiliar project. Preserve its language and architecture. '
            'All commands run in an isolated fixed operator image with Python3, no network and synthetic data only. '
            'An explicitly declared single PostgreSQL service can be provided on a private internal network; '
            'its synthetic connection URL is available only as DATABASE_URL in project commands. '
            'Give exact argv and relative cwd under repositories/<name>. Never put secrets in commands. '
            'Tests and recovery_checks must assert real behavior and required schema/fixture/service state. '
            'For a stateless project state_strategy is stateless; otherwise restore must rebuild from migrations and synthetic fixtures. '
            'Setup must be repeat-safe; restore must deterministically reset and rebuild the synthetic test state from current code migrations and fixtures. '
            'List unavailable integrations in limitations; never replace them with a guessed success.', context, EnvironmentRecipe)
        self._validate_recipe(recipe)
        self.engine.add_step(claim['run']['id'], 'baseline', 'baseline', **self._fence(claim))
        return {'inspection': inspection, 'source_archive': ref.to_dict(), 'recipe': recipe}

    def _validate_recipe(self, recipe: dict) -> None:
        parsed = EnvironmentRecipe.model_validate(recipe)
        if parsed.services and parsed.state_strategy != 'rebuild_from_fixtures':
            raise InputRequired('TEST_STATE_RESTORE_RECIPE_REQUIRED')
        ids = [c.id for c in parsed.tests]
        if len(ids) != len(set(ids)):
            raise StepFailure('INVALID_RESPONSE', {'reason': 'duplicate test ids'})
        if parsed.state_strategy == 'rebuild_from_fixtures' and not parsed.restore:
            raise InputRequired('TEST_STATE_RESTORE_RECIPE_REQUIRED')
        for command in parsed.setup + parsed.restore + parsed.tests + parsed.recovery_checks:
            if command.timeout_seconds > self.limits.command_timeout_seconds:
                raise InputRequired('RECIPE_COMMAND_LIMIT_EXCEEDED', {'command_id': command.id})

    async def _stage_baseline(self, claim: dict) -> dict:
        inspected = self._required_result(claim['run']['id'], 'inspect')
        recipe = EnvironmentRecipe.model_validate(inspected['recipe'])
        _, runner = await self._allocate(claim, 'baseline')
        workspace = await self._prepare(claim, runner, self._artifact_bytes(inspected['source_archive']), recipe.model_dump())
        setup = await self._commands(claim, runner, workspace, recipe.setup + recipe.restore, 'baseline_setup')
        recovery = await self._commands(claim, runner, workspace, recipe.recovery_checks, 'baseline_recovery')
        checks = await self._commands(claim, runner, workspace, recipe.tests, 'baseline_test', require_success=False)
        checkpoint = await self._checkpoint(claim, runner, workspace, recipe.model_dump(), key='baseline_checkpoint',
                                            expected_source=self._artifact_bytes(inspected['source_archive']))
        self.engine.add_step(claim['run']['id'], 'plan', 'plan', **self._fence(claim))
        return {'setup': setup, 'recovery': recovery, 'checks': checks, 'checkpoint': checkpoint,
                'known_failures': [r['check_id'] for r in checks if r.get('exit_code') != 0]}

    async def _stage_plan(self, claim: dict) -> None:
        inspected = self._required_result(claim['run']['id'], 'inspect')
        baseline = self._required_result(claim['run']['id'], 'baseline')
        plan = await self._model(claim, 'plan',
            'Diagnose maintainability obstacles evidenced in this project, then propose small ordered changes. '
            'Keep current language/framework/data model unless a specific evidenced obstacle requires replacement. '
            'Each step must give exact workspace file paths (including new files), its reason, preserved behavior and check ids from the frozen recipe. '
            'Return an empty steps list when the existing project has no evidenced obstacle requiring changes. '
            'No held-out acceptance tasks are available to you. Architectural preservation is valid; documentation and tests can be the needed improvement.',
            {'inspection': {k: v for k, v in inspected['inspection'].items() if k != 'workspace_root'},
             'baseline': baseline, 'recipe': inspected['recipe'],
             'source': archive_context(self._artifact_bytes(baseline['checkpoint']), max_bytes=self.limits.model_max_input_bytes // 2)}, ProjectPlan)
        valid_checks = {c['id'] for c in inspected['recipe']['tests']}
        for work in plan['steps']:
            for path in work['files']:
                safe_relative_path(path)
                if not any(path.startswith(r.path + '/') for r in self._source(claim).repositories):
                    raise StepFailure('INVALID_RESPONSE', {'reason': 'work outside known repositories'})
            if not set(work['checks']) <= valid_checks:
                raise StepFailure('INVALID_RESPONSE', {'reason': 'unknown check ids'})
        published = self.engine.publish_plan(claim['run']['id'], plan, **self._fence(claim))
        self.engine.set_waiting(claim['attempt']['id'], claim['attempt']['generation'], 'AWAITING_PLAN_APPROVAL',
                                'PLAN_DECISION_REQUIRED', {'plan_revision': published['revision'], 'snapshot_id': published['snapshot_id']})
        return None

    def _checked_patch(self, proposal: dict, allowed: list[str], archive: bytes) -> PatchProposal:
        patch = PatchProposal.model_validate(proposal)
        if len({f.path for f in patch.files}) != len(patch.files):
            raise StepFailure('PATCH_DUPLICATE_PATH')
        total = 0
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            names = set(source.namelist())
            for file in patch.files:
                if file.path not in allowed:
                    raise InputRequired('PATCH_OUTSIDE_APPROVED_SCOPE', {'path': file.path})
                old = hashlib.sha256(source.read(file.path)).hexdigest() if file.path in names else None
                if old != file.old_sha256:
                    raise StepFailure('PATCH_BASE_MISMATCH', {'path': file.path})
                if file.content_b64 is not None:
                    try:
                        body = base64.b64decode(file.content_b64, validate=True)
                    except ValueError as error:
                        raise StepFailure('PATCH_ENCODING_INVALID') from error
                    screened_text(file.path, body)
                    total += len(body)
                elif old is None:
                    raise StepFailure('PATCH_DELETE_MISSING_FILE')
        if total > self.limits.max_patch_bytes:
            raise InputRequired('PATCH_SIZE_LIMIT')
        return patch

    async def _apply_patch(self, claim: dict, runner: Any, workspace: str, patch: PatchProposal, key: str) -> dict:
        encoded = base64.b64encode(json_bytes([f.model_dump() for f in patch.files])).decode()
        command = RecipeCommand(id='apply_files', argv=['python3', '-c', _APPLY_FILES, encoded],
                                cwd='.', timeout_seconds=self.limits.command_timeout_seconds)
        return (await self._commands(claim, runner, workspace, [command], key))[0]

    @staticmethod
    def _patched_archive(archive: bytes, patch: PatchProposal) -> bytes:
        files = checked_archive_files(archive)
        executable_paths = archive_executables(archive)
        for change in patch.files:
            if change.content_b64 is None:
                del files[change.path]
                executable_paths.discard(change.path)
            else:
                files[change.path] = base64.b64decode(change.content_b64, validate=True)
        return code_zip(files, executable_paths=executable_paths)

    async def _stage_transform(self, claim: dict) -> dict:
        inspected = self._required_result(claim['run']['id'], 'inspect')
        recipe = EnvironmentRecipe.model_validate(inspected['recipe'])
        work = claim['step']['payload'].get('plan_step')
        if work is None:
            plan = self.engine.get_plan(claim['run']['id'])
            work = plan['plan']['steps'][0]
        work = PlannedWork.model_validate(work)
        source_checkpoint = self._last_checkpoint(claim)
        archive = self._artifact_bytes(source_checkpoint)
        # The context contains neither held-out task nor expected acceptance.
        proposal = await self._model(claim, 'patch',
            'Implement only the approved work item. Return complete replacement file bytes as base64; '
            'use old_sha256 from source, null for a new file, content_b64 null for deletion. '
            'Do not change tests or files outside the approved exact paths. Preserve the listed behavior. '
            'The controller applies and checks your proposal in a disposable project container.',
            {'work': work.model_dump(), 'source': archive_context(archive, max_bytes=self.limits.model_max_input_bytes // 2, selected=work.files),
             'previous_failure': claim['step'].get('result')}, PatchProposal)
        patch = self._checked_patch(proposal, work.files, archive)
        _, runner = await self._allocate(claim, 'transform')
        workspace = await self._prepare(claim, runner, archive, recipe.model_dump())
        await self._commands(claim, runner, workspace, recipe.setup + recipe.restore, 'restore_setup')
        restored = await self._commands(claim, runner, workspace, recipe.recovery_checks, 'restore_verify')
        await self._apply_patch(claim, runner, workspace, patch, 'patch')
        await self._commands(claim, runner, workspace, recipe.setup + recipe.restore, 'post_patch_restore')
        updated_state = await self._commands(claim, runner, workspace, recipe.recovery_checks, 'post_patch_recovery')
        checks = await self._commands(claim, runner, workspace, recipe.tests, 'transform_test')
        checkpoint = await self._checkpoint(claim, runner, workspace, recipe.model_dump(), key='transform_checkpoint',
                                            expected_source=self._patched_archive(archive, patch))
        following = [s for s in self.engine.list_steps(claim['run']['id'])
                     if s['kind'] == 'transform' and s['position'] > claim['step']['position']]
        if not following:
            self.engine.add_step(claim['run']['id'], 'verify', 'verify', **self._fence(claim))
        return {'work_id': work.id, 'explanation': patch.explanation, 'files': [f.path for f in patch.files],
                'recovery': restored, 'post_patch_recovery': updated_state, 'checks': checks, 'checkpoint': checkpoint}

    async def _stage_verify(self, claim: dict) -> dict:
        inspected = self._required_result(claim['run']['id'], 'inspect')
        recipe = EnvironmentRecipe.model_validate(inspected['recipe'])
        checkpoint = self._last_checkpoint(claim)
        archive = self._artifact_bytes(checkpoint)
        final_digest = hashlib.sha256(archive).hexdigest()
        allocation, runner = await self._allocate(claim, 'final_verification')
        workspace = await self._prepare(claim, runner, archive, recipe.model_dump())
        await self._commands(claim, runner, workspace, recipe.setup + recipe.restore, 'final_restore')
        recovery = await self._commands(claim, runner, workspace, recipe.recovery_checks, 'final_recovery')
        checks = await self._commands(claim, runner, workspace, recipe.tests, 'final_regression')
        assert_source_unchanged(archive, await runner.checkpoint(workspace))
        evidence = {'vm_id': allocation.vm_id, 'checkpoint_sha256': checkpoint['sha256'],
                    'checks': checks, 'test_double': bool(getattr(runner, 'is_test', False))}
        self.engine.record_verification(claim['run']['id'], 'final_regression', True, evidence, final_digest,
                                        attempt_id=claim['attempt']['id'], generation=claim['attempt']['generation'])
        self.engine.record_verification(claim['run']['id'], 'recovery', True,
                                        {**evidence, 'checks': recovery, 'strategy': recipe.state_strategy}, final_digest,
                                        attempt_id=claim['attempt']['id'], generation=claim['attempt']['generation'])
        self.engine.confirm_checkpoint(claim['run']['id'], checkpoint['artifact_id'], restore_verified=True, **self._fence(claim))
        self.engine.add_step(claim['run']['id'], 'package', 'package', **self._fence(claim))
        return {'checkpoint': checkpoint, 'final_digest': final_digest, 'checks': checks, 'recovery': recovery,
                'verification_vm_id': allocation.vm_id}

    async def _stage_package(self, claim: dict) -> dict:
        from .intake import restore_snapshot_archive
        from .delivery import package_delivery
        verified = self._required_result(claim['run']['id'], 'verify')
        inspected = self._required_result(claim['run']['id'], 'inspect')
        source = self._source(claim)
        work_root = Path(getattr(self.settings, 'work_root', source.root.parent))
        export_root = work_root / 'deliveries' / claim['run']['id'] / claim['attempt']['id']
        export_root.mkdir(parents=True, exist_ok=True)
        restored_path = export_root / 'snapshot'
        if restored_path.exists():
            snapshot = Snapshot.load(restored_path)
        else:
            snapshot = restore_snapshot_archive(self._artifact_bytes(verified['checkpoint']), restored_path, original_snapshot=source)
        recipe = inspected['recipe']
        transformed = self._completed(claim['run']['id'], 'transform')
        project_manifest = {'final_digest': verified['final_digest'], 'environment_recipe': recipe,
            'commands': {label: [{'argv': c['argv'], 'cwd': c['cwd'], 'evidence_id': c['id']} for c in recipe[key]]
                         for label, key in [('setup', 'setup'), ('test', 'tests'), ('restore', 'restore')]},
            'map': {k: v for k, v in inspected['inspection'].items() if k != 'workspace_root'},
            'changes': [{'work_id': s['result']['work_id'], 'files': s['result']['files']} for s in transformed],
            'examples': [{'file': f.removeprefix('repositories/'), 'description': s['result']['explanation']}
                         for s in transformed for f in s['result']['files']],
            'limitations': recipe['limitations'] + ['Human and independent AI handover acceptance remain required.']}
        bundle = package_delivery(snapshot, export_root / 'bundle', project_manifest=project_manifest,
                                  verification={'status': 'CANDIDATE', 'checks': self._public_receipts(self.engine.verifications(claim['run']['id']))},
                                  store=self.store, original_snapshot=source)
        value = bundle.to_dict()
        # The delivery module supplies an immutable object reference; a candidate
        # is downloadable but does not grant READY by itself.
        ref = value.get('artifact') or value.get('artifact_ref') or value.get('object_ref')
        if ref is None:
            archive_path = Path(value.get('archive_path') or value.get('archive') or value.get('path'))
            data = archive_path.read_bytes()
            ref = self.store.put_bytes(f"runs/{claim['run']['id']}/bundles/{hashlib.sha256(data).hexdigest()}.zip", data).to_dict()
        self.engine.record_artifact(claim['run']['id'], 'bundle', ref['key'], ref['sha256'], ref['size'],
                                    {**value, 'final_digest': verified['final_digest'], 'status': 'CANDIDATE'}, True,
                                    attempt_id=claim['attempt']['id'], generation=claim['attempt']['generation'])
        self.engine.add_step(claim['run']['id'], 'ai_handoff', 'ai_handoff', **self._fence(claim))
        return {'bundle': ref, 'final_digest': verified['final_digest'], 'delivery': value}

    async def _stage_ai_handoff(self, claim: dict) -> dict:
        verified = self._required_result(claim['run']['id'], 'verify')
        payload = claim['step']['payload']
        if not payload.get('task') or not payload.get('checks'):
            raise InputRequired('HELD_OUT_TASK_REQUIRED', {'final_digest': verified['final_digest']})
        checks = [RecipeCommand.model_validate(c) for c in payload['checks']]
        archive = self._artifact_bytes(verified['checkpoint'])
        source = self._source(claim)
        allowed = []
        with zipfile.ZipFile(io.BytesIO(archive)) as files:
            allowed = files.namelist()
        proposal = await self._model(claim, 'independent_handoff',
            'You are a new maintainer. Use only the delivered project and its project instructions to perform this held-out change. '
            'Return replacement file bytes as base64 using existing hashes. No prior transformation conversation or hidden checks are available.',
            {'task': payload['task'], 'source': archive_context(archive, max_bytes=self.limits.model_max_input_bytes // 2),
             'instructions': self._delivery_instructions(claim)}, PatchProposal)
        # New file paths can be explicitly allowed by the owner for this task.
        for path in payload.get('allowed_new_files', []):
            safe_relative_path(path)
            if not any(path.startswith(r.path + '/') for r in source.repositories):
                raise InputRequired('HELD_OUT_PATH_INVALID')
            allowed.append(path)
        patch = self._checked_patch(proposal, allowed, archive)
        recipe = EnvironmentRecipe.model_validate(self._required_result(claim['run']['id'], 'inspect')['recipe'])
        allocation, runner = await self._allocate(claim, 'ai_handoff')
        workspace = await self._prepare(claim, runner, archive, recipe.model_dump())
        await self._commands(claim, runner, workspace, recipe.setup + recipe.restore, 'handoff_restore')
        await self._commands(claim, runner, workspace, recipe.recovery_checks, 'handoff_recovery')
        await self._apply_patch(claim, runner, workspace, patch, 'handoff_patch')
        await self._commands(claim, runner, workspace, recipe.setup + recipe.restore, 'handoff_post_patch_restore')
        await self._commands(claim, runner, workspace, recipe.recovery_checks, 'handoff_post_patch_recovery')
        regression = await self._commands(claim, runner, workspace, recipe.tests, 'handoff_regression')
        acceptance = await self._commands(claim, runner, workspace, checks, 'hidden_acceptance')
        assert_source_unchanged(self._patched_archive(archive, patch), await runner.checkpoint(workspace))
        evidence = {'vm_id': allocation.vm_id, 'checks': acceptance, 'regression': regression,
                    'request_id': stable_id(claim['attempt']['id'], 'model_independent_handoff'),
                    'task_digest': hashlib.sha256(payload['task'].encode()).hexdigest(),
                    'test_double': bool(getattr(runner, 'is_test', False) or getattr(self.model, 'is_test', False))}
        self.engine.record_verification(claim['run']['id'], 'ai_handoff', True, evidence, verified['final_digest'],
                                        attempt_id=claim['attempt']['id'], generation=claim['attempt']['generation'])
        # The held-out modification is an acceptance exercise, not silently a
        # new version of the product handed to its owner.
        return {'final_digest': verified['final_digest'], 'acceptance': acceptance,
                'human_handoff': 'AWAITING_HANDOFF'}

    def _delivery_instructions(self, claim: dict) -> dict:
        package = self._required_result(claim['run']['id'], 'package')
        bundle = self._artifact_bytes(package['bundle'])
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            docs = {}
            remaining = self.limits.model_max_input_bytes // 4
            for name in sorted(archive.namelist()):
                if ('skills/' in name or name.endswith(('README.md', 'ARCHITECTURE.md'))) and archive.getinfo(name).file_size <= remaining:
                    body = archive.read(name)
                    text = screened_text(name, body)
                    if text is not None:
                        docs[name] = text
                        remaining -= len(body)
            return docs

    @staticmethod
    def _public_receipts(rows: list[dict]) -> list[dict]:
        """Evidence references travel; hidden assertions and answers do not."""
        return [{**{key: row[key] for key in ('id', 'kind', 'passed', 'final_digest', 'sequence') if key in row},
                 'evidence_sha256': digest(row['evidence']),
                 'test_double': bool(row['evidence'].get('test_double', False)),
                 **({'actor': row['evidence']['actor']} if row['evidence'].get('actor') else {})}
                for row in rows]

    def seal_handoff_bundle(self, run_id: str, final_digest: str) -> dict:
        """Trusted operator finalization: code and skills stay byte-for-byte fixed.

        This synchronously seals already recorded acceptance. It performs no
        model/runner operation and never invents human or AI acceptance. READY is
        a subsequent Engine transition guarded by these exact receipt identities.
        """
        run = self.engine.get_run(run_id)
        if run['cancel_requested'] or run['status'] not in {'AWAITING_HANDOFF', 'READY'}:
            raise DomainError('HANDOFF_NOT_AVAILABLE')
        if run.get('final_digest') != final_digest:
            raise DomainError('FINAL_VERSION_MISMATCH')
        if any(s['status'] in {'PENDING', 'RUNNING', 'FAILED'} for s in self.engine.list_steps(run_id)):
            raise DomainError('WORK_INCOMPLETE')
        latest: dict[str, dict] = {}
        for row in self.engine.verifications(run_id):
            if row['final_digest'] == final_digest:
                latest[row['kind']] = row
        if not REQUIRED_VERIFICATIONS <= {kind for kind, row in latest.items() if row['passed']}:
            raise DomainError('HANDOFF_NOT_VERIFIED')
        sealed_receipts = {kind: latest[kind]['id'] for kind in sorted(REQUIRED_VERIFICATIONS)}
        candidates = [a for a in self.engine.artifacts(run_id)
                      if a['kind'] == 'bundle' and a['verified'] and a['manifest'].get('final_digest') == final_digest]
        for existing in reversed(candidates):
            if existing['manifest'].get('status') == 'VERIFIED' and existing['manifest'].get('sealed_receipts') == sealed_receipts:
                return existing
        if not candidates:
            raise DomainError('HANDOFF_BUNDLE_MISSING')
        candidate = candidates[-1]
        archive = self._artifact_bytes({'key': candidate['object_key'], 'sha256': candidate['sha256'], 'size': candidate['size']})
        files = checked_archive_files(archive, max_file_bytes=64 * 1024 * 1024)
        executable_paths = archive_executables(archive)
        if 'manifest.json' not in files:
            raise DomainError('HANDOFF_MANIFEST_MISSING')
        manifest = json.loads(files['manifest.json'])
        expected_paths = {entry['path'] for entry in manifest['files']}
        if expected_paths != set(files) - {'manifest.json'}:
            raise DomainError('HANDOFF_MANIFEST_MISMATCH')
        for entry in manifest['files']:
            body = files[entry['path']]
            if len(body) != entry['size'] or hashlib.sha256(body).hexdigest() != entry['sha256']:
                raise DomainError('HANDOFF_MANIFEST_MISMATCH')
            if 'executable' in entry and entry['executable'] != (entry['path'] in executable_paths):
                raise DomainError('HANDOFF_MANIFEST_MISMATCH')
        receipts = self._public_receipts([latest[kind] for kind in sorted(REQUIRED_VERIFICATIONS)])
        receipt_document = {'version': 1, 'status': 'VERIFIED', 'final_digest': final_digest,
                            'sealed_receipts': sealed_receipts, 'receipts': receipts}
        files['docs/handoff-receipts.json'] = json_bytes(receipt_document) + b'\n'
        verification = json.loads(files.get('docs/verification.json', b'{}'))
        verification.update(status='VERIFIED', checks=receipts, sealed_receipts=sealed_receipts,
                            final_digest=final_digest)
        pending_text = 'Human and independent AI handover acceptance remain required.'
        verification['limitations'] = [v for v in verification.get('limitations', []) if v != pending_text]
        files['docs/verification.json'] = json_bytes(verification) + b'\n'
        manifest.update(status='VERIFIED', final_digest=final_digest, sealed_receipts=sealed_receipts)
        manifest['limitations'] = [v for v in manifest.get('limitations', []) if v != pending_text]
        manifest['files'] = [{'path': path, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
                              'executable': path in executable_paths}
                             for path, body in sorted(files.items()) if path != 'manifest.json']
        files['manifest.json'] = json_bytes(manifest) + b'\n'
        # Only metadata changes: a sealing error must not rewrite accepted code,
        # Git bundles, project instructions or the evidence used to verify them.
        sealed_bytes = code_zip(files, executable_paths=executable_paths)
        ref = self.store.put_bytes(f'runs/{run_id}/bundles/{hashlib.sha256(sealed_bytes).hexdigest()}.zip', sealed_bytes)
        return self.engine.record_artifact(run_id, 'bundle', ref.key, ref.sha256, ref.size,
            {'status': 'VERIFIED', 'final_digest': final_digest, 'sealed_receipts': sealed_receipts,
             'sealed_receipts_digest': digest(receipt_document), 'candidate_artifact_id': candidate['id'],
             'manifest': manifest}, True)

    async def cancel_run(self, run_id: str) -> bool:
        """A cancel request becomes CANCELLED only after provider stop readback."""
        run = self.engine.get_run(run_id)
        if not run['cancel_requested']:
            return False
        allocations: dict[str, str] = {}
        try:
            rows = self.engine.list_commands(run_id=run_id)
            known_usage = {row['id'] for row in self.engine.usage(run_id)}
            for row in rows:
                if row['request'].get('operation') == 'model' and row['request']['request_id'] not in known_usage:
                    self.engine.record_usage(run_id, row['request']['request_id'], row['request']['profile_version'],
                        {'usage_unknown': True, 'delivery_unknown': True, 'reason': 'CANCELLED_DURING_MODEL_REQUEST'},
                        row['attempt_id'])
                if row['request'].get('operation') != 'allocate_vm':
                    continue
                result = row.get('result') or {}
                if result.get('vm_id'):
                    allocations[result['vm_id']] = result.get('runner_endpoint', '')
                elif self.vm_provider is not None:
                    # Lost create response: the same idempotency identity returns
                    # the accepted allocation, not a fresh arbitrary VM.
                    value = await self.vm_provider.create(row['request']['allocation_id'], row['request']['purpose'])
                    allocations[value.vm_id] = value.runner_endpoint
            if allocations and self.vm_provider is None:
                return False
            for vm_id in allocations:
                value = await self.vm_provider.destroy(vm_id)
                deadline = time.monotonic() + self.limits.command_timeout_seconds
                while value.state != 'DESTROYED' and time.monotonic() < deadline:
                    await asyncio.sleep(self.limits.poll_seconds)
                    value = await self.vm_provider.status(vm_id)
                if value.state != 'DESTROYED':
                    return False
        except RunnerError:
            return False
        self.engine.confirm_cancel(run_id, stop_confirmed=True)
        return True

    async def reconcile(self, claim: dict) -> bool:
        """Fence the old generation, then read effects before allowing new work."""
        rows = self.engine.commands_for_attempt(claim['attempt']['id'])
        allocations = [r for r in rows if r['request'].get('operation') == 'allocate_vm']
        unknown_models = [r for r in rows if r['request'].get('operation') == 'model'
                          and r['status'] not in {'SUCCEEDED', 'FAILED', 'CANCELLED', 'STOPPED'}]
        if unknown_models:
            # A dead worker cannot cancel or read back a generic model API.
            # Preserve unknown delivery/cost, and require an explicit decision;
            # replaying the request would silently make another paid call.
            existing_usage = {u['id'] for u in self.engine.usage(claim['run']['id'])}
            for operation in unknown_models:
                request_id = operation['request']['request_id']
                if request_id not in existing_usage:
                    self.engine.record_usage(claim['run']['id'], request_id,
                        operation['request']['profile_version'],
                        {'usage_unknown': True, 'delivery_unknown': True, 'reason': 'WORKER_LOST_MODEL_RESPONSE'},
                        claim['attempt']['id'])
                self._observed(claim, operation['id'], 'STOPPED',
                               {'provider_outcome': 'UNKNOWN', 'usage_unknown': request_id not in existing_usage,
                                'local_dispatch_stopped': True})
            # The pipeline performs model proposals before allocating a VM in
            # each such stage. Fail closed if a future stage changes that rule.
            if allocations:
                return False
            self.engine.confirm_reconciliation(claim['attempt']['id'], claim['attempt']['generation'])
            self.engine.set_waiting(claim['attempt']['id'], claim['attempt']['generation'], 'NEEDS_INPUT',
                                    'MODEL_OUTCOME_UNKNOWN', {'request_ids': [r['request']['request_id'] for r in unknown_models]})
            return False
        if not allocations:
            self.engine.confirm_reconciliation(claim['attempt']['id'], claim['attempt']['generation'])
            return True
        from .adapters.runner import VMAllocation
        for row in allocations:
            if row['status'] == 'SUCCEEDED':
                allocation = VMAllocation.model_validate(row['result'])
            else:
                allocation = await self.vm_provider.create(row['request']['allocation_id'], row['request']['purpose'])
                self._observed(claim, row['id'], 'SUCCEEDED', allocation.model_dump())
            runner = self.runner_factory(allocation)
            workspace = stable_id(claim['attempt']['id'], 'workspace')
            try:
                await runner.authorize(workspace, claim['attempt']['id'], claim['attempt']['generation'])
                if any(r['request'].get('operation') in {'prepare', 'checkpoint'}
                       and r['status'] not in {'SUCCEEDED', 'FAILED', 'CANCELLED', 'STOPPED'} for r in rows):
                    # Lost metadata/file-transfer response has no independent
                    # readback contract. Stop its VM, then restore a confirmed
                    # checkpoint rather than guessing whether files are complete.
                    raise RunnerError('UNKNOWN', 'file operation requires restoration')
                for command in rows:
                    if not command['request'].get('argv') or command['status'] in {'SUCCEEDED', 'FAILED', 'CANCELLED', 'STOPPED'}:
                        continue
                    value = await runner.status(command['id'])
                    deadline = time.monotonic() + self.limits.command_timeout_seconds
                    while value.state in {'ACCEPTED', 'RUNNING'} and time.monotonic() < deadline:
                        await asyncio.sleep(self.limits.poll_seconds)
                        value = await runner.status(command['id'])
                    if value.state in {'UNKNOWN', 'ACCEPTED', 'RUNNING'}:
                        raise RunnerError('UNKNOWN', 'previous command did not terminate')
                    if value.request_hash != CommandRequest.model_validate(command['request']).content_hash():
                        raise RunnerError('INVALID_RESPONSE', 'previous command identity mismatch')
                    self._observed(claim, command['id'], value.state, value.model_dump())
            except RunnerError:
                stopped = await self.vm_provider.destroy(allocation.vm_id)
                deadline = time.monotonic() + self.limits.command_timeout_seconds
                while stopped.state != 'DESTROYED' and time.monotonic() < deadline:
                    await asyncio.sleep(self.limits.poll_seconds)
                    stopped = await self.vm_provider.status(allocation.vm_id)
                if stopped.state != 'DESTROYED':
                    return False
                self.engine.confirm_reconciliation(claim['attempt']['id'], claim['attempt']['generation'], stop_confirmed=True)
                self.engine.finish_attempt(claim['attempt']['id'], claim['attempt']['generation'],
                                           {'restore_from_checkpoint_required': True}, outcome='FAILED',
                                           reason='PREVIOUS_EXECUTION_UNKNOWN', retryable=True, execution_confirmed=True)
                return False
        self.engine.confirm_reconciliation(claim['attempt']['id'], claim['attempt']['generation'])
        return True
