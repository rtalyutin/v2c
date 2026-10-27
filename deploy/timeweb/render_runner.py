#!/usr/bin/env python3
"""Render operator-only Timeweb cloud_init; never creates cloud resources."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import ssl
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from v2c.runner_service import RunnerSettings
from v2c.timeweb_gateway import public_control_cidr

MARKER = "__V2C_ALLOCATION_ID__"


class BuildProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    runner_settings_file: Path
    tls_server_cert_file: Path
    tls_server_key_file: Path
    tls_client_ca_file: Path
    runner_server_name: str
    control_cidrs: list[str] = Field(min_length=1)
    admin_cidrs: list[str] = Field(default_factory=list)
    runner_port: int = Field(default=8443, ge=1, le=65535)
    bootstrap_timeout_seconds: int = Field(default=900, ge=60, le=3600)

    @field_validator("source_url")
    @classmethod
    def checked_source(cls, value: str) -> str:
        p = urlsplit(value)
        if (
            p.scheme != "https"
            or not p.hostname
            or p.username
            or p.password
            or p.query
            or p.fragment
        ):
            raise ValueError(
                "source URL must be public HTTPS without credentials, query or fragment"
            )
        return value

    @field_validator("runner_server_name")
    @classmethod
    def checked_name(cls, value: str) -> str:
        if len(value) > 253 or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value
        ):
            raise ValueError("runner_server_name must be an operator DNS SAN")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return value
        raise ValueError("SNI identity must be a DNS name, not a dynamic IP")

    @field_validator("control_cidrs", "admin_cidrs")
    @classmethod
    def checked_cidrs(cls, values: list[str]) -> list[str]:
        return sorted({public_control_cidr(value) for value in values})


def firewall(profile: BuildProfile) -> str:
    ingress = "\n".join(
        f"    ip saddr {cidr} tcp dport {profile.runner_port} accept"
        for cidr in profile.control_cidrs
    )
    admin = "\n".join(f"    ip saddr {cidr} tcp dport 22 accept" for cidr in profile.admin_cidrs)
    # Priority precedes Docker's forwarding chains. Same internal bridge traffic
    # permits the one synthetic PostgreSQL service, never an external interface.
    return f"""table inet v2c_runner {{
  chain input {{
    type filter hook input priority -200; policy drop;
    iifname \"lo\" accept
    ct state established,related accept
{ingress}
{admin}
  }}
  chain output {{
    type filter hook output priority -200; policy drop;
    oifname \"lo\" accept
    ct state established,related accept
  }}
  chain forward {{
    type filter hook forward priority -200; policy drop;
    iifname \"br-*\" oifname \"br-*\" accept
  }}
}}
"""


def checked_tls(profile: BuildProfile) -> tuple[str, str, str]:
    cert = profile.tls_server_cert_file.read_text()
    key = profile.tls_server_key_file.read_text()
    ca = profile.tls_client_ca_file.read_text()
    # Match key to leaf and parse client CA using the TLS library. Fail before
    # producing a cloud-init artifact if operator files are malformed.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(profile.tls_server_cert_file, profile.tls_server_key_file)
    context.load_verify_locations(cafile=str(profile.tls_client_ca_file))
    result = subprocess.run(
        [
            "openssl",
            "x509",
            "-in",
            str(profile.tls_server_cert_file),
            "-noout",
            "-ext",
            "subjectAltName",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
        text=True,
    )
    names = re.findall(r"(?:^|[\s,])DNS:([^,\s]+)", result.stdout)
    if result.returncode or profile.runner_server_name.lower() not in {
        value.lower() for value in names
    }:
        raise ValueError("runner certificate does not match configured DNS SAN")
    return cert, key, ca


def render(profile: BuildProfile) -> tuple[str, dict]:
    settings = RunnerSettings.model_validate_json(profile.runner_settings_file.read_text())
    if settings.data_dir != Path("/var/lib/v2c-runner"):
        raise ValueError("template requires dedicated data_dir=/var/lib/v2c-runner")
    if settings.docker_binary != "/usr/bin/docker":
        raise ValueError("template requires /usr/bin/docker")
    if settings.workspace_disk_bytes < 16 * 1024 * 1024:
        raise ValueError("ext4 workspace needs at least 16 MiB")
    settings = settings.model_copy(update={"allocation_id": MARKER})
    cert, key, ca = checked_tls(profile)
    root = Path(__file__).resolve().parent
    bootstrap = root.joinpath("bootstrap.py").read_text()
    service = (
        root.joinpath("v2c-runner.service")
        .read_text()
        .replace("__RUNNER_PORT__", str(profile.runner_port))
    )
    policy_service = root.joinpath("v2c-runner-firewall.service").read_text()
    setup = {
        "source_url": profile.source_url,
        "source_sha256": profile.source_sha256,
        "workspace_disk_bytes": settings.workspace_disk_bytes,
        "images": [settings.image] + ([settings.postgres_image] if settings.postgres_image else []),
        "expected_profile_sha256": settings.profile_sha256(),
    }

    def item(path: str, content: str, mode: str = "0600") -> dict:
        return {"path": path, "owner": "root:root", "permissions": mode, "content": content}

    cloud = {
        "package_update": True,
        "packages": [
            "python3",
            "python3-venv",
            "docker.io",
            "ca-certificates",
            "curl",
            "nftables",
            "util-linux",
            "e2fsprogs",
            "iproute2",
        ],
        "ssh_pwauth": False,
        "write_files": [
            item("/etc/v2c-runner/runner.json", settings.model_dump_json(indent=2)),
            item("/etc/v2c-runner/server.crt", cert),
            item("/etc/v2c-runner/server.key", key),
            item("/etc/v2c-runner/client-ca.crt", ca),
            item("/etc/v2c-runner/bootstrap.json", json.dumps(setup, sort_keys=True)),
            item("/etc/v2c-runner/docker-client/config.json", '{"auths":{}}\n'),
            item("/etc/v2c-runner/lockdown.nft", firewall(profile)),
            item("/opt/v2c-runner-bootstrap.py", bootstrap, "0700"),
            item("/etc/systemd/system/v2c-runner.service", service, "0644"),
            item("/etc/systemd/system/v2c-runner-firewall.service", policy_service, "0644"),
        ],
        "runcmd": [
            [
                "timeout",
                str(profile.bootstrap_timeout_seconds),
                "python3",
                "/opt/v2c-runner-bootstrap.py",
            ]
        ],
    }
    # JSON is valid YAML; the literal allocation marker remains visible for the
    # gateway's bounded replacement without interpreting arbitrary templates.
    output = "#cloud-config\n" + json.dumps(cloud, ensure_ascii=False, indent=2) + "\n"
    receipt = {
        "runner_profile_sha256": settings.profile_sha256(),
        "runner_server_name": profile.runner_server_name,
        "runtime_image": settings.image,
        "source_sha256": profile.source_sha256,
        "contains_operator_secrets": True,
        "cloud_boot_tested": False,
    }
    return output, receipt


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile = BuildProfile.model_validate_json(args.config.read_text())
    cloud, receipt = render(profile)
    write_private(args.output, cloud)
    write_private(args.output.with_suffix(".profile.json"), json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            {"output": str(args.output), "runner_profile_sha256": receipt["runner_profile_sha256"]}
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Runner template generation failed; validate operator files/configuration (secrets not printed)"
        )
