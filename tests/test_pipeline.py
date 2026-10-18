"""Controller integration with deterministic ports, never real VM qualification."""
import base64
import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from v2c.adapters.model import ModelProfile, ModelResult, ModelUsage
from v2c.adapters.runner import CommandRequest, CommandResult, VMAllocation
from v2c.engine import DomainError, Engine
from v2c.intake import safe_import_archive
from v2c.pipeline import (EnvironmentRecipe, FileChange, PatchProposal, Pipeline, InputRequired,
                          archive_context, archive_executables, assert_source_unchanged,
                          code_zip, snapshot_archive, stable_id)
from v2c.storage import LocalArtifactStore
from v2c.worker import run_once


ORIGINAL = b'def answer():\n    return 42\n'
REFACTORED = b'def answer():\n    """The supported answer contract."""\n    return 42\n'
HELDOUT = REFACTORED + b'\ndef twice_answer():\n    return answer() * 2\n'
PATH = 'repositories/main/app.py'


def zip_bytes(files):
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return body.getvalue()


def read_zip(body):
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not archive.getinfo(name).is_dir()}


RECIPE = {'runtime': 'synthetic test port', 'setup': [], 'restore': [],
          'tests': [{'id': 'regression', 'argv': ['fixture-regression'], 'cwd': '.', 'timeout_seconds': 10}],
          'recovery_checks': [{'id': 'state', 'argv': ['fixture-state'], 'cwd': '.', 'timeout_seconds': 10}],
          'state_strategy': 'stateless', 'services': [], 'limitations': []}
PLAN = {'summary': 'Document one preserved function contract',
        'findings': [{'id': 'f1', 'criterion': 'human maintainability', 'reason': 'Function contract lacks documentation.',
                      'evidence_files': [PATH]}],
        'steps': [{'id': 'document-answer', 'title': 'Document answer', 'files': [PATH],
                   'reason': 'Expose the existing behavior.', 'preserve': ['answer() returns 42'], 'checks': ['regression']}],
        'limitations': []}


def patch(before, after):
    return {'explanation': 'Preserve answer and document its contract.', 'files': [{'path': PATH,
            'old_sha256': hashlib.sha256(before).hexdigest(), 'content_b64': base64.b64encode(after).decode()}]}


class FixtureModel:
    is_test = True
    def __init__(self):
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        if request.task.startswith('Infer'):
            value = RECIPE
        elif request.task.startswith('Diagnose'):
            value = PLAN
        elif request.task.startswith('Implement'):
            value = patch(ORIGINAL, REFACTORED)
        else:
            value = patch(REFACTORED, HELDOUT)
        return ModelResult(text=json.dumps(value), structured=value, finish_reason='stop', usage=ModelUsage())


class FixtureRunner:
    is_test = True
    def __init__(self, allocation, *, lost_response=False):
        self.allocation = allocation
        self.files = {}
        self.commands = {}
        self.executions = 0
        self.lost_response = lost_response

    async def prepare(self, request):
        self.files = read_zip(base64.b64decode(request.archive_b64))
        return {'workspace_id': request.workspace_id, 'state': 'READY', 'archive_sha256': request.archive_sha256}

    async def authorize(self, workspace_id, attempt_id, generation):
        return {'workspace_id': workspace_id, 'generation': generation}

    async def execute(self, request):
        from v2c.adapters.runner import RunnerError
        if request.command_id in self.commands:
            return self.commands[request.command_id]
        self.executions += 1
        code = 0
        if request.argv[0] == 'python3':
            # Emulate the trusted file effect, without executing generated code
            # on the test/control host.
            changes = json.loads(base64.b64decode(request.argv[3]))
            for change in changes:
                if change['content_b64'] is None:
                    del self.files[change['path']]
                else:
                    self.files[change['path']] = base64.b64decode(change['content_b64'])
        elif request.argv[0] == 'fixture-regression':
            assert self.files[PATH] in {ORIGINAL, REFACTORED, HELDOUT}
        elif request.argv[0] == 'fixture-hidden':
            code = 0 if self.files[PATH] == HELDOUT else 1
        elif request.argv[0] == 'fixture-state':
            code = 0 if PATH in self.files else 1
        else:
            raise AssertionError('fixture received an unexpected command')
        result = CommandResult(command_id=request.command_id, request_hash=request.content_hash(),
                               state='SUCCEEDED' if code == 0 else 'FAILED', exit_code=code,
                               output='fixture evidence', stop_confirmed=True)
        self.commands[request.command_id] = result
        if self.lost_response:
            self.lost_response = False
            raise RunnerError('TRANSPORT_OR_TIMEOUT', 'response lost after effect', delivery_unknown=True)
        return result

    async def status(self, command_id):
        return self.commands[command_id]

    async def cancel(self, command_id):
        return self.commands[command_id]

    async def checkpoint(self, workspace_id):
        return zip_bytes(self.files)


