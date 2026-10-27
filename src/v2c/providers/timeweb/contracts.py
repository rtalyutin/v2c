"""Gateway configuration, allocation request and classified failures."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...adapters.runner import MTLSConfig

_ID = r"^[A-Za-z0-9_-]{1,128}$"
_PLACEHOLDER_ENDPOINT = "https://unassigned.invalid"


class GatewayError(Exception):
    def __init__(self, code: str, message: str, *, unknown: bool = False):
        super().__init__(message)
        self.code, self.unknown = code, unknown


class GatewaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path
    api_token: str = Field(min_length=1, repr=False)
    bearer_secret: str = Field(min_length=16, repr=False)
    ownership_secret: str = Field(min_length=32, repr=False)
    server_template_file: Path
    cloud_init_template_file: Path
    expected_runner_profile_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    runner_ca_file: str
    runner_cert_file: str
    runner_key_file: str
    runner_bearer_secret: str = Field(min_length=16, repr=False)
    runner_server_name: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_response_bytes: int = Field(gt=0)
    max_list_pages: int = Field(gt=0)
    list_page_size: int = Field(gt=0, le=1000)
    runner_port: int = Field(ge=1, le=65535)
    max_active_vms: int = Field(default=1, ge=1, le=1)
    runner_host_template: str = "{ip}"
    firewall_group_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    expected_firewall_rules_file: Path | None = None
    control_cidr: str | None = None

    @field_validator("runner_server_name")
    @classmethod
    def checked_server_name(cls, value: str) -> str:
        return cast(str, MTLSConfig.dns_server_name(value))

    @field_validator("runner_host_template")
    @classmethod
    def validate_host_template(cls, value: str) -> str:
        if value != "{ip}":
            raise ValueError(
                "Timeweb gateway connects to authoritative public IPv4 directly; external DNS templates are unsupported"
            )
        return value

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        path = os.environ.get("V2C_TIMEWEB_SETTINGS_FILE")
        if path:
            return cls.model_validate_json(Path(path).read_text())
        values = {
            field: os.environ["V2C_TIMEWEB_" + field.upper()]
            for field in cls.model_fields
            if "V2C_TIMEWEB_" + field.upper() in os.environ
        }
        return cls.model_validate(values)


class AllocateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    allocation_id: str = Field(pattern=_ID)
    purpose: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
