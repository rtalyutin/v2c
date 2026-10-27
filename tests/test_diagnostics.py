"""Failure evidence must distinguish causes without disclosing payloads."""

import asyncio
import json
import logging

import pytest

from v2c import worker
from v2c.diagnostics import annotate_exception, bind_context, failure_record, reset_context


def test_exception_payload_and_unknown_context_never_enter_diagnostics():
    secret = "synthetic-secret-token-do-not-log"
    reset_context(worker_id="worker-1")
    bind_context(run_id="run-1", token=secret)
    error = ValueError(f"https://user:{secret}@provider.test?key={secret}")
    annotate_exception(error, command_id="command-1", api_key=secret)
    record = failure_record(error, worker_id="worker-1")
    assert record["error_class"] == "ValueError"
    assert record["run_id"] == "run-1"
    assert record["command_id"] == "command-1"
    assert secret not in json.dumps(record)
    assert "token" not in record and "api_key" not in record
    reset_context(worker_id="worker-2")
    assert "run_id" not in failure_record(OSError(secret), worker_id="worker-2")


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, OSError])
async def test_supervisor_logs_structured_failure_without_replaying_work(
    monkeypatch, caplog, error_type
):
    stop = asyncio.Event()
    calls = []

    async def failing_run_once(*args, **kwargs):
        calls.append(True)
        reset_context(worker_id="worker-test")
        bind_context(run_id="run-1", step_id="step-1", attempt_id="attempt-1", generation=2)
        stop.set()
        raise error_type("synthetic-secret-must-not-appear")

    monkeypatch.setattr(worker, "run_once", failing_run_once)
    with caplog.at_level(logging.ERROR, logger="v2c.worker"):
        await worker.serve(None, None, worker_id="worker-test", stop=stop)
    assert calls == [True]
    record = json.loads(caplog.records[-1].message)
    assert record["error_class"] == error_type.__name__
    assert record["event"] == "worker_operation_failed"
    assert record["attempt_id"] == "attempt-1" and record["generation"] == 2
    assert record["locations"][-1]["function"] == "serve"
    assert "synthetic-secret" not in caplog.text