class FixtureVMs:
    def __init__(self):
        self.allocations = {}
        self.runners = {}
        self.purposes = []
        self.destroyed = []

    async def create(self, allocation_id, purpose):
        if allocation_id not in self.allocations:
            value = VMAllocation(vm_id=allocation_id, runner_endpoint='https://fixture.invalid/' + allocation_id, state='READY')
            self.allocations[allocation_id] = value
            self.runners[allocation_id] = FixtureRunner(value)
            self.purposes.append(purpose)
        return self.allocations[allocation_id]

    async def status(self, vm_id):
        return self.allocations[vm_id]

    async def destroy(self, vm_id):
        value = self.allocations[vm_id].model_copy(update={'state': 'DESTROYED'})
        self.allocations[vm_id] = value
        self.destroyed.append(vm_id)
        return value

    def runner(self, allocation):
        return self.runners[allocation.vm_id]


@pytest.fixture
def setup(tmp_path):
    engine = Engine('sqlite:///' + str(tmp_path / 'state.db'))
    engine.init_schema()
    project = engine.create_project('operator', 'Unknown fixture')
    archive = tmp_path / 'input.zip'
    archive.write_bytes(zip_bytes({'app.py': ORIGINAL}))
    source = safe_import_archive(archive, tmp_path / 'source', project_id=project['id'])
    snapshot = engine.create_snapshot(project['id'], source.manifest)
    execution_limits = {'cpus': 1, 'memory_mb': 256, 'pids': 16, 'disk_mb': 64,
                        'max_output_bytes': 4096, 'network': 'none', 'image': 'fixture@sha256:' + 'a' * 64}
    run = engine.create_run(project['id'], snapshot['id'], 'fixture-run', {'execution_limits': execution_limits})
    settings = SimpleNamespace(work_root=tmp_path / 'work', execution_limits=execution_limits,
                               command_timeout_seconds=10, run_timeout_seconds=3600, model_max_input_bytes=262144)
    store, model, vms = LocalArtifactStore(tmp_path / 'objects'), FixtureModel(), FixtureVMs()
    pipeline = Pipeline(engine, store, model, settings=settings, vm_provider=vms, runner_factory=vms.runner)
    return engine, pipeline, model, vms, run


async def advance_to_plan(engine, pipeline, run):
    for _ in range(3):
        assert await run_once(engine, pipeline, 'test-worker')
    assert engine.get_run(run['id'])['status'] == 'AWAITING_PLAN_APPROVAL'
    current = engine.get_plan(run['id'])
    engine.decide_plan(run['id'], run['project_id'], run['snapshot_id'], current['revision'], True, 'operator')


async def advance_to_handoff(engine, pipeline, run):
    await advance_to_plan(engine, pipeline, run)
    for _ in range(4):
        assert await run_once(engine, pipeline, 'test-worker')
    assert engine.get_run(run['id'])['reason'] == 'HELD_OUT_TASK_REQUIRED'


