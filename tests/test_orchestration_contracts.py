"""Compatibility and rejection controls for the composed pipeline boundary."""

import hashlib
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from v2c.adapters.runner import MTLSConfig, PrepareRequest, RunnerClient, VMAllocation
from v2c.diagnostics import failure_record
from v2c.engine import DomainError, Engine, digest
from v2c.orchestration.execution import ExecutionOperations
from v2c.orchestration.journal import OperationJournal
from v2c.orchestration.model import ModelOperations
from v2c.orchestration.sources import SourceArtifacts
from v2c.pipeline import (
    _APPLY_FILES,
    DTO,
    EnvironmentRecipe,
    PatchProposal,
    Pipeline,
    PipelineLimits,
    ProjectPlan,
    RecipeCommand,
    code_zip,
    json_bytes,
    stable_id,
)
from v2c.storage import LocalArtifactStore


@pytest.fixture
def claimed_run(tmp_path):
    engine = Engine(f"sqlite:///{tmp_path}/state.db")
    engine.init_schema()
    project = engine.create_project("operator", "contract fixture")
    snapshot = engine.create_snapshot(project["id"], {"synthetic": True})
    run = engine.create_run(project["id"], snapshot["id"], "contract-run")
    claim = engine.claim_next("contract-worker")
    assert claim is not None
    return engine, run, claim


@pytest.mark.asyncio
async def test_unknown_stage_rejected_without_dispatch_or_state_mutation(claimed_run, tmp_path):
    engine, run, claim = claimed_run
    pipeline = Pipeline(engine, LocalArtifactStore(tmp_path / "objects"))
    before = engine.get_run(run["id"])
    claim["step"]["kind"] = "_destroy"

    # A method name must never become an executable stage through reflection.
    with pytest.raises(DomainError, match="UNKNOWN_STEP_KIND"):
        await pipeline.execute(claim)
    assert engine.get_run(run["id"]) == before
    assert engine.list_commands(run_id=run["id"]) == []
    assert engine.usage(run["id"]) == []
    with pytest.raises(TypeError):
        pipeline.stages["_destroy"] = pipeline._destroy


