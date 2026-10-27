"""Independent Timeweb/free-model acceptance boundaries; never uses live cloud credentials."""

import asyncio
import importlib.util
import ipaddress
import json
import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from v2c.adapters.model import EnvSecretResolver, ModelResult, ModelUsage, OpenAICompatibleModel
from v2c.config import Settings, build_model
from v2c.model_probe import probe as model_probe
from v2c.runner_service import RunnerSettings
from v2c.timeweb_gateway import (
    AllocateRequest,
    GatewayError,
    GatewaySettings,
    RunnerProbe,
    TimewebClient,
    TimewebGateway,
    create_app,
    public_firewall_rules,
)


def free_settings(monkeypatch):
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
    monkeypatch.setenv("V2C_MODEL_PRESET", "groq-free-gpt-oss-120b")
    return Settings.from_env()


@pytest.mark.asyncio
async def test_free_profile_without_key_is_blocked_before_any_http(monkeypatch):
    settings = free_settings(monkeypatch)
    requests = []

    async def forbidden_http(*args, **kwargs):
        requests.append(True)
        raise AssertionError("missing credential must not cause any HTTP request")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_http)
    report = await model_probe(settings)
    assert settings.model_base_url == "https://api.groq.com/openai/v1"
    assert settings.model == "openai/gpt-oss-120b"
    assert report["status"] == "NEEDS_KEY"
    assert report["network_request_performed"] is False
    assert report["provider_exchange_verified"] is False
    assert requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("V2C_MODEL_BASE_URL", "https://paid-provider.test/v1"),
        ("V2C_MODEL", "paid-model"),
    ],
)
def test_free_profile_refuses_conflicting_provider_or_model(monkeypatch, field, value):
    free_settings(monkeypatch)
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError, match="MODEL_PRESET_CONFLICT"):
        Settings.from_env()