@pytest.mark.asyncio
async def test_full_candidate_requires_independent_ai_and_human(setup):
    engine, pipeline, model, vms, run = setup
    await advance_to_handoff(engine, pipeline, run)
    candidate_digest = engine.get_run(run['id'])['final_digest']
    assert candidate_digest
    assert vms.purposes == ['baseline', 'transform', 'final_verification']
    assert len(set(vms.destroyed)) == 3
    assert len(engine.artifacts(run['id'])) >= 3
    assert {v['kind'] for v in engine.verifications(run['id'])} == {'recovery', 'final_regression'}
    with pytest.raises(DomainError, match='HANDOFF'):
        engine.complete_handoff(run['id'], candidate_digest)

    step = next(s for s in engine.list_steps(run['id']) if s['kind'] == 'ai_handoff')
    task = 'Add twice_answer() returning twice the supported answer.'
    engine.supply_input(run['id'], step['id'], {'task': task,
        'checks': [{'id': 'owner_expectation', 'argv': ['fixture-hidden'], 'cwd': '.', 'timeout_seconds': 10}]})
    assert await run_once(engine, pipeline, 'independent-worker')
    assert engine.get_run(run['id'])['status'] == 'AWAITING_HANDOFF'
    # Expected checks remain outside all model contexts, including the new AI.
    assert all('fixture-hidden' not in request.context for request in model.requests)
    assert all(task not in request.context for request in model.requests[:-1])
    assert task in model.requests[-1].context
    assert 'project-skills/' in model.requests[-1].context
    assert vms.purposes[-1] == 'ai_handoff'
    assert len(set(vms.destroyed)) == 4
    assert engine.get_run(run['id'])['final_digest'] == candidate_digest
    engine.record_verification(run['id'], 'human_handoff', True,
                               {'actor': 'independent fixture reviewer', 'notes': 'Synthetic controller test only'}, candidate_digest)
    candidate = next(a for a in engine.artifacts(run['id']) if a['kind'] == 'bundle')
    before = read_zip(pipeline.store.get_bytes(candidate['object_key']))
    sealed = pipeline.seal_handoff_bundle(run['id'], candidate_digest)
    after = read_zip(pipeline.store.get_bytes(sealed['object_key']))
    assert sealed['manifest']['status'] == 'VERIFIED'
    assert pipeline.seal_handoff_bundle(run['id'], candidate_digest)['id'] == sealed['id']
    for path in before:
        if path.startswith(('code/', 'git/', 'project-skills/')):
            assert before[path] == after[path]
    assert 'fixture-hidden' not in after['docs/handoff-receipts.json'].decode()
    assert json.loads(after['manifest.json'])['status'] == 'VERIFIED'
    assert set(json.loads(after['docs/handoff-receipts.json'])['sealed_receipts']) == {'recovery', 'final_regression', 'ai_handoff', 'human_handoff'}
    engine.complete_handoff(run['id'], candidate_digest)
    assert engine.get_run(run['id'])['status'] == 'READY'


@pytest.mark.asyncio
async def test_unconfigured_model_waits_without_fake_plan_or_vm(setup):
    engine, pipeline, _, vms, run = setup
    pipeline.model = None
    await run_once(engine, pipeline, 'test-worker')
    state = engine.get_run(run['id'])
    assert (state['status'], state['reason']) == ('NEEDS_INPUT', 'MODEL_PROFILE_REQUIRED')
    assert engine.get_plan(run['id']) is None
    assert not vms.allocations
    assert engine.list_steps(run['id'])[0]['attempts_used'] == 1
    assert not await run_once(engine, pipeline, 'test-worker')


@pytest.mark.asyncio
async def test_missing_vm_provider_stops_after_static_inspection(setup):
    engine, pipeline, _, vms, run = setup
    pipeline.vm_provider = None
    await run_once(engine, pipeline, 'test-worker')
    await run_once(engine, pipeline, 'test-worker')
    assert engine.get_run(run['id'])['reason'] == 'VM_PROVIDER_REQUIRED'
    assert engine.list_steps(run['id'])[1]['attempts_used'] == 1
    assert not vms.allocations


@pytest.mark.asyncio
async def test_same_version_changed_endpoint_pauses_before_any_new_model_dispatch(setup):
    engine, pipeline, model, _, run = setup
    model.profile = ModelProfile(version='operator-profile-1', adapter_type='openai-compatible',
        endpoint='https://first.example.invalid/v1', model='fixture-model', secret_ref='env:TEST_ONLY_KEY',
        capabilities=frozenset({'text', 'structured'}), max_input_bytes=262144, max_output_tokens=8192,
        max_response_bytes=1048576, timeout_seconds=10)
    await run_once(engine, pipeline, 'test-worker')
    bound = engine.get_run(run['id'])['limits']['model_profile']
    assert bound['endpoint'] == 'https://first.example.invalid/v1'
    operation = next(c for c in engine.list_commands(run_id=run['id']) if c['request'].get('operation') == 'model')
    assert operation['request']['profile_manifest'] == bound
    assert len(operation['request']['profile_hash']) == 64
    model.profile = model.profile.model_copy(update={'endpoint': 'https://silently-changed.example.invalid/v1'})
    await run_once(engine, pipeline, 'test-worker')  # Reproduce using the already frozen recipe.
    await run_once(engine, pipeline, 'test-worker')  # Planning must not use a silently replaced model.
    assert engine.get_run(run['id'])['reason'] == 'MODEL_PROFILE_CHANGED'
    assert len(model.requests) == 1
    assert engine.get_run(run['id'])['limits']['model_profile'] == bound
    assert not await run_once(engine, pipeline, 'test-worker')


