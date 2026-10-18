"""Environment configuration. No credentials are serialized into project manifests."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


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
    model_base_url: str = ""
    model: str = ""
    model_api_key_env: str = "V2C_MODEL_API_KEY"
    model_profile_version: str = "1"
    max_output_tokens: int = 8192
    model_timeout_seconds: int = 90
    model_max_input_bytes: int = 262144
    max_response_bytes: int = 1048576
    runner_base_url: str = ""
    runner_token: str = field(default="", repr=False)
    runner_ca_cert: str | None = None
    runner_client_cert: str | None = None
    runner_client_key: str | None = field(default=None, repr=False)
    vm_provider_url: str = ""
    vm_provider_token: str = field(default="", repr=False)
    lease_seconds: int = 60
    command_timeout_seconds: int = 120
    run_timeout_seconds: int = 3600
    max_upload_bytes: int = 67108864
    git_allowed_hosts: tuple[str, ...] = ()
    execution_limits: dict = field(default_factory=lambda: {
        "cpus": 1, "memory_mb": 1024, "pids": 128, "disk_mb": 512,
        "max_output_bytes": 65536, "network": "none", "image": "",
    })

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        tokens = json.loads(env.get("V2C_OPERATOR_TOKENS", "{}"))
        if not isinstance(tokens, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in tokens.items()):
            raise ValueError("V2C_OPERATOR_TOKENS must map operator IDs to tokens")
        return cls(
            database_url=env.get("V2C_DATABASE_URL", "sqlite:///./data/v2c.db"),
            api_token=env.get("V2C_API_TOKEN", ""), operator_tokens=tokens,
            work_root=Path(env.get("V2C_WORK_ROOT", "data/work")),
            artifact_root=Path(env.get("V2C_ARTIFACT_ROOT", "data/artifacts")),
            storage_backend=env.get("V2C_STORAGE_BACKEND", "local"),
            s3_endpoint_url=env.get("V2C_S3_ENDPOINT_URL"),
            s3_bucket=env.get("V2C_S3_BUCKET", "v2c"), s3_prefix=env.get("V2C_S3_PREFIX", "v2c"),
            model_base_url=env.get("V2C_MODEL_BASE_URL", ""), model=env.get("V2C_MODEL", ""),
            model_api_key_env=env.get("V2C_MODEL_API_KEY_ENV", "V2C_MODEL_API_KEY"),
            model_profile_version=env.get("V2C_MODEL_PROFILE_VERSION", "1"),
            max_output_tokens=_int("V2C_MAX_OUTPUT_TOKENS", 8192),
            model_timeout_seconds=_int("V2C_MODEL_TIMEOUT_SECONDS", 90),
            model_max_input_bytes=_int("V2C_MODEL_MAX_INPUT_BYTES", 262144),
            max_response_bytes=_int("V2C_MAX_RESPONSE_BYTES", 1048576),
            runner_base_url=env.get("V2C_RUNNER_BASE_URL", ""), runner_token=env.get("V2C_RUNNER_TOKEN", ""),
            runner_ca_cert=env.get("V2C_RUNNER_CA_CERT"),
            runner_client_cert=env.get("V2C_RUNNER_CLIENT_CERT"),
            runner_client_key=env.get("V2C_RUNNER_CLIENT_KEY"),
            vm_provider_url=env.get("V2C_VM_PROVIDER_URL", ""),
            vm_provider_token=env.get("V2C_VM_PROVIDER_TOKEN", ""),
            lease_seconds=_int("V2C_LEASE_SECONDS", 60),
            command_timeout_seconds=_int("V2C_COMMAND_TIMEOUT_SECONDS", 120),
            run_timeout_seconds=_int("V2C_RUN_TIMEOUT_SECONDS", 3600),
            max_upload_bytes=_int("V2C_MAX_UPLOAD_BYTES", 67108864),
            git_allowed_hosts=tuple(x.strip() for x in env.get("V2C_GIT_ALLOWED_HOSTS", "").split(",") if x.strip()),
            execution_limits={"cpus": _int("V2C_RUNNER_CPUS", 1),
                "memory_mb": _int("V2C_RUNNER_MEMORY_MB", 1024),
                "pids": _int("V2C_RUNNER_PIDS", 128), "disk_mb": _int("V2C_RUNNER_DISK_MB", 512),
                "max_output_bytes": _int("V2C_RUNNER_MAX_OUTPUT_BYTES", 65536),
                "network": "none", "image": env.get("V2C_RUNNER_IMAGE", "")},
        )

    def tokens(self) -> dict[str, str]:
        result = dict(self.operator_tokens)
        if self.api_token:
            result.setdefault("operator", self.api_token)
        if not result or any(len(t) < 24 or not t.isascii() or any(c.isspace() for c in t) for t in result.values()):
            raise ValueError("Configure ASCII operator tokens of at least 24 characters without whitespace")
        if len(set(result.values())) != len(result):
            raise ValueError("Operator tokens must be unique")
        return result


def build_store(settings: Settings):
    from .storage import LocalArtifactStore, S3ArtifactStore
    if settings.storage_backend == "local":
        return LocalArtifactStore(settings.artifact_root)
    if settings.storage_backend == "s3":
        import boto3
        if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
            raise ValueError("Configure S3 credentials in server environment")
        return S3ArtifactStore(boto3.client("s3", endpoint_url=settings.s3_endpoint_url), settings.s3_bucket, settings.s3_prefix)
    raise ValueError("V2C_STORAGE_BACKEND must be local or s3")


def build_pipeline(settings: Settings, engine):
    """Wire configured ports; absent integrations remain explicit NEEDS_INPUT."""
    from .adapters.model import EnvSecretResolver, ModelProfile, OpenAICompatibleModel
    from .adapters.runner import HTTPVMProvider, MTLSConfig, RunnerClient
    from .pipeline import Pipeline
    model = None
    if settings.model_base_url and settings.model:
        profile = ModelProfile(version=settings.model_profile_version, adapter_type="openai-compatible",
            endpoint=settings.model_base_url, model=settings.model,
            secret_ref="env:" + settings.model_api_key_env, capabilities=frozenset({"text", "structured"}),
            max_input_bytes=settings.model_max_input_bytes, max_output_tokens=settings.max_output_tokens,
            max_response_bytes=settings.max_response_bytes, timeout_seconds=settings.model_timeout_seconds)
        model = OpenAICompatibleModel(profile, EnvSecretResolver())
    provider = factory = None
    tls = all((settings.runner_ca_cert, settings.runner_client_cert, settings.runner_client_key))
    if settings.vm_provider_url and settings.vm_provider_token and tls and settings.runner_token:
        common = dict(ca_file=settings.runner_ca_cert, cert_file=settings.runner_client_cert,
            key_file=settings.runner_client_key, timeout_seconds=settings.command_timeout_seconds,
            max_response_bytes=max(settings.max_upload_bytes * 2, settings.max_response_bytes))
        provider = HTTPVMProvider(MTLSConfig(endpoint=settings.vm_provider_url,
            bearer_secret=settings.vm_provider_token, **common))
        def factory(allocation):
            return RunnerClient(MTLSConfig(endpoint=allocation.runner_endpoint,
                bearer_secret=settings.runner_token, **common))
    return Pipeline(engine, build_store(settings), model=model, settings=settings,
                    vm_provider=provider, runner_factory=factory)
