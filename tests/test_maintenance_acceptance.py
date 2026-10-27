"""Independent wire compatibility evidence captured before implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from v2c import worker
from v2c.engine import Engine
from v2c.orchestration.common import PipelineLimits, stable_id
from v2c.orchestration.execution import ExecutionOperations
from v2c.orchestration.journal import OperationJournal


@pytest.mark.parametrize("contract", ["operator-wire", "identity-wire"])
def test_wire_matches_released_baseline(contract: str) -> None:
    root = Path(__file__).resolve().parents[1]
    fixtures = root / "tests" / "fixtures" / contract
    expected = json.loads((fixtures / "bea8639.json").read_text())
    environment = {key: value for key, value in os.environ.items() if not key.startswith("V2C_")}
    environment["PYTHONPATH"] = str(root / "src")
    result = subprocess.run(
        [sys.executable, str(fixtures / "probe.py")],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    actual = json.loads(result.stdout)
    assert actual.keys() == expected.keys()
    for scenario in expected:
        assert actual[scenario] == expected[scenario], scenario


@pytest.mark.asyncio
async def test_real_worker_logs_failed_durable_operation_across_task_boundary(tmp_path, caplog):
    """A child-task failure must identify the existing intent, without replay."""
    engine = Engine(f"sqlite:///{tmp_path / 'fault.db'}", clock=lambda: 1000.0)
    engine.init_schema()
    project = engine.create_project("qa", "Diagnostic acceptance")
    snapshot = engine.create_snapshot(project["id"], {})
    run = engine.create_run(
        project["id"],
        snapshot["id"],
        "diagnostic-acceptance",
        limits={
            "execution_limits": {
                "cpus": 1,
                "memory_mb": 256,
                "pids": 16,
                "disk_mb": 64,
                "max_output_bytes": 4096,
                "network": "none",
                "image": "fixture@sha256:" + "a" * 64,
            }
        },
    )
    stop = asyncio.Event()
    dispatches = []
    claims = []
    secret = "synthetic-provider-secret-never-log"

    class BrokenRunner:
        async def prepare(self, request):
            dispatches.append(request)
            raise OSError(f"https://operator:{secret}@example.invalid")

    runner = BrokenRunner()
    limits = PipelineLimits()
    operations = ExecutionOperations(
        engine,
        OperationJournal(engine, limits),
        limits,
        None,
        lambda: None,
        object(),
        lambda _: runner,
    )

    class FailingPipeline:
        async def execute(self, claim):
            claims.append(claim)
            try:
                await operations.prepare(claim, runner, b"synthetic archive", {})
            finally:
                stop.set()

    try:
        with caplog.at_level(logging.ERROR, logger="v2c.worker"):
            await worker.serve(engine, FailingPipeline(), worker_id="qa-worker", stop=stop)
        assert len(claims) == 1 and len(dispatches) == 1
        attempt = claims[0]["attempt"]
        record = json.loads(caplog.records[-1].message)
        assert record["error_class"] == "OSError"
        assert record["run_id"] == run["id"]
        assert record["attempt_id"] == attempt["id"]
        assert record["generation"] == attempt["generation"]
        assert record["command_id"] == stable_id(attempt["id"], "prepare")
        assert secret not in caplog.text
        commands = engine.list_commands(run_id=run["id"])
        assert len(commands) == 1
        assert commands[0]["status"] == "PREPARED" and commands[0]["result"] is None
        assert engine.get_run(run["id"])["status"] == "RUNNING"
        assert engine.list_steps(run["id"])[0]["attempts_used"] == 1
    finally:
        engine.db.dispose()