@pytest.mark.asyncio
async def test_already_maintainable_project_can_proceed_without_invented_patch(setup):
    engine, pipeline, model, vms, run = setup
    original = model.generate
    async def no_changes(request):
        if request.task.startswith('Diagnose'):
            model.requests.append(request)
            plan = {**PLAN, 'summary': 'No evidenced change is necessary.', 'findings': [], 'steps': []}
            return ModelResult(text=json.dumps(plan), structured=plan, finish_reason='stop')
        return await original(request)
    model.generate = no_changes
    await advance_to_plan(engine, pipeline, run)
    for _ in range(3):
        assert await run_once(engine, pipeline, 'test-worker')
    assert engine.get_run(run['id'])['reason'] == 'HELD_OUT_TASK_REQUIRED'
    assert not [s for s in engine.list_steps(run['id']) if s['kind'] == 'transform']
    assert not [request for request in model.requests if request.task.startswith('Implement')]
    assert vms.purposes == ['baseline', 'final_verification']
    assert {v['kind'] for v in engine.verifications(run['id'])} == {'final_regression', 'recovery'}
    assert engine.get_run(run['id'])['status'] != 'READY'
    candidate = next(a for a in engine.artifacts(run['id']) if a['kind'] == 'bundle')
    bundle = read_zip(pipeline.store.get_bytes(candidate['object_key']))
    assert bundle['code/main/app.py'] == ORIGINAL


def test_replacement_and_checked_checkpoint_preserve_executable_bit():
    source = code_zip({'repositories/main/run.sh': b'#!/bin/sh\nexit 0\n'},
                      executable_paths={'repositories/main/run.sh'})
    patch = PatchProposal(explanation='Document script', files=[FileChange(path='repositories/main/run.sh',
        old_sha256=hashlib.sha256(b'#!/bin/sh\nexit 0\n').hexdigest(),
        content_b64=base64.b64encode(b'#!/bin/sh\n# documented\nexit 0\n').decode())])
    changed = Pipeline._patched_archive(source, patch)
    assert archive_executables(changed) == {'repositories/main/run.sh'}
    checkpoint = assert_source_unchanged(changed, changed)
    assert archive_executables(checkpoint) == {'repositories/main/run.sh'}


@pytest.mark.asyncio
async def test_lost_command_response_uses_readback_without_second_execution(setup):
    engine, pipeline, _, vms, run = setup
    await run_once(engine, pipeline, 'test-worker')
    original_factory = vms.runner
    def lost_response(allocation):
        runner = original_factory(allocation)
        runner.lost_response = True
        return runner
    pipeline.runner_factory = lost_response
    await run_once(engine, pipeline, 'test-worker')
    baseline = engine.list_steps(run['id'])[1]
    assert baseline['status'] == 'SUCCEEDED'
    runner = next(iter(vms.runners.values()))
    assert runner.executions == 2  # Recovery check + regression, one effect each.
    assert len(runner.commands) == 2
    assert baseline['attempts_used'] == 1


@pytest.mark.asyncio
async def test_five_failed_project_checks_never_publish_candidate(setup):
    engine, pipeline, _, vms, run = setup
    await advance_to_plan(engine, pipeline, run)
    original_factory = vms.runner
    def always_failing_check(allocation):
        runner = original_factory(allocation)
        execute = runner.execute
        async def failed(request):
            result = await execute(request)
            if request.argv == ['fixture-regression']:
                result = result.model_copy(update={'state': 'FAILED', 'exit_code': 1, 'output': 'PASS PASS PASS'})
                runner.commands[request.command_id] = result
            return result
        runner.execute = failed
        return runner
    pipeline.runner_factory = always_failing_check
    for _ in range(5):
        assert await run_once(engine, pipeline, 'test-worker')
    state = engine.get_run(run['id'])
    assert (state['status'], state['reason']) == ('FAILED', 'RETRY_LIMIT_EXHAUSTED')
    transform = next(s for s in engine.list_steps(run['id']) if s['kind'] == 'transform')
    assert transform['attempts_used'] == 5
    assert not await run_once(engine, pipeline, 'test-worker')
    assert not engine.verifications(run['id'])
    assert not [a for a in engine.artifacts(run['id']) if a['kind'] == 'bundle']
    assert vms.purposes.count('transform') == 5
    assert len(set(vms.destroyed)) == 6  # Source baseline plus five stopped attempts.