def test_journal_reuses_identity_and_preserves_opaque_request(claimed_run):
    engine, run, claim = claimed_run
    journal = OperationJournal(engine, PipelineLimits())
    request = {
        "operation": "fixture",
        "generation": claim["attempt"]["generation"],
        "extension": {"omitted_by_nobody": None, "values": [False, 0, ""]},
    }
    command_id, first, existed = journal.prepare(claim, "fixture-operation", request)
    assert existed is False
    assert first["request"] == request
    assert first["request_hash"] == digest(request)

    # Readback of an existing operation keeps its original dispatch generation.
    repeated_id, repeated, existed = journal.prepare(
        claim, "fixture-operation", {**request, "generation": 999}
    )
    assert existed is True
    assert repeated_id == command_id
    assert repeated["request"] == request
    assert repeated["request_hash"] == first["request_hash"]
    assert len(engine.list_commands(run_id=run["id"])) == 1
    with pytest.raises(DomainError, match="COMMAND_ID_CONFLICT"):
        journal.prepare(claim, "fixture-operation", {**request, "extension": {}})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["command", "allocate", "destroy", "prepare", "model", "checkpoint"]
)
async def test_execution_failure_keeps_exception_state_and_operation_identity(
    claimed_run, boundary, tmp_path
):
    engine, run, claim = claimed_run
    error = OSError("sensitive provider detail must not enter diagnostics")
    limits = PipelineLimits()
    journal = OperationJournal(engine, limits)
    claim["run"]["limits"]["execution_limits"] = {
        "cpus": 1,
        "memory_mb": 256,
        "pids": 16,
        "disk_mb": 64,
        "max_output_bytes": 4096,
        "network": "none",
        "image": "fixture@sha256:" + "a" * 64,
    }

    class BrokenPort:
        is_test = True
        calls = 0

        async def execute(self, request):
            self.calls += 1
            raise error

        async def create(self, allocation_id, purpose):
            self.calls += 1
            raise error

        async def destroy(self, vm_id):
            self.calls += 1
            raise error

        async def prepare(self, request):
            self.calls += 1
            raise error

        async def generate(self, request):
            self.calls += 1
            raise error

        async def checkpoint(self, workspace_id):
            self.calls += 1
            raise error

    port = BrokenPort()
    operations = ExecutionOperations(
        engine, journal, limits, None, lambda: None, port, lambda _: port
    )
    allocation = VMAllocation(
        vm_id="fixture-vm", state="READY", runner_endpoint="https://fixture.invalid"
    )
    before = engine.get_run(run["id"])
    with pytest.raises(OSError) as caught:
        if boundary == "command":
            await operations.command(
                claim,
                port,
                "workspace",
                RecipeCommand(id="check", argv=["fixture"], timeout_seconds=10),
                "check",
            )
        elif boundary == "allocate":
            await operations.allocate(claim, "baseline")
        elif boundary == "prepare":
            await operations.prepare(claim, port, b"fixture", {})
        elif boundary == "model":
            await ModelOperations(engine, journal, port, limits).generate(
                claim, "fixture", "task", {}, DTO
            )
        elif boundary == "checkpoint":
            sources = SourceArtifacts(
                engine, LocalArtifactStore(tmp_path / "objects"), journal, limits, None
            )
            await sources.checkpoint(
                claim, port, "workspace", {}, key="fixture_checkpoint", expected_source=b"fixture"
            )
        else:
            operations.active[claim["attempt"]["id"]] = (allocation, port)
            await operations.destroy(claim, allocation)
    assert caught.value is error
    assert port.calls == 1
    after = engine.get_run(run["id"])
    # Intent updates timestamps; model dispatch also freezes the selected profile.
    ignored = {"updated_at", "limits"} if boundary == "model" else {"updated_at"}
    assert {key: value for key, value in after.items() if key not in ignored} == {
        key: value for key, value in before.items() if key not in ignored
    }
    commands = engine.list_commands(run_id=run["id"])
    assert len(commands) == 1 and commands[0]["status"] == "PREPARED"
    assert commands[0]["result"] is None
    record = failure_record(error, worker_id="contract-worker")
    if boundary in {"command", "prepare", "model", "checkpoint"}:
        key = {
            "command": "check",
            "prepare": "prepare",
            "model": "model_fixture",
            "checkpoint": "fixture_checkpoint",
        }[boundary]
        assert record["command_id"] == stable_id(claim["attempt"]["id"], key)
    else:
        expected = (
            stable_id(claim["attempt"]["id"], "baseline")
            if boundary == "allocate"
            else allocation.vm_id
        )
        assert record["allocation_id"] == expected
    if boundary == "destroy":
        assert operations.active[claim["attempt"]["id"]] == (allocation, port)
    assert "sensitive provider detail" not in str(record)


def test_refactor_keeps_baseline_operation_schema_and_archive_bytes():
    """Golden values captured from bea8639 before extraction, not recomputed here."""
    assert stable_id("attempt-123", "transform_0_check") == "c156255a-aa26-5726-a941-80158e00b46f"
    assert (
        hashlib.sha256(_APPLY_FILES.encode()).hexdigest()
        == "554fa009c7f29d473786028e0de3ff1469f1645767d41601130be24086f82d49"
    )
    expected_schemas = {
        EnvironmentRecipe: "9ef348ef07d30f694e8f0eec20d35338bc894c870e94dbdf06e2c71c49dc3ff3",
        ProjectPlan: "37ae68c9a3a51635e8eaa46cfa62eaba9f713a6df9f7984a01afb8c41a1a5e45",
        PatchProposal: "db3c496e9b3956ae206486cd51585c2c2dee75e986a0901e1c41fccf9334e054",
    }
    for schema, expected in expected_schemas.items():
        assert hashlib.sha256(json_bytes(schema.model_json_schema())).hexdigest() == expected
    archive = code_zip(
        {
            "repositories/main/app.py": b"print(42)\n",
            "repositories/main/run.sh": b"#!/bin/sh\npython3 app.py\n",
        },
        executable_paths={"repositories/main/run.sh"},
    )
    assert (
        hashlib.sha256(archive).hexdigest()
        == "8a1bff7867bf6d02bf5b8483500029e98b164ea5be0ea3843628125d40f9a531"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation, reply",
    [
        ("prepare", {"workspace_id": "workspace", "state": "READY", "archive_sha256": "a" * 64}),
        ("prepare", {"workspace_id": "workspace", "state": "READY", "generation": 1}),
        ("authorize", {"workspace_id": "workspace", "state": "READY", "generation": 1}),
        ("authorize", {"workspace_id": "workspace", "generation": 1}),
    ],
)
async def test_typed_runner_replies_preserve_fresh_replay_and_legacy_wire_shapes(operation, reply):
    expected = {**reply, "extension": {"retained_null": None}}
    config = MTLSConfig(
        endpoint="https://fixture.invalid",
        ca_file="unused",
        cert_file="unused",
        key_file="unused",
        bearer_secret="fixture-token-1234",
        timeout_seconds=1,
        max_response_bytes=4096,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=expected))
    ) as client:
        runner = RunnerClient(config, client)
        if operation == "prepare":
            actual = await runner.prepare(
                PrepareRequest(
                    workspace_id="workspace",
                    attempt_id="attempt",
                    generation=1,
                    archive_b64="",
                    archive_sha256="a" * 64,
                    input_manifest={},
                )
            )
        else:
            actual = await runner.authorize("workspace", "attempt", 1)
    assert actual == expected
    assert set(actual) == set(expected)