@pytest.mark.asyncio
async def test_free_quota_rejection_sends_one_selected_request_without_fallback(monkeypatch):
    settings = free_settings(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-not-a-provider-key")
    calls = []

    def reject(request):
        calls.append((str(request.url), json.loads(request.content)))
        return httpx.Response(
            429, json={"error": {"message": "synthetic quota exhausted"}}, request=request
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        profile = build_model(settings).profile
        model = OpenAICompatibleModel(profile, EnvSecretResolver(), http_client=client)
        report = await model_probe(settings, model=model)
    assert report["status"] == "REJECTED" and report["reason_code"] == "RATE_LIMIT"
    assert report["provider_exchange_verified"] is False
    assert len(calls) == 1
    assert calls[0][0] == "https://api.groq.com/openai/v1/chat/completions"
    assert calls[0][1]["model"] == "openai/gpt-oss-120b"


@pytest.mark.asyncio
async def test_successful_test_model_cannot_qualify_real_provider_generation(monkeypatch):
    settings = free_settings(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-not-a-provider-key")

    class SyntheticModel:
        is_test = True
        profile = build_model(settings).profile

        async def generate(self, request):
            return ModelResult(
                text='{"probe":"v2c","answer":2}',
                structured={"probe": "v2c", "answer": 2},
                finish_reason="stop",
                usage=ModelUsage(),
            )

    report = await model_probe(settings, model=SyntheticModel())
    assert report["status"] == "LOCAL_VERIFIED"
    assert report["provider_exchange_verified"] is False
    assert report["network_request_performed"] is False
    assert report["billing_plan_verified"] is False
    assert report["test_adapter"] is True


def gateway_settings(tmp_path, **changes):
    tmp_path.mkdir(parents=True, exist_ok=True)
    server = tmp_path / "server.json"
    server.write_text(
        json.dumps({"preset_id": 11, "image_id": "6a2c9f7e-0000-0000-0000-000000000000"})
    )
    cloud_init = tmp_path / "cloud-init.yaml"
    cloud_init.write_text(
        "#cloud-config\nwrite_files:\n  - path: /etc/v2c/allocation\n    content: __V2C_ALLOCATION_ID__\n"
    )
    values = dict(
        data_dir=tmp_path / "state",
        api_token="synthetic-cloud-token",
        bearer_secret="synthetic-gateway-bearer",
        ownership_secret="synthetic-ownership-secret-at-least-32-characters",
        server_template_file=server,
        cloud_init_template_file=cloud_init,
        expected_runner_profile_sha256="b" * 64,
        runner_ca_file="/unused/ca",
        runner_cert_file="/unused/cert",
        runner_key_file="/unused/key",
        runner_bearer_secret="synthetic-runner-bearer",
        runner_server_name="runner.test.invalid",
        request_timeout_seconds=2,
        max_response_bytes=10000,
        max_list_pages=2,
        list_page_size=10,
        runner_port=8443,
    )
    values.update(changes)
    return GatewaySettings(**values)


@pytest.fixture(scope="module")
def tls_material(tmp_path_factory):
    """Temporary private CA, server DNS SAN and client certificate; no external network."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl absent; real local mTLS gate cannot execute")
    root = tmp_path_factory.mktemp("timeweb-qa-tls")

    def run(*args):
        subprocess.run(
            ["openssl", *map(str, args)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )

    run(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=v2c QA CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout",
        root / "ca.key",
        "-out",
        root / "ca.crt",
    )
    for name in ("server", "client"):
        run(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=" + name,
            "-keyout",
            root / (name + ".key"),
            "-out",
            root / (name + ".csr"),
        )
        extension = root / (name + ".ext")
        extension.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            + (
                "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:runner.test.invalid\n"
                if name == "server"
                else "extendedKeyUsage=clientAuth\n"
            )
        )
        run(
            "x509",
            "-req",
            "-in",
            root / (name + ".csr"),
            "-CA",
            root / "ca.crt",
            "-CAkey",
            root / "ca.key",
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            extension,
            "-out",
            root / (name + ".crt"),
        )
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [None, "hostname", "allocation", "profile"])
async def test_dynamic_ip_probe_requires_real_mtls_hostname_and_identity(
    tmp_path, tls_material, mismatch
):
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(tls_material / "server.crt", tls_material / "server.key")
    context.load_verify_locations(tls_material / "ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    receipt = {
        "status": "ok",
        "protocol": "v1",
        "allocation_id": "qa-allocation",
        "profile_sha256": "b" * 64,
    }
    if mismatch == "allocation":
        receipt["allocation_id"] = "different-allocation"
    if mismatch == "profile":
        receipt["profile_sha256"] = "c" * 64

    async def health(reader, writer):
        try:
            incoming = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            assert b"Authorization: Bearer synthetic-runner-bearer" in incoming
            body = json.dumps(receipt).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(
        health, "127.0.0.1", 0, ssl=context, ssl_handshake_timeout=2
    )
    try:
        config = gateway_settings(
            tmp_path,
            runner_ca_file=str(tls_material / "ca.crt"),
            runner_cert_file=str(tls_material / "client.crt"),
            runner_key_file=str(tls_material / "client.key"),
            runner_server_name="wrong.test.invalid"
            if mismatch == "hostname"
            else "runner.test.invalid",
        )
        port = server.sockets[0].getsockname()[1]
        accepted = await RunnerProbe(config).verify(f"https://127.0.0.1:{port}", "qa-allocation")
        assert accepted is (mismatch is None)
    finally:
        server.close()
        await server.wait_closed()


class ControlledCloud:
    def __init__(self):
        self.posts = 0
        self.deleted = []
        self.servers = {}
        self.accepted = asyncio.Event()
        self.release = asyncio.Event()

    async def create(self, payload):
        self.posts += 1
        self.accepted.set()
        await self.release.wait()
        self.servers[101] = {
            "id": 101,
            "name": payload["name"],
            "comment": payload["comment"],
            "networks": [{"type": "public", "ips": [{"ip": "8.8.4.4"}]}],
        }
        raise GatewayError("TRANSPORT_OR_TIMEOUT", "synthetic lost acknowledgement", unknown=True)

    async def find(self, name, marker):
        return [
            value
            for value in self.servers.values()
            if value["name"] == name and value["comment"] == marker
        ]

    async def get(self, cloud_id):
        return self.servers.get(cloud_id)

    async def delete(self, cloud_id):
        self.deleted.append(cloud_id)
        self.servers.pop(cloud_id, None)


class SyntheticProbe:
    async def verify(self, endpoint, allocation_id):
        return True


class SyntheticNetworkPolicy:
    """Qualified firewall contract fixture, never proof of Timeweb network isolation."""

    def preflight(self):
        return None

    async def verify_and_attach(self, row, server):
        return True


@pytest.mark.asyncio
async def test_parallel_gateway_instances_never_redispatch_an_unknown_create(tmp_path):
    config = gateway_settings(tmp_path)
    cloud = ControlledCloud()
    first = TimewebGateway(
        config, cloud=cloud, probe=SyntheticProbe(), network_policy=SyntheticNetworkPolicy()
    )
    request = AllocateRequest(allocation_id="qa-allocation", purpose="baseline")
    pending = asyncio.create_task(first.create(request))
    try:
        await asyncio.wait_for(cloud.accepted.wait(), timeout=2)
        second = TimewebGateway(
            config, cloud=cloud, probe=SyntheticProbe(), network_policy=SyntheticNetworkPolicy()
        )
        third = TimewebGateway(
            config, cloud=cloud, probe=SyntheticProbe(), network_policy=SyntheticNetworkPolicy()
        )
        assert (await second.create(request)).state == "UNKNOWN"
        assert (await third.create(request)).state == "UNKNOWN"
        assert cloud.posts == 1
        with pytest.raises(GatewayError, match="one active or unresolved"):
            await second.create(AllocateRequest(allocation_id="other-allocation", purpose="final"))
    finally:
        cloud.release.set()
        await pending
    assert (await third.status("qa-allocation")).state == "READY"
    assert cloud.posts == 1


@pytest.mark.asyncio
async def test_unqualified_external_firewall_blocks_before_cloud_create(tmp_path):
    cloud = ControlledCloud()
    gateway = TimewebGateway(gateway_settings(tmp_path), cloud=cloud, probe=SyntheticProbe())
    with pytest.raises(GatewayError) as denied:
        await gateway.create(AllocateRequest(allocation_id="qa-allocation", purpose="baseline"))
    assert denied.value.code == "EXTERNAL_FIREWALL_PROFILE_REQUIRED"
    assert cloud.posts == 0 and cloud.servers == {}


def test_gateway_delete_api_cannot_target_foreign_cloud_id(tmp_path):
    config = gateway_settings(tmp_path)
    cloud = ControlledCloud()
    cloud.servers[900] = {"id": 900, "name": "foreign-server", "comment": "not-a-gateway-marker"}
    with TestClient(create_app(config, cloud=cloud, probe=SyntheticProbe())) as client:
        denied = client.delete(
            "/v1/vms/900", headers={"Authorization": "Bearer " + config.bearer_secret}
        )
        assert denied.status_code == 404
    assert cloud.deleted == [] and 900 in cloud.servers


def renderer_module():
    filename = Path(__file__).resolve().parents[1] / "deploy/timeweb/render_runner.py"
    spec = importlib.util.spec_from_file_location("_v2c_qa_timeweb_renderer", filename)
    renderer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = renderer
    spec.loader.exec_module(renderer)
    return renderer


def test_rendered_template_preserves_runner_profile_and_private_file_boundaries(
    tmp_path, tls_material
):
    """Inspect the actual rendered cloud-init artifact without booting or provisioning it."""
    renderer = renderer_module()
    settings = RunnerSettings(
        data_dir=Path("/var/lib/v2c-runner"),
        bearer_secret="synthetic-runner-bearer",
        image="test/image@sha256:" + "d" * 64,
        cpu_limit=1,
        memory_bytes=1024**3,
        pids_limit=128,
        tmpfs_bytes=64 * 1024**2,
        workspace_disk_bytes=512 * 1024**2,
        max_archive_bytes=64 * 1024**2,
        max_expanded_bytes=256 * 1024**2,
        max_files=20000,
        max_command_seconds=120,
        max_output_bytes=65536,
        uid=10001,
        gid=10001,
    )
    settings_file = tmp_path / "runner-settings.json"
    settings_file.write_text(settings.model_dump_json())
    profile = renderer.BuildProfile(
        source_url="https://artifacts.test.invalid/v2c.tar.gz",
        source_sha256="f" * 64,
        runner_settings_file=settings_file,
        tls_server_cert_file=tls_material / "server.crt",
        tls_server_key_file=tls_material / "server.key",
        tls_client_ca_file=tls_material / "ca.crt",
        runner_server_name="runner.test.invalid",
        control_cidrs=["8.8.4.4/32"],
        runner_port=9449,
    )
    text, receipt = renderer.render(profile)
    assert text.startswith("#cloud-config\n")
    cloud_init = json.loads(text.split("\n", 1)[1])
    files = {item["path"]: item for item in cloud_init["write_files"]}
    rendered = RunnerSettings.model_validate_json(files["/etc/v2c-runner/runner.json"]["content"])
    assert rendered.allocation_id == "__V2C_ALLOCATION_ID__"
    assert (
        rendered.profile_sha256() == settings.profile_sha256() == receipt["runner_profile_sha256"]
    )
    bootstrap = json.loads(files["/etc/v2c-runner/bootstrap.json"]["content"])
    assert bootstrap["workspace_disk_bytes"] == rendered.workspace_disk_bytes
    assert bootstrap["images"] == [settings.image]
    assert bootstrap["expected_profile_sha256"] == rendered.profile_sha256()
    for path in (
        "/etc/v2c-runner/runner.json",
        "/etc/v2c-runner/server.key",
        "/etc/v2c-runner/bootstrap.json",
    ):
        assert files[path]["permissions"] == "0600" and files[path]["owner"] == "root:root"
    service = files["/etc/systemd/system/v2c-runner.service"]["content"]
    assert (
        "--port 9449" in service
        and "Requires=docker.service v2c-runner-firewall.service" in service
    )
    assert "ConditionPathExists=/etc/v2c-runner/bootstrap-ready" in service
    assert receipt["contains_operator_secrets"] is True
    assert receipt["cloud_boot_tested"] is False


@pytest.mark.parametrize("runner_port", [1, 9, 8443, 65535])
def test_drop_whitelist_has_only_control_ingress_and_two_explicit_egress_exceptions(runner_port):
    """Enumerate packet predicates independently; this is rule-shape, not cloud packet evidence."""
    control = ipaddress.ip_network("8.8.4.1/32")
    rules = public_firewall_rules(str(control), runner_port)
    assert len(rules) == 3
    addresses = [
        "8.8.3.255",
        "8.8.4.0",
        "8.8.4.1",
        "8.8.4.3",
        "8.8.4.4",
        "1.1.1.1",
        "10.0.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.0.2.0",
        "192.0.2.1",
        "192.0.2.2",
        "::1",
        "2001:db8::",
        "2001:db8::1",
        "2001:db8::2",
        "2001:4860:4860::8888",
    ]
    ports = {
        0,
        1,
        8,
        9,
        10,
        max(0, runner_port - 1),
        runner_port,
        min(65535, runner_port + 1),
        65535,
    }

    def admitted(direction, protocol, address, port):
        for rule in rules:
            network = ipaddress.ip_network(rule["cidr"])
            if (
                rule["direction"] != direction
                or rule["protocol"] != protocol
                or address.version != network.version
                or address not in network
            ):
                continue
            low, _, high = rule["port"].partition("-")
            if int(low) <= port <= int(high or low):
                return True
        return False

    for raw_address in addresses:
        address = ipaddress.ip_address(raw_address)
        for direction in ("ingress", "egress"):
            for protocol in (
                "tcp",
                "udp",
                "icmp",
                "tcp6",
                "udp6",
                "icmp6",
                "gre",
                "esp",
                "ipv6-in-ipv4",
            ):
                for port in ports:
                    expected = (
                        (
                            direction == "ingress"
                            and protocol == "tcp"
                            and address.version == 4
                            and address in control
                            and port == runner_port
                        )
                        or (
                            direction == "egress"
                            and protocol == "tcp"
                            and raw_address == "192.0.2.1"
                            and port == 9
                        )
                        or (
                            direction == "egress"
                            and protocol == "tcp6"
                            and raw_address == "2001:db8::1"
                            and port == 9
                        )
                    )
                    assert admitted(direction, protocol, address, port) == expected, (
                        direction,
                        protocol,
                        raw_address,
                        port,
                    )


@pytest.mark.parametrize(
    "control",
    [
        "8.8.4.0/30",
        "0.0.0.0/0",
        "0.0.0.0/1",
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "192.0.2.0/24",
        "224.0.0.0/4",
        "::/0",
    ],
)
def test_public_control_cidr_rejects_nonpublic_or_mixed_ranges(control):
    with pytest.raises(ValueError):
        public_firewall_rules(control, 8443)


@pytest.mark.parametrize("control", ["0.0.0.0/1", "224.0.0.0/4"])
def test_template_rejects_the_same_mixed_or_multicast_control_ranges(tmp_path, control):
    with pytest.raises(ValueError):
        renderer_module().BuildProfile(
            source_url="https://artifacts.test.invalid/v2c.tar.gz",
            source_sha256="f" * 64,
            runner_settings_file=tmp_path / "settings.json",
            tls_server_cert_file=tmp_path / "server.crt",
            tls_server_key_file=tmp_path / "server.key",
            tls_client_ca_file=tmp_path / "ca.crt",
            runner_server_name="runner.test.invalid",
            control_cidrs=[control],
        )


def test_actual_compose_env_layers_preserve_control_model_key_and_gateway_separation(tmp_path):
    """Use Compose's own interpolation/precedence, not a handwritten dotenv simulator."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip(
            "Docker Compose unavailable; actual env-layer configuration gate remains unexecuted"
        )
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DOCKER_CONFIG": str(tmp_path / "docker-config"),
    }
    available = subprocess.run(
        [docker, "compose", "version"], env=environment, capture_output=True, timeout=10
    )
    if available.returncode:
        pytest.skip(
            "Docker Compose plugin unavailable; actual env-layer configuration gate remains unexecuted"
        )
    root = Path(__file__).resolve().parents[1]
    shutil.copyfile(root / "compose.yaml", tmp_path / "compose.yaml")
    # Compose validates required settings before it can expose model-key precedence.
    (tmp_path / ".env").write_text(
        (root / ".env.example")
        .read_text()
        .replace("V2C_API_TOKEN=\n", "V2C_API_TOKEN=synthetic-compose-api-token\n")
        .replace(
            "V2C_POSTGRES_PASSWORD=\n",
            "V2C_POSTGRES_PASSWORD=synthetic-compose-postgres-password\n",
        )
        .replace("GROQ_API_KEY=", "GROQ_API_KEY=synthetic-inherited-free-key")
    )
    shutil.copyfile(root / ".env.timeweb.example", tmp_path / ".env.timeweb")
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            ".env",
            "--env-file",
            ".env.timeweb",
            "--profile",
            "timeweb-gateway",
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for name in ("api", "worker", "migrate"):
        assert services[name]["environment"]["GROQ_API_KEY"] == "synthetic-inherited-free-key"
        assert services[name]["environment"]["V2C_MODEL_PRESET"] == "groq-free-gpt-oss-120b"
    assert "GROQ_API_KEY" not in services["timeweb-gateway"].get("environment", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("denial_status", [401, 403])
async def test_known_timeweb_auth_rejection_is_terminal_and_releases_capacity(
    tmp_path, denial_status
):
    """Run HTTP classification and durable allocation logic together, without a cloud request."""
    config = gateway_settings(tmp_path)
    posts, servers = [], {}
    authorized = False

    def wire(request):
        nonlocal authorized
        if request.method == "POST" and request.url.path == "/api/v1/servers":
            payload = json.loads(request.content)
            posts.append(payload)
            if not authorized:
                return httpx.Response(
                    denial_status, json={"error": "synthetic auth refusal"}, request=request
                )
            server = {
                "id": 101,
                "name": payload["name"],
                "comment": payload["comment"],
                "networks": [{"type": "public", "ips": [{"ip": "8.8.4.4"}]}],
            }
            servers[101] = server
            return httpx.Response(201, json={"server": server}, request=request)
        if request.method == "GET" and request.url.path == "/api/v1/servers/101":
            return httpx.Response(200, json={"server": servers[101]}, request=request)
        raise AssertionError("unexpected synthetic Timeweb request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        gateway = TimewebGateway(
            config,
            cloud=TimewebClient(config, client),
            probe=SyntheticProbe(),
            network_policy=SyntheticNetworkPolicy(),
        )
        request = AllocateRequest(allocation_id="rejected-allocation", purpose="baseline")
        first = await gateway.create(request)
        assert first.state == "DESTROYED" and first.reason == "CREATE_REJECTED_AUTH"
        assert (await gateway.create(request)).state == "DESTROYED"
        assert len(posts) == 1 and servers == {}
        authorized = True
        changed = config.model_copy(update={"api_token": "synthetic-corrected-cloud-key"})
        restarted = TimewebGateway(
            changed,
            cloud=TimewebClient(changed, client),
            probe=SyntheticProbe(),
            network_policy=SyntheticNetworkPolicy(),
        )
        assert (await restarted.create(request)).state == "DESTROYED"
        assert len(posts) == 1
        accepted = await restarted.create(
            AllocateRequest(allocation_id="new-explicit-allocation", purpose="baseline")
        )
        assert accepted.state == "READY" and len(posts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "server_failure", "malformed_success"])
async def test_uncertain_create_still_retains_capacity_without_resending(tmp_path, failure):
    config = gateway_settings(tmp_path)
    posts = []

    def wire(request):
        if request.method == "POST" and request.url.path == "/api/v1/servers":
            posts.append(json.loads(request.content))
            if failure == "transport":
                raise httpx.ReadTimeout("synthetic lost create reply", request=request)
            return httpx.Response(
                500 if failure == "server_failure" else 201,
                json={"not_a_confirmed_server": True},
                request=request,
            )
        if request.method == "GET" and request.url.path == "/api/v1/servers":
            return httpx.Response(200, json={"servers": [], "meta": {"total": 0}}, request=request)
        raise AssertionError("unexpected synthetic Timeweb request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        gateway = TimewebGateway(
            config,
            cloud=TimewebClient(config, client),
            probe=SyntheticProbe(),
            network_policy=SyntheticNetworkPolicy(),
        )
        request = AllocateRequest(allocation_id="unknown-allocation", purpose="baseline")
        assert (await gateway.create(request)).state == "UNKNOWN"
        assert (await gateway.create(request)).state == "UNKNOWN"
        with pytest.raises(GatewayError) as blocked:
            await gateway.create(
                AllocateRequest(allocation_id="another-allocation", purpose="final")
            )
        assert blocked.value.code == "CAPACITY"
        assert len(posts) == 1