@pytest.mark.asyncio
async def test_tests_cannot_replace_the_source_then_claim_success(setup):
    engine, pipeline, _, vms, run = setup
    await run_once(engine, pipeline, 'test-worker')
    original_factory = vms.runner
    def mutating_test(allocation):
        runner = original_factory(allocation)
        execute = runner.execute
        async def mutate(request):
            result = await execute(request)
            if request.argv == ['fixture-regression']:
                runner.files[PATH] = b'print("PASS")\n'
            return result
        runner.execute = mutate
        return runner
    pipeline.runner_factory = mutating_test
    await run_once(engine, pipeline, 'test-worker')
    assert engine.get_run(run['id'])['reason'] == 'SOURCE_MUTATED_BY_EXECUTION'
    assert not engine.verifications(run['id'])
    assert engine.get_plan(run['id']) is None


@pytest.mark.asyncio
async def test_schema_fixture_rebuild_after_patch_precedes_project_checks(setup):
    engine, pipeline, model, vms, run = setup
    original_generate = model.generate
    recipe = {**RECIPE, 'state_strategy': 'rebuild_from_fixtures',
              'restore': [{'id': 'schema_fixtures', 'argv': ['fixture-rebuild-state'], 'cwd': '.', 'timeout_seconds': 10}]}
    async def stateful(request):
        if request.task.startswith('Infer'):
            model.requests.append(request)
            return ModelResult(text=json.dumps(recipe), structured=recipe, finish_reason='stop')
        return await original_generate(request)
    model.generate = stateful
    original_factory = vms.runner
    def stateful_runner(allocation):
        runner = original_factory(allocation)
        original_execute = runner.execute
        runner.schema_version = None
        runner.rebuild_count = 0
        async def execute(request):
            if request.argv == ['fixture-rebuild-state']:
                runner.schema_version = runner.files[PATH]
                runner.rebuild_count += 1
                result = CommandResult(command_id=request.command_id, request_hash=request.content_hash(),
                                       state='SUCCEEDED', exit_code=0, stop_confirmed=True)
                runner.commands[request.command_id] = result
                return result
            result = await original_execute(request)
            if request.argv == ['fixture-regression'] and runner.schema_version != runner.files[PATH]:
                result = result.model_copy(update={'state': 'FAILED', 'exit_code': 1, 'output': 'schema does not match current migration'})
                runner.commands[request.command_id] = result
            return result
        runner.execute = execute
        return runner
    pipeline.runner_factory = stateful_runner
    await advance_to_plan(engine, pipeline, run)
    assert await run_once(engine, pipeline, 'test-worker')
    transform = next(s for s in engine.list_steps(run['id']) if s['kind'] == 'transform')
    assert transform['status'] == 'SUCCEEDED'
    allocation_id = next(key for key in vms.runners if vms.runners[key].rebuild_count == 2)
    transformed_runner = vms.runners[allocation_id]
    assert transformed_runner.schema_version == REFACTORED
    assert transform['result']['post_patch_recovery']
    for _ in range(3):
        assert await run_once(engine, pipeline, 'test-worker')
    handoff = next(s for s in engine.list_steps(run['id']) if s['kind'] == 'ai_handoff')
    engine.supply_input(run['id'], handoff['id'], {'task': 'Add twice_answer() returning twice the answer.',
        'checks': [{'id': 'expected_function', 'argv': ['fixture-hidden'], 'cwd': '.', 'timeout_seconds': 10}]})
    assert await run_once(engine, pipeline, 'new-maintainer-worker')
    assert engine.get_run(run['id'])['status'] == 'AWAITING_HANDOFF'
    maintainer_vm = next(key for key, value in vms.allocations.items()
                         if vms.runners[key].schema_version == HELDOUT)
    assert vms.runners[maintainer_vm].rebuild_count == 2


