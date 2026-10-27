"""Environment configuration. No credentials are serialized into project manifests."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from .contracts.workflow import ExecutionLimits

if TYPE_CHECKING:
    from .adapters.model import OpenAICompatibleModel
    from .adapters.runner import VMAllocation
    from .contracts.ports import RunnerFactory
    from .engine import Engine
    from .pipeline import Pipeline
    from .storage import ArtifactStore


class _ModelPreset(TypedDict):
    model_base_url: str
    model: str
    model_api_key_env: str
    model_profile_version: str
    max_output_tokens: int
    model_timeout_seconds: int
    model_max_input_bytes: int
    max_response_bytes: int


class _ModelEnvironment(_ModelPreset):
    model_preset: str


class _TLSOptions(TypedDict):
    ca_file: str
    cert_file: str
    key_file: str
    timeout_seconds: int
    max_response_bytes: int


def _int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


# Explicit smoke/small-change preset. It never selects another provider or a
# paid model when quota is exhausted. Account billing eligibility is not known
# from an API key and is not claimed by this configuration.
GROQ_FREE_PRESET = "groq-free-gpt-oss-120b"
MODEL_PRESETS: dict[str, _ModelPreset] = {
    GROQ_FREE_PRESET: {
        "model_base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "model_api_key_env": "GROQ_API_KEY",
        "model_profile_version": "groq-free-smoke-1",
        "max_output_tokens": 1024,
        "model_timeout_seconds": 60,
        "model_max_input_bytes": 6000,
        "max_response_bytes": 65536,
    },
}


def _model_environment() -> _ModelEnvironment:
    """Resolve an explicitly selected preset; contradictory overrides fail."""
    name = os.environ.get("V2C_MODEL_PRESET", "")
    if name and name not in MODEL_PRESETS:
        raise ValueError("MODEL_PRESET_UNKNOWN")
    defaults: _ModelPreset = MODEL_PRESETS.get(
        name,
        {
            "model_base_url": "",
            "model": "",
            "model_api_key_env": "V2C_MODEL_API_KEY",
            "model_profile_version": "1",
            "max_output_tokens": 8192,
            "model_timeout_seconds": 90,
            "model_max_input_bytes": 262144,
            "max_response_bytes": 1048576,
        },
    )

    def text_value(env_name: str, default: str, *, fixed: bool = False) -> str:
        value = os.environ.get(env_name) or default
        if name and fixed and value != default:
            raise ValueError("MODEL_PRESET_CONFLICT")
        return value

    def limit_value(env_name: str, default: int) -> int:
        value = _int(env_name, default)
        if name and value > default:
            raise ValueError("MODEL_PRESET_LIMIT_EXCEEDED")
        return value

    return {
        "model_preset": name,
        "model_base_url": text_value("V2C_MODEL_BASE_URL", defaults["model_base_url"], fixed=True),
        "model": text_value("V2C_MODEL", defaults["model"], fixed=True),
        "model_api_key_env": text_value(
            "V2C_MODEL_API_KEY_ENV", defaults["model_api_key_env"], fixed=True
        ),
        "model_profile_version": text_value(
            "V2C_MODEL_PROFILE_VERSION", defaults["model_profile_version"]
        ),
        "max_output_tokens": limit_value("V2C_MAX_OUTPUT_TOKENS", defaults["max_output_tokens"]),
        "model_timeout_seconds": limit_value(
            "V2C_MODEL_TIMEOUT_SECONDS", defaults["model_timeout_seconds"]
        ),
        "model_max_input_bytes": limit_value(
            "V2C_MODEL_MAX_INPUT_BYTES", defaults["model_max_input_bytes"]
        ),
        "max_response_bytes": limit_value("V2C_MAX_RESPONSE_BYTES", defaults["max_response_bytes"]),
    }


def _default_execution_limits() -> ExecutionLimits:
    return {
        "cpus": 1,
        "memory_mb": 1024,
        "pids": 128,
        "disk_mb": 512,
        "max_output_bytes": 65536,
        "network": "none",
        "image": "",
    }


@dataclass
class Settings:
    database_url: str = "sqlite:///./data/v2c.db"
    api_token: str = field(default="", repr=False)
    operator_tokens: dict[str, str] = field(default_factory=dict, repr=False)
    work_root: Path = Path("data/work")
    artifact_root: Path = Path("data/artifacts")
    storage_backend: str = "local"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "v2c"
    s3_prefix: str = "v2c"
    model_preset: str = ""
    model_base_url: str = ""
    model: str = ""
    model_api_key_env: str = "V2C_MODEL_API_KEY"
    model_profile_version: str = "1"
    max_output_tokens: int = 8192
    model_timeout_seconds: int = 90
    model_max_input_bytes: int = 262144
    max_response_bytes: int = 1048576
    runner_base_url: str = ""
    runner_server_name: str | None = None
    runner_token: str = field(default="", repr=False)
    runner_ca_cert: str | None = None
    runner_client_cert: str | None = None
    runner_client_key: str | None = field(default=None, repr=False)
    vm_provider_url: str = ""
    vm_provider_token: str = field(default="", repr=False)
    lease_seconds: int = 60
    command_timeout_seconds: int = 120
    provision_timeout_seconds: int = 600
    provision_poll_seconds: int = 5
    run_timeout_seconds: int = 3600
    max_upload_bytes: int = 67108864
    git_allowed_hosts: tuple[str, ...] = ()
    execution_limits: ExecutionLimits = field(default_factory=_default_execution_limits)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        tokens = json.loads(env.get("V2C_OPERATOR_TOKENS", "{}"))
        if not isinstance(tokens, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in tokens.items()
        ):
            raise ValueError("V2C_OPERATOR_TOKENS must map operator IDs to tokens")
        return cls(
            database_url=env.get("V2C_DATABASE_URL", "sqlite:///./data/v2c.db"),
            api_token=env.get("V2C_API_TOKEN", ""),
            operator_tokens=tokens,
            work_root=Path(env.get("V2C_WORK_ROOT", "data/work")),
            artifact_root=Path(env.get("V2C_ARTIFACT_ROOT", "data/artifacts")),
            storage_backend=env.get("V2C_STORAGE_BACKEND", "local"),
            s3_endpoint_url=env.get("V2C_S3_ENDPOINT_URL"),
            s3_bucket=env.get("V2C_S3_BUCKET", "v2c"),
            s3_prefix=env.get("V2C_S3_PREFIX", "v2c"),
            **_model_environment(),
            runner_base_url=env.get("V2C_RUNNER_BASE_URL", ""),
            runner_token=env.get("V2C_RUNNER_TOKEN", ""),
            runner_server_name=env.get("V2C_RUNNER_SERVER_NAME") or None,
            runner_ca_cert=env.get("V2C_RUNNER_CA_CERT"),
            runner_client_cert=env.get("V2C_RUNNER_CLIENT_CERT"),
            runner_client_key=env.get("V2C_RUNNER_CLIENT_KEY"),
            vm_provider_url=env.get("V2C_VM_PROVIDER_URL", ""),
            vm_provider_token=env.get("V2C_VM_PROVIDER_TOKEN", ""),
            lease_seconds=_int("V2C_LEASE_SECONDS", 60),
            command_timeout_seconds=_int("V2C_COMMAND_TIMEOUT_SECONDS", 120),
            provision_timeout_seconds=_int("V2C_PROVISION_TIMEOUT_SECONDS", 600),
            provision_poll_seconds=_int("V2C_PROVISION_POLL_SECONDS", 5),
            run_timeout_seconds=_int("V2C_RUN_TIMEOUT_SECONDS", 3600),
            max_upload_bytes=_int("V2C_MAX_UPLOAD_BYTES", 67108864),
            git_allowed_hosts=tuple(
                x.strip() for x in env.get("V2C_GIT_ALLOWED_HOSTS", "").split(",") if x.strip()
            ),
            execution_limits={
                "cpus": _int("V2C_RUNNER_CPUS", 1),
                "memory_mb": _int("V2C_RUNNER_MEMORY_MB", 1024),
                "pids": _int("V2C_RUNNER_PIDS", 128),
                "disk_mb": _int("V2C_RUNNER_DISK_MB", 512),
                "max_output_bytes": _int("V2C_RUNNER_MAX_OUTPUT_BYTES", 65536),
                "network": "none",
                "image": env.get("V2C_RUNNER_IMAGE", ""),
            },
        )

    def tokens(self) -> dict[str, str]:
        result = dict(self.operator_tokens)
        if self.api_token:
            result.setdefault("operator", self.api_token)
        if not result or any(
            len(t) < 24 or not t.isascii() or any(c.isspace() for c in t) for t in result.values()
        ):
            raise ValueError(
                "Configure ASCII operator tokens of at least 24 characters without whitespace"
            )
        if len(set(result.values())) != len(result):
            raise ValueError("Operator tokens must be unique")
        return result


def build_store(settings: Settings) -> ArtifactStore:
    from .storage import LocalArtifactStore, S3ArtifactStore

    if settings.storage_backend == "local":
        return LocalArtifactStore(settings.artifact_root)
    if settings.storage_backend == "s3":
        # boto3 supplies no inline typing; the SDK stays behind the storage adapter.
        import boto3  # type: ignore[import-untyped]

        if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
            raise ValueError("Configure S3 credentials in server environment")
        return S3ArtifactStore(
            boto3.client("s3", endpoint_url=settings.s3_endpoint_url),
            settings.s3_bucket,
            settings.s3_prefix,
        )
    raise ValueError("V2C_STORAGE_BACKEND must be local or s3")


def build_model(settings: Settings) -> OpenAICompatibleModel | None:
    """Build exactly the selected model profile, without a provider fallback."""
    from .adapters.model import EnvSecretResolver, ModelProfile, OpenAICompatibleModel

    if settings.model_preset:
        expected = MODEL_PRESETS.get(settings.model_preset)
        if expected is None:
            raise ValueError("MODEL_PRESET_UNKNOWN")
        for name in ("model_base_url", "model", "model_api_key_env"):
            if getattr(settings, name) != expected[name]:
                raise ValueError("MODEL_PRESET_CONFLICT")
        for limit_name in (
            "max_output_tokens",
            "model_timeout_seconds",
            "model_max_input_bytes",
            "max_response_bytes",
        ):
            if getattr(settings, limit_name) > expected[limit_name]:
                raise ValueError("MODEL_PRESET_LIMIT_EXCEEDED")
    if settings.model_base_url and settings.model:
        profile = ModelProfile(
            version=settings.model_profile_version,
            adapter_type="openai-compatible",
            endpoint=settings.model_base_url,
            model=settings.model,
            secret_ref="env:" + settings.model_api_key_env,
            capabilities=frozenset({"text", "structured"}),
            max_input_bytes=settings.model_max_input_bytes,
            max_output_tokens=settings.max_output_tokens,
            max_response_bytes=settings.max_response_bytes,
            timeout_seconds=settings.model_timeout_seconds,
        )
        return OpenAICompatibleModel(profile, EnvSecretResolver())
    return None


def build_pipeline(settings: Settings, engine: Engine) -> Pipeline:
    """Wire configured ports; absent integrations remain explicit NEEDS_INPUT."""
    from .adapters.runner import HTTPVMProvider, MTLSConfig, RunnerClient
    from .pipeline import Pipeline

    model = build_model(settings)
    provider: HTTPVMProvider | None = None
    factory: RunnerFactory | None = None
    ca_file = settings.runner_ca_cert
    cert_file = settings.runner_client_cert
    key_file = settings.runner_client_key
    if (
        settings.vm_provider_url
        and settings.vm_provider_token
        and ca_file
        and cert_file
        and key_file
        and settings.runner_token
    ):
        common: _TLSOptions = dict(
            ca_file=ca_file,
            cert_file=cert_file,
            key_file=key_file,
            timeout_seconds=settings.command_timeout_seconds,
            max_response_bytes=max(settings.max_upload_bytes * 2, settings.max_response_bytes),
        )
        provider = HTTPVMProvider(
            MTLSConfig(
                endpoint=settings.vm_provider_url,
                bearer_secret=settings.vm_provider_token,
                **common,
            )
        )

        def runner_factory(allocation: VMAllocation) -> RunnerClient:
            return RunnerClient(
                MTLSConfig(
                    endpoint=allocation.runner_endpoint,
                    server_name=settings.runner_server_name,
                    bearer_secret=settings.runner_token,
                    **common,
                )
            )

        factory = runner_factory

    return Pipeline(
        engine,
        build_store(settings),
        model=model,
        settings=settings,
        vm_provider=provider,
        runner_factory=factory,
    )