def test_type_checker_accepts_adapters_and_rejects_broken_claim_and_port(tmp_path):
    root = Path(__file__).resolve().parents[1]
    sample = tmp_path / "port_contract.py"
    prelude = """from typing import assert_type
from v2c.adapters.runner import RunnerClient, HTTPVMProvider
from v2c.config import Settings
from v2c.pipeline import Pipeline
from v2c.contracts.ports import RunnerPort, VMProviderPort, PipelineSettings
from v2c.contracts.workflow import RunClaim, InspectResult, BaselineResult, TransformResult, VerifyResult, PackageResult, HandoffResult, RecipePayload, CheckOutcome
from v2c.orchestration.sources import SourceArtifacts

def accept_runner(value: RunnerPort) -> None:
    pass

def valid_adapters(runner: RunnerClient, provider: HTTPVMProvider, settings: Settings) -> None:
    accept_runner(runner)
    vm: VMProviderPort = provider
    config: PipelineSettings = settings

def stage_results(pipeline: Pipeline, sources: SourceArtifacts) -> None:
    assert_type(pipeline._required_result("run", "inspect"), InspectResult)
    assert_type(pipeline._required_result("run", "baseline"), BaselineResult)
    assert_type(pipeline._required_result("run", "transform"), TransformResult)
    assert_type(pipeline._required_result("run", "verify"), VerifyResult)
    assert_type(pipeline._required_result("run", "package"), PackageResult)
    assert_type(pipeline._required_result("run", "ai_handoff"), HandoffResult)
    assert_type(sources.required_result("run", "verify"), VerifyResult)
    assert_type(pipeline._required_result("run", "inspect")["recipe"], RecipePayload)
    assert_type(pipeline._required_result("run", "verify")["checks"], list[CheckOutcome])

def generation(claim: RunClaim) -> int:
    return claim["attempt"]["generation"]
"""

    def check(source):
        sample.write_text(source)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--strict",
                "--follow-imports=silent",
                "--no-incremental",
                "--cache-dir",
                str(tmp_path / "mypy-cache"),
                str(sample),
            ],
            cwd=root,
            text=True,
            capture_output=True,
        )

    positive = check(prelude)
    assert positive.returncode == 0, positive.stdout + positive.stderr
    negative = check(
        prelude
        + """
def misspelled_claim(claim: RunClaim) -> object:
    return claim["atempt"]

def misspelled_result(pipeline: Pipeline) -> object:
    return pipeline._required_result("run", "verify")["checkpiont"]

def misspelled_recipe(pipeline: Pipeline) -> object:
    return pipeline._required_result("run", "inspect")["recipe"]["revovery_checks"]

def misspelled_outcome(pipeline: Pipeline) -> object:
    return pipeline._required_result("run", "verify")["checks"][0]["exit_cod"]

class IncompleteRunner:
    async def checkpoint(self, workspace_id: str) -> bytes:
        return b"fixture"

accept_runner(IncompleteRunner())
"""
    )
    assert negative.returncode == 1, negative.stdout + negative.stderr
    assert "[typeddict-item]" in negative.stdout
    assert "[arg-type]" in negative.stdout
    assert "IncompleteRunner" in negative.stdout