@pytest.mark.asyncio
async def test_out_of_scope_patch_does_not_reach_runner(setup):
    engine, pipeline, model, vms, run = setup
    await advance_to_plan(engine, pipeline, run)
    original = model.generate
    async def scope_violation(request):
        if request.task.startswith('Implement'):
            value = patch(ORIGINAL, REFACTORED)
            value['files'][0]['path'] = 'repositories/main/unapproved.py'
            return ModelResult(text=json.dumps(value), structured=value, finish_reason='stop')
        return await original(request)
    model.generate = scope_violation
    await run_once(engine, pipeline, 'test-worker')
    assert engine.get_run(run['id'])['reason'] == 'PATCH_OUTSIDE_APPROVED_SCOPE'
    assert vms.purposes == ['baseline']


@pytest.mark.asyncio
async def test_malformed_paid_response_keeps_known_usage(setup):
    engine, pipeline, model, _, run = setup
    async def malformed(request):
        return ModelResult(text='invalid proposal', structured={'unexpected': True}, finish_reason='stop',
                           provider_request_id='fixture-paid-response',
                           usage=ModelUsage(input_tokens=37, output_tokens=12, total_tokens=49))
    model.generate = malformed
    await run_once(engine, pipeline, 'test-worker')
    usage = engine.usage(run['id'])
    assert len(usage) == 1
    assert usage[0]['usage']['usage']['total_tokens'] == 49
    assert usage[0]['usage']['provider_request_id'] == 'fixture-paid-response'
    assert engine.get_run(run['id'])['reason'] == 'INVALID_RESPONSE'


@pytest.mark.asyncio
async def test_cancel_after_response_retains_known_usage(setup):
    engine, pipeline, model, _, run = setup
    async def cancelled_after_response(request):
        engine.request_cancel(run['id'])
        return ModelResult(text=json.dumps(RECIPE), structured=RECIPE, finish_reason='stop',
                           provider_request_id='fixture-completed-before-cancel',
                           usage=ModelUsage(total_tokens=53))
    model.generate = cancelled_after_response
    await run_once(engine, pipeline, 'test-worker')
    assert engine.get_run(run['id'])['status'] == 'CANCELLED'
    assert engine.usage(run['id'])[0]['usage']['usage']['total_tokens'] == 53


@pytest.mark.asyncio
async def test_crash_during_model_call_waits_with_unknown_cost_without_replay(setup):
    engine, pipeline, model, _, run = setup
    clock = [engine.clock()]
    engine.clock = lambda: clock[0]
    claim = engine.claim_next('worker-that-died', lease_seconds=1)
    request_id = 'fixture_unknown_paid_request'
    engine.prepare_command(claim['attempt']['id'], claim['attempt']['generation'], 'fixture_model_dispatch',
        {'operation': 'model', 'request_id': request_id, 'profile_version': 'fixture-profile'})
    clock[0] += 2
    assert await run_once(engine, pipeline, 'recovery-worker', lease_seconds=10)
    state = engine.get_run(run['id'])
    assert (state['status'], state['reason']) == ('NEEDS_INPUT', 'MODEL_OUTCOME_UNKNOWN')
    assert not model.requests
    assert engine.list_steps(run['id'])[0]['attempts_used'] == 1
    usage = engine.usage(run['id'])
    assert usage[0]['id'] == request_id
    assert usage[0]['usage']['usage_unknown'] is True
    assert usage[0]['usage']['delivery_unknown'] is True
    assert not await run_once(engine, pipeline, 'recovery-worker')


@pytest.mark.asyncio
async def test_cancel_of_unobserved_model_dispatch_preserves_possible_cost(setup):
    engine, pipeline, _, _, run = setup
    claim = engine.claim_next('worker-that-lost-response')
    engine.prepare_command(claim['attempt']['id'], claim['attempt']['generation'], 'unobserved_model',
        {'operation': 'model', 'request_id': 'unobserved_paid_request', 'profile_version': 'fixture-profile'})
    engine.request_cancel(run['id'])
    assert await pipeline.cancel_run(run['id'])
    assert engine.get_run(run['id'])['status'] == 'CANCELLED'
    usage = engine.usage(run['id'])
    assert usage[0]['id'] == 'unobserved_paid_request'
    assert usage[0]['usage']['usage_unknown']
    assert usage[0]['usage']['delivery_unknown']


