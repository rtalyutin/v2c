"""Configuration and the actual model-adapter probe, with a synthetic HTTP peer."""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from v2c.adapters.model import EnvSecretResolver, OpenAICompatibleModel
from v2c.adapters.runner import VMAllocation
from v2c.config import GROQ_FREE_PRESET, Settings, build_model, build_pipeline
from v2c.engine import Engine
from v2c.pipeline import InputRequired, Pipeline
from v2c.model_probe import main, probe


def model_settings():
    return Settings(
        model_base_url="https://model.example.invalid/v1",
        model="explicit-test-model",
        model_api_key_env="V2C_TEST_PROBE_KEY",
        model_profile_version="probe-test-1",
        model_max_input_bytes=6000,
        max_output_tokens=256,
    )


@pytest.mark.asyncio
async def test_empty_model_profile_is_blocked_without_adapter_or_network():
    result = await probe(Settings())
    assert result["status"] == "NEEDS_PROFILE"
    assert result["network_request_performed"] is False
    assert result["validated_capabilities"] == []


@pytest.mark.asyncio
async def test_missing_key_is_detected_before_adapter_invocation(monkeypatch):
    settings = model_settings()
    monkeypatch.delenv(settings.model_api_key_env, raising=False)
    model = build_model(settings)

    async def forbidden(_):
        raise AssertionError("An absent key must never reach a provider or adapter call.")

    model.generate = forbidden
    result = await probe(settings, model=model)
    assert result["status"] == "NEEDS_KEY"
    assert result["network_request_performed"] is False
    assert result["key_env"] == settings.model_api_key_env
    assert result["validated_capabilities"] == []


