"""Operator settings and immutable runner profile."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RunnerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path
    bearer_secret: str = Field(min_length=16, repr=False)
    image: str = Field(pattern=r"^.+@sha256:[a-f0-9]{64}$")
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
    docker_binary: str = "/usr/bin/docker"
    allocation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    postgres_image: str | None = None
    postgres_data_bytes: int | None = Field(default=None, gt=0)
    postgres_ready_seconds: int | None = Field(default=None, gt=0)
    postgres_uid: int | None = Field(default=None, ge=1)
    postgres_gid: int | None = Field(default=None, ge=1)

    @field_validator("postgres_image")
    @classmethod
    def pinned_postgres(cls, value: str | None) -> str | None:
        if not value:
            return None
        import re

        if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", value):
            raise ValueError("PostgreSQL image must be pinned to a digest")
        return value

    @model_validator(mode="after")
    def postgres_configuration(self) -> Self:
        if self.postgres_image and any(
            getattr(self, field) is None
            for field in (
                "postgres_data_bytes",
                "postgres_ready_seconds",
                "postgres_uid",
                "postgres_gid",
            )
        ):
            raise ValueError("PostgreSQL requires explicit data/readiness/nonroot UID/GID limits")
        return self

    def public_profile(self) -> dict[str, Any]:
        # Only execution semantics enter the attestation; filesystem locations,
        # credentials and the allocation identity are checked separately.
        return {
            "protocol": "v1",
            **self.model_dump(
                mode="json", exclude={"data_dir", "bearer_secret", "docker_binary", "allocation_id"}
            ),
        }

    def profile_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.public_profile(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()

    @classmethod
    def from_env(cls) -> "RunnerSettings":
        config_file = os.environ.get("V2C_RUNNER_SETTINGS_FILE")
        if config_file:
            return cls.model_validate_json(Path(config_file).read_text())
        values = {
            name: os.environ["V2C_RUNNER_" + name.upper()]
            for name in cls.model_fields
            if "V2C_RUNNER_" + name.upper() in os.environ
        }
        return cls.model_validate(values)