@pytest.mark.asyncio
async def test_crash_after_project_effect_resumes_same_attempt_by_command_id(setup):
    engine, pipeline, _, vms, run = setup
    await run_once(engine, pipeline, 'first-worker')  # Static inspection + frozen recipe.
    clock = [engine.clock()]
    engine.clock = lambda: clock[0]
    claim = engine.claim_next('worker-that-died', lease_seconds=1)
    _, runner = await pipeline._allocate(claim, 'baseline')
    inspected = pipeline._required_result(run['id'], 'inspect')
    workspace = await pipeline._prepare(claim, runner, pipeline._artifact_bytes(inspected['source_archive']), RECIPE)
    command = CommandRequest(command_id=stable_id(claim['attempt']['id'], 'baseline_recovery_0_state'),
        workspace_id=workspace, attempt_id=claim['attempt']['id'], generation=claim['attempt']['generation'],
        argv=['fixture-state'], cwd='.', timeout_seconds=10)
    engine.prepare_command(claim['attempt']['id'], claim['attempt']['generation'], command.command_id, command.model_dump())
    await runner.execute(command)  # Effect accepted; worker dies before saving the response.
    pipeline._active.clear()  # Process memory is gone; durable journals remain.
    clock[0] += 2
    assert await run_once(engine, pipeline, 'recovery-worker', lease_seconds=10)
    baseline = next(s for s in engine.list_steps(run['id']) if s['kind'] == 'baseline')
    assert baseline['status'] == 'SUCCEEDED'
    assert baseline['attempts_used'] == 1
    assert baseline['generation'] == 2
    assert runner.executions == 2  # Existing recovery effect + one subsequent regression.
    assert len(vms.allocations) == 1
    assert engine.get_command(command.command_id)['status'] == 'SUCCEEDED'


def test_context_secret_is_blocked_not_forwarded():
    body = zip_bytes({'repositories/main/.env': b'TOKEN=secret-value-for-production'})
    with pytest.raises(InputRequired, match='SOURCE_SECRET_REVIEW_REQUIRED'):
        archive_context(body, max_bytes=4096)


def test_declared_postgres_requires_rebuild_recipe_and_rejects_other_services(setup):
    _, pipeline, _, _, _ = setup
    recipe = {**RECIPE, 'services': [{'name': 'testdb', 'kind': 'postgres', 'database': 'synthetic', 'user': 'project'}],
              'state_strategy': 'rebuild_from_fixtures',
              'restore': [{'id': 'migrations_fixtures', 'argv': ['fixture-restore'], 'cwd': '.', 'timeout_seconds': 10}]}
    pipeline._validate_recipe(recipe)
    parsed = EnvironmentRecipe.model_validate(recipe)
    assert parsed.services[0].env_var == 'DATABASE_URL'
    with pytest.raises(InputRequired, match='TEST_STATE_RESTORE_RECIPE_REQUIRED'):
        pipeline._validate_recipe({**recipe, 'restore': []})
    with pytest.raises(InputRequired, match='TEST_STATE_RESTORE_RECIPE_REQUIRED'):
        pipeline._validate_recipe({**recipe, 'state_strategy': 'stateless'})
    with pytest.raises(ValidationError):
        EnvironmentRecipe.model_validate({**recipe, 'services': [{'name': 'prod', 'kind': 'redis', 'database': 'x', 'user': 'x'}]})


@pytest.mark.asyncio
async def test_cancelled_run_waits_for_confirmed_provider_destruction(setup):
    engine, pipeline, _, vms, run = setup
    await run_once(engine, pipeline, 'test-worker')
    # Complete baseline has an allocation. Stop refusal must not produce CANCELLED.
    await run_once(engine, pipeline, 'test-worker')
    engine.request_cancel(run['id'])
    actual_destroy = vms.destroy
    async def unknown(vm_id):
        return vms.allocations[vm_id].model_copy(update={'state': 'UNKNOWN'})
    vms.destroy = unknown
    actual_status = vms.status
    vms.status = unknown
    pipeline.limits = pipeline.limits.__class__(command_timeout_seconds=0, run_timeout_seconds=3600)
    assert not await pipeline.cancel_run(run['id'])
    assert engine.get_run(run['id'])['status'] != 'CANCELLED'
    vms.destroy, vms.status = actual_destroy, actual_status
    assert await pipeline.cancel_run(run['id'])
    assert engine.get_run(run['id'])['status'] == 'CANCELLED'