@pytest.mark.asyncio
async def test_probe_uses_real_adapter_and_fixed_synthetic_schema_without_printing_key(monkeypatch):
    settings = model_settings()
    secret = "synthetic-probe-key-not-for-output"
    monkeypatch.setenv(settings.model_api_key_env, secret)
    seen = []

    def provider(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer " + secret
        payload = json.loads(request.content)
        assert payload["model"] == "explicit-test-model"
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert payload["max_tokens"] == 256
        assert "Synthetic constants only. No project data." in payload["messages"][1]["content"]
        assert "repositories/" not in request.content.decode()
        return httpx.Response(
            200,
            headers={"x-request-id": "synthetic-provider-receipt"},
            json={
                "choices": [
                    {"message": {"content": '{"probe":"v2c","answer":2}'}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 19, "completion_tokens": 11, "total_tokens": 30},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        model = OpenAICompatibleModel(build_model(settings).profile, EnvSecretResolver(), client)
        result = await probe(settings, model=model)
    assert len(seen) == 1
    assert result["status"] == "VERIFIED"
    assert result["network_request_performed"] is True
    assert result["schema_valid"]
    assert result["validated_capabilities"] == ["structured", "text"]
    assert result["usage"]["total_tokens"] == 30
    assert result["billing_plan_verified"] is False
    assert secret not in json.dumps(result)


@pytest.mark.asyncio
async def test_probe_rate_limit_has_one_request_and_no_provider_fallback(monkeypatch):
    settings = model_settings()
    monkeypatch.setenv(settings.model_api_key_env, "synthetic-secret")
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(
            429, headers={"retry-after": "30"}, json={"error": {"message": "quota"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        model = OpenAICompatibleModel(build_model(settings).profile, EnvSecretResolver(), client)
        result = await probe(settings, model=model)
    assert len(requests) == 1
    assert result["status"] == "REJECTED"
    assert result["reason_code"] == "RATE_LIMIT"
    assert result["schema_valid"] is False


def test_probe_cli_missing_key_and_invalid_url_never_print_credentials(monkeypatch, capsys):
    monkeypatch.setenv("V2C_MODEL_BASE_URL", "https://model.example.invalid/v1")
    monkeypatch.setenv("V2C_MODEL", "explicit-test-model")
    monkeypatch.setenv("V2C_MODEL_API_KEY_ENV", "V2C_TEST_PROBE_KEY")
    monkeypatch.delenv("V2C_TEST_PROBE_KEY", raising=False)
    assert main([]) == 2
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "NEEDS_KEY"
    assert first["network_request_performed"] is False
    monkeypatch.setenv(
        "V2C_MODEL_BASE_URL", "https://user:do-not-print-this-secret@model.example.invalid/v1"
    )
    assert main([]) == 1
    output = capsys.readouterr().out
    assert "do-not-print-this-secret" not in output
    assert json.loads(output)["status"] == "INVALID_CONFIG"


def free_environment(monkeypatch):
    for name in (
        "V2C_MODEL_BASE_URL",
        "V2C_MODEL",
        "V2C_MODEL_API_KEY_ENV",
        "V2C_MODEL_PROFILE_VERSION",
        "V2C_MAX_OUTPUT_TOKENS",
        "V2C_MODEL_TIMEOUT_SECONDS",
        "V2C_MODEL_MAX_INPUT_BYTES",
        "V2C_MAX_RESPONSE_BYTES",
        "GROQ_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("V2C_MODEL_PRESET", GROQ_FREE_PRESET)


def test_groq_free_preset_is_explicit_bounded_and_has_no_paid_override(monkeypatch):
    free_environment(monkeypatch)
    settings = Settings.from_env()
    model = build_model(settings)
    assert model.profile.endpoint == "https://api.groq.com/openai/v1"
    assert model.profile.model == "openai/gpt-oss-120b"
    assert model.profile.secret_ref == "env:GROQ_API_KEY"
    assert model.profile.max_input_bytes == 6000
    assert model.profile.max_output_tokens == 1024
    assert model.profile.max_response_bytes == 65536
    assert model.profile.timeout_seconds == 60
    monkeypatch.setenv("V2C_MODEL", "some-other-paid-model")
    with pytest.raises(ValueError, match="MODEL_PRESET_CONFLICT"):
        Settings.from_env()
    monkeypatch.delenv("V2C_MODEL")
    monkeypatch.setenv("V2C_MODEL_MAX_INPUT_BYTES", "131072")
    with pytest.raises(ValueError, match="MODEL_PRESET_LIMIT_EXCEEDED"):
        Settings.from_env()


def test_control_factory_uses_runner_sni_only_for_dynamic_runner_endpoint(tmp_path):
    settings = Settings(
        work_root=tmp_path / "work",
        artifact_root=tmp_path / "artifacts",
        runner_server_name="runner.v2c.internal",
        runner_token="r" * 32,
        vm_provider_url="https://timeweb-gateway:9443",
        vm_provider_token="g" * 32,
        runner_ca_cert="/run/control/ca.crt",
        runner_client_cert="/run/control/client.crt",
        runner_client_key="/run/control/client.key",
    )
    pipeline = build_pipeline(settings, None)
    runner = pipeline.runner_factory(
        VMAllocation(
            vm_id="synthetic-allocation", runner_endpoint="https://192.0.2.1:9443", state="READY"
        )
    )
    assert runner.config.endpoint == "https://192.0.2.1:9443"
    assert runner.config.server_name == "runner.v2c.internal"
    assert runner.config.request_extensions() == {"sni_hostname": "runner.v2c.internal"}
    assert pipeline.vm_provider.config.server_name is None


def test_actual_preset_cli_reports_missing_key_without_network(monkeypatch, capsys):
    free_environment(monkeypatch)
    assert main([]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "NEEDS_KEY"
    assert result["key_env"] == "GROQ_API_KEY"
    assert result["network_request_performed"] is False
    assert result["profile"]["model"] == "openai/gpt-oss-120b"
    assert result["provider_exchange_verified"] is False


def test_timeweb_override_keeps_control_model_key_and_global_run_deadline():
    root = Path(__file__).resolve().parents[1]
    assignments = {}
    for line in (root / ".env.timeweb.example").read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            assignments[key.strip()] = value
    merged = {
        "GROQ_API_KEY": "synthetic-control-key",
        "V2C_RUN_TIMEOUT_SECONDS": "3600",
        **assignments,
    }
    assert merged["GROQ_API_KEY"] == "synthetic-control-key"
    assert assignments["V2C_MODEL_PRESET"] == GROQ_FREE_PRESET
    assert assignments["V2C_PROVISION_TIMEOUT_SECONDS"] == "1800"
    assert assignments["V2C_PROVISION_POLL_SECONDS"] == "5"
    assert merged["V2C_RUN_TIMEOUT_SECONDS"] == "3600"
    assert "V2C_RUN_TIMEOUT_SECONDS" not in assignments


class PackedAnswer(BaseModel):
    answer: str


def probe_claim(tmp_path):
    engine = Engine("sqlite:///" + str(tmp_path / "state.sqlite3"))
    engine.init_schema()
    project = engine.create_project("operator", "Context selection fixture")
    snapshot = engine.create_snapshot(project["id"], {"fixture": "no-source-execution"})
    run = engine.create_run(project["id"], snapshot["id"], "one-request")
    return engine, engine.claim_next("fixture-worker", lease_seconds=120), run


@pytest.mark.asyncio
async def test_exact_wire_budget_keeps_required_work_and_reports_omitted_whole_files(
    tmp_path, monkeypatch
):
    settings = model_settings()
    settings.model_max_input_bytes = 2400
    monkeypatch.setenv(settings.model_api_key_env, "synthetic-credential")
    seen = []

    def provider(request):
        assert len(request.content) <= 2400
        payload = json.loads(request.content)
        context = json.loads(json.loads(payload["messages"][1]["content"])["context"])
        seen.append(context)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}]
            },
        )

    engine, claim, run = probe_claim(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        model = OpenAICompatibleModel(build_model(settings).profile, EnvSecretResolver(), client)
        pipeline = Pipeline(engine, None, model=model, settings=settings)
        context = {
            "work": {
                "id": "keep-this-work",
                "files": ["repositories/main/target.py"],
                "preserve": ["the unchanged contract"],
            },
            "source": {
                "files": [
                    {
                        "path": "repositories/main/target.py",
                        "sha256": "a" * 64,
                        "text": "def answer(): return 42\n",
                    },
                    {
                        "path": "repositories/main/huge.py",
                        "sha256": "b" * 64,
                        "text": "large optional context\n" * 1000,
                    },
                ],
                "omitted_files": [],
            },
        }
        answer = await pipeline._model(
            claim, "test_budget", "Return the requested small result.", context, PackedAnswer
        )
    assert answer == {"answer": "ok"}
    assert seen[0]["work"] == context["work"]
    assert seen[0]["source"]["files"][0]["text"] == "def answer(): return 42\n"
    assert seen[0]["source"]["coverage"]["omitted_file_count"] == 1
    assert "repositories/main/huge.py" in seen[0]["source"]["omitted_files"]
    operation = next(
        c
        for c in engine.list_commands(run_id=run["id"])
        if c["request"].get("operation") == "model"
    )
    assert operation["request"]["context_selection"]["partial"] is True
    assert operation["request"]["context_selection"]["request_bytes"] <= 2400


@pytest.mark.asyncio
async def test_task_or_schema_that_cannot_fit_is_not_trimmed_or_dispatched(tmp_path, monkeypatch):
    settings = model_settings()
    settings.model_max_input_bytes = 300
    monkeypatch.setenv(settings.model_api_key_env, "synthetic-credential")
    model = build_model(settings)

    async def forbidden(_):
        raise AssertionError("An impossible base request must not reach the provider.")

    model.generate = forbidden
    engine, claim, run = probe_claim(tmp_path)
    pipeline = Pipeline(engine, None, model=model, settings=settings)
    with pytest.raises(InputRequired, match="MODEL_BASE_CONTEXT_LIMIT"):
        await pipeline._model(
            claim,
            "too_large",
            "Important task that must not be shortened." * 20,
            {"source": {"files": [], "omitted_files": []}},
            PackedAnswer,
        )
    assert not engine.list_commands(run_id=run["id"])
