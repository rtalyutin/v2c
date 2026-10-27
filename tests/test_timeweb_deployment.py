"""Deployment generators are local-only and preserve the trust boundary."""

from __future__ import annotations

import importlib.util
import json
import os
import ssl
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/timeweb" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


@pytest.fixture(scope="module")
def generated_pki(tmp_path_factory):
    output = tmp_path_factory.mktemp("pki-parent") / "pki"
    module("make_pki").generate(output, "runner.v2c.internal", "timeweb-gateway.v2c.internal", 1)
    return output


def test_pki_private_separate_identities(generated_pki):
    for path in generated_pki.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    runner = ssl._ssl._test_decode_cert(str(generated_pki / "runner.crt"))
    gateway = ssl._ssl._test_decode_cert(str(generated_pki / "gateway.crt"))
    assert ("DNS", "runner.v2c.internal") in runner["subjectAltName"]
    assert ("DNS", "runner.v2c.internal") not in gateway["subjectAltName"]
    assert (generated_pki / "runner.key").read_bytes() != (
        generated_pki / "gateway.key"
    ).read_bytes()


def profile(tmp_path, pki):
    from v2c.runner_service import RunnerSettings

    settings = RunnerSettings(
        data_dir=Path("/var/lib/v2c-runner"),
        bearer_secret="runner-secret-for-private-test",
        image="example/runtime@sha256:" + "a" * 64,
        cpu_limit=1,
        memory_bytes=1073741824,
        pids_limit=128,
        tmpfs_bytes=67108864,
        workspace_disk_bytes=536870912,
        max_archive_bytes=67108864,
        max_expanded_bytes=268435456,
        max_files=20000,
        max_command_seconds=120,
        max_output_bytes=65536,
        uid=10001,
        gid=10001,
    )
    source = tmp_path / "runner.json"
    source.write_text(settings.model_dump_json())
    data = dict(
        source_url="https://example.invalid/v2c/source.tar.gz",
        source_sha256="b" * 64,
        runner_settings_file=source,
        tls_server_cert_file=pki / "runner.crt",
        tls_server_key_file=pki / "runner.key",
        tls_client_ca_file=pki / "ca.crt",
        runner_server_name="runner.v2c.internal",
        control_cidrs=["8.8.8.8/32"],
    )
    return data, settings


def test_render_keeps_marker_and_profile_but_no_cloud_credentials(tmp_path, generated_pki):
    renderer = module("render_runner")
    data, settings = profile(tmp_path, generated_pki)
    raw, receipt = renderer.render(renderer.BuildProfile(**data))
    cloud = json.loads(raw.removeprefix("#cloud-config\n"))
    files = {row["path"]: row for row in cloud["write_files"]}
    runner = json.loads(files["/etc/v2c-runner/runner.json"]["content"])
    assert runner["allocation_id"] == renderer.MARKER
    assert receipt["runner_profile_sha256"] == settings.profile_sha256()
    assert "V2C_TIMEWEB_API_TOKEN" not in raw and "AWS_SECRET_ACCESS_KEY" not in raw
    assert all(
        row["permissions"] == "0600"
        for row in cloud["write_files"]
        if row["path"].startswith("/etc/v2c-runner/")
    )
    policy = files["/etc/v2c-runner/lockdown.nft"]["content"]
    assert "ip saddr 8.8.8.8/32 tcp dport 8443 accept" in policy
    assert "chain output" in policy and "policy drop" in policy
    script = files["/opt/v2c-runner-bootstrap.py"]["content"]
    assert script.index("hashlib.sha256(archive.read_bytes())") < script.index("source.extractall")
    assert script.index('docker", "pull') < script.index(
        'enable", "--now", "v2c-runner-firewall.service'
    )


def test_renderer_rejects_global_allow_and_wrong_certificate(tmp_path, generated_pki):
    renderer = module("render_runner")
    data, _ = profile(tmp_path, generated_pki)
    with pytest.raises(ValueError):
        renderer.BuildProfile(**{**data, "control_cidrs": ["0.0.0.0/0"]})
    with pytest.raises(ValueError):
        renderer.render(
            renderer.BuildProfile(**{**data, "runner_server_name": "wrong.v2c.internal"})
        )


def test_private_writer_refuses_overwrite(tmp_path):
    renderer = module("render_runner")
    output = tmp_path / "private.cloud-init.yaml"
    old = os.umask(0)
    try:
        renderer.write_private(output, "secret")
    finally:
        os.umask(old)
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        renderer.write_private(output, "replacement")
