import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from v2c.timeweb_gateway import (
    AllocateRequest,
    GatewayError,
    GatewaySettings,
    RunnerProbe,
    TimewebClient,
    TimewebGateway,
    create_app,
)


def settings(tmp_path, **changes):
    template = tmp_path / "server.json"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(
        json.dumps(
            {"preset_id": 11, "image_id": "550e8400-e29b-41d4-a716-446655440000", "project_id": 33}
        )
    )
    cloud_init = tmp_path / "cloud-init.yaml"
    cloud_init.write_text(
        "#cloud-config\nwrite_files:\n  - path: /etc/v2c/allocation\n    content: __V2C_ALLOCATION_ID__\n"
    )
    values = dict(
        data_dir=tmp_path / "data",
        api_token="fake-cloud-token-not-a-live-key",
        bearer_secret="fake-gateway-credential",
        ownership_secret="fake-ownership-secret-long-enough-for-tests",
        server_template_file=template,
        cloud_init_template_file=cloud_init,
        expected_runner_profile_sha256="a" * 64,
        runner_ca_file="/test/ca",
        runner_cert_file="/test/cert",
        runner_key_file="/test/key",
        runner_bearer_secret="fake-runner-credential",
        runner_server_name="runner.v2c.internal",
        request_timeout_seconds=1,
        max_response_bytes=20000,
        max_list_pages=3,
        list_page_size=2,
        runner_port=8443,
    )
    values.update(changes)
    return GatewaySettings(**values)


class Probe:
    def __init__(self, ready=True):
        self.ready = ready
        self.calls = []

    async def verify(self, endpoint, allocation_id):
        self.calls.append((endpoint, allocation_id))
        return self.ready


class NetworkPolicy:
    """Explicit qualified-provider stand-in; not Timeweb production proof."""

    def preflight(self):
        return None

    async def verify_and_attach(self, row, server):
        return True


class Cloud:
    def __init__(self):
        self.servers = {}
        self.posts = []
        self.deletes = []
        self.gets = []
        self.create_timeout = False
        self.delete_timeout = False
        self.create_effect = True
        self.delete_blocked = False

    async def create(self, payload):
        self.posts.append(payload)
        server = {
            "id": 99 + len(self.posts),
            "name": payload["name"],
            "comment": payload["comment"],
            "status": "on",
            "networks": [
                {
                    "type": "public",
                    "nat_mode": "dnat_and_snat",
                    "ips": [{"type": "ipv4", "ip": "203.0.113.10", "is_main": True}],
                }
            ],
        }
        if self.create_effect:
            self.servers[server["id"]] = server
        if self.create_timeout:
            raise GatewayError("TRANSPORT_OR_TIMEOUT", "lost create acknowledgement", unknown=True)
        return server

    async def get(self, cloud_id):
        self.gets.append(cloud_id)
        return self.servers.get(cloud_id)

    async def find(self, name, marker):
        return [
            server
            for server in self.servers.values()
            if server["name"] == name and server["comment"] == marker
        ]

    async def delete(self, cloud_id):
        self.deletes.append(cloud_id)
        if self.delete_blocked:
            raise GatewayError("DELETE_CONFIRMATION_REQUIRED", "blocked")
        self.servers.pop(cloud_id, None)
        if self.delete_timeout:
            raise GatewayError(
                "TRANSPORT_OR_TIMEOUT", "lost deletion acknowledgement", unknown=True
            )


def test_duplicate_create_preserves_intent_and_freshness(tmp_path):
    async def run():
        config = settings(tmp_path)
        cloud = Cloud()
        probe = Probe()
        gateway = TimewebGateway(config, cloud=cloud, probe=probe, network_policy=NetworkPolicy())
        request = AllocateRequest(allocation_id="alloc1", purpose="baseline")
        first = await gateway.create(request)
        assert first.vm_id == "alloc1" and first.state == "READY"
        assert first.runner_endpoint == "https://203.0.113.10:8443"
        restarted = TimewebGateway(config, cloud=cloud, probe=probe, network_policy=NetworkPolicy())
        duplicate = await restarted.create(request)
        assert duplicate == first and len(cloud.posts) == 1
        assert "__V2C_ALLOCATION_ID__" not in cloud.posts[0]["cloud_init"]
        assert config.api_token not in json.dumps(cloud.posts[0])
        with pytest.raises(GatewayError) as failure:
            await restarted.create(AllocateRequest(allocation_id="alloc1", purpose="different"))
        assert failure.value.code == "CONFLICT" and len(cloud.posts) == 1

    asyncio.run(run())


def test_unknown_create_reconciles_without_any_second_post(tmp_path):
    async def run():
        cloud = Cloud()
        cloud.create_timeout = True
        gateway = TimewebGateway(
            settings(tmp_path), cloud=cloud, probe=Probe(), network_policy=NetworkPolicy()
        )
        request = AllocateRequest(allocation_id="alloc1", purpose="baseline")
        assert (await gateway.create(request)).state == "UNKNOWN"
        assert (await gateway.create(request)).state == "READY"
        assert len(cloud.posts) == 1

    asyncio.run(run())


def test_absence_after_unknown_create_never_licenses_repeat_or_releases_capacity(tmp_path):
    async def run():
        cloud = Cloud()
        cloud.create_timeout = True
        cloud.create_effect = False
        gateway = TimewebGateway(
            settings(tmp_path), cloud=cloud, probe=Probe(), network_policy=NetworkPolicy()
        )
        req = AllocateRequest(allocation_id="alloc1", purpose="baseline")
        assert (await gateway.create(req)).state == "UNKNOWN"
        assert (await gateway.create(req)).state == "UNKNOWN"
        assert (await gateway.destroy("alloc1")).state == "UNKNOWN"
        with pytest.raises(GatewayError) as failure:
            await gateway.create(AllocateRequest(allocation_id="alloc2", purpose="final"))
        assert failure.value.code == "CAPACITY" and len(cloud.posts) == 1 and cloud.deletes == []

    asyncio.run(run())


def test_crash_after_committing_intent_before_cloud_post_does_not_send_on_restart(tmp_path):
    async def run():
        cloud = Cloud()
        config = settings(tmp_path)
        first = TimewebGateway(config, cloud=cloud, probe=Probe(), network_policy=NetworkPolicy())
        req = AllocateRequest(allocation_id="alloc1", purpose="baseline")
        # Stop exactly at the durable intent cut, before the external request.
        name, marker = first.identity("alloc1")
        import hashlib

        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "request": req.model_dump(),
                    "template_sha256": first.template_hash,
                    "profile_sha256": config.expected_runner_profile_sha256,
                    "network_profile_sha256": first.network_profile_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        first.journal.intent(req, request_hash, name, marker)
        restarted = TimewebGateway(
            config, cloud=cloud, probe=Probe(), network_policy=NetworkPolicy()
        )
        assert (await restarted.create(req)).state == "UNKNOWN"
        assert cloud.posts == []

    asyncio.run(run())


def test_delete_acceptance_and_unknown_ack_need_authoritative_readback(tmp_path):
    async def run():
        cloud = Cloud()
        gateway = TimewebGateway(
            settings(tmp_path), cloud=cloud, probe=Probe(), network_policy=NetworkPolicy()
        )
        await gateway.create(AllocateRequest(allocation_id="alloc1", purpose="baseline"))
        cloud.delete_timeout = True
        assert (await gateway.destroy("alloc1")).state == "DESTROYING"
        assert (await gateway.status("alloc1")).state == "DESTROYED"
        assert (await gateway.destroy("alloc1")).state == "DESTROYED"
        assert cloud.deletes == [100]
        await gateway.create(AllocateRequest(allocation_id="alloc2", purpose="final"))
        assert len(cloud.posts) == 2

    asyncio.run(run())


def test_deletion_confirmation_and_ownership_mismatch_fail_closed(tmp_path):
    async def run():
        cloud = Cloud()
        gateway = TimewebGateway(
            settings(tmp_path), cloud=cloud, probe=Probe(), network_policy=NetworkPolicy()
        )
        await gateway.create(AllocateRequest(allocation_id="alloc1", purpose="baseline"))
        cloud.delete_blocked = True
        assert (await gateway.destroy("alloc1")).state == "DESTROYING"
        assert gateway.journal.get("alloc1")["reason"] == "DELETE_CONFIRMATION_REQUIRED"
        assert (await gateway.destroy("alloc1")).state == "DESTROYING"
        assert cloud.deletes == [100]
        cloud.servers[100]["comment"] = "belongs to somebody else"
        assert (await gateway.destroy("alloc1")).state == "UNKNOWN"
        assert cloud.deletes == [100]
        with pytest.raises(GatewayError) as failure:
            await gateway.destroy("99999")
        assert failure.value.code == "NOT_FOUND" and cloud.deletes == [100]

    asyncio.run(run())


def test_probe_failure_prevents_ready(tmp_path):
    async def run():
        cloud = Cloud()
        gateway = TimewebGateway(
            settings(tmp_path), cloud=cloud, probe=Probe(False), network_policy=NetworkPolicy()
        )
        result = await gateway.create(AllocateRequest(allocation_id="alloc1", purpose="baseline"))
        assert result.state == "CREATING"
        assert gateway.journal.get("alloc1")["reason"] == "RUNNER_NOT_READY"

    asyncio.run(run())


def test_authorization_and_body_limits_precede_cloud_calls(tmp_path):
    cloud = Cloud()
    config = settings(tmp_path)
    with TestClient(
        create_app(config, cloud=cloud, probe=Probe(), network_policy=NetworkPolicy())
    ) as client:
        assert (
            client.post(
                "/v1/vms", json={"allocation_id": "alloc1", "purpose": "baseline"}
            ).status_code
            == 401
        )
        assert client.post("/v1/vms", content="x" * 4097).status_code == 413
        assert not cloud.posts
        headers = {"Authorization": "Bearer " + config.bearer_secret}
        assert client.delete("/v1/vms/not-owned", headers=headers).status_code == 404
        assert not cloud.deletes


def test_timeweb_wire_contract_pagination_and_no_hidden_retries(tmp_path):
    async def run():
        config = settings(tmp_path)
        observed = []

        def handler(req):
            observed.append(req)
            assert req.headers["Authorization"] == "Bearer " + config.api_token
            assert req.url.host == "api.timeweb.cloud"
            if req.method == "POST":
                payload = json.loads(req.content)
                assert "cloud_init" in payload and "user_data" not in payload
                assert "Idempotency-Key" not in req.headers
                raise httpx.ReadTimeout("accepted but response lost", request=req)
            offset = int(req.url.params["offset"])
            servers = (
                [
                    {"id": 1, "name": "other", "comment": "other"},
                    {"id": 2, "name": "other2", "comment": "other"},
                ]
                if offset == 0
                else [{"id": 3, "name": "match", "comment": "marker"}]
            )
            return httpx.Response(200, json={"servers": servers, "meta": {"total": 3}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TimewebClient(config, http)
            with pytest.raises(GatewayError) as failure:
                await client.create({"cloud_init": "#cloud-config", "name": "n", "comment": "m"})
            assert failure.value.unknown and len(observed) == 1
            matches = await client.find("match", "marker")
            assert matches == [{"id": 3, "name": "match", "comment": "marker"}]
            assert len(observed) == 3

    asyncio.run(run())


def test_runner_probe_uses_sni_and_checks_allocation_and_profile(tmp_path):
    async def run():
        config = settings(tmp_path)
        value = {
            "status": "ok",
            "protocol": "v1",
            "allocation_id": "alloc1",
            "profile_sha256": config.expected_runner_profile_sha256,
        }
        calls = []

        def handler(req):
            calls.append(req)
            assert req.url.host == "203.0.113.10"
            assert req.headers["Host"] == config.runner_server_name
            assert req.extensions["sni_hostname"] == config.runner_server_name
            return httpx.Response(200, json=value)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            probe = RunnerProbe(config, http)
            assert await probe.verify("https://203.0.113.10:8443", "alloc1")
            assert not await probe.verify("https://203.0.113.10:8443", "alloc2")
            value["profile_sha256"] = "b" * 64
            assert not await probe.verify("https://203.0.113.10:8443", "alloc1")
            assert len(calls) == 3

    asyncio.run(run())


def test_default_deny_whitelist_has_only_control_and_two_reserved_exceptions():
    from v2c.timeweb_gateway import public_firewall_rules

    rules = public_firewall_rules("8.8.8.8/32", 8443)
    assert rules == sorted(
        [
            {"direction": "ingress", "protocol": "tcp", "port": "8443", "cidr": "8.8.8.8/32"},
            {"direction": "egress", "protocol": "tcp", "port": "9", "cidr": "192.0.2.1/32"},
            {"direction": "egress", "protocol": "tcp6", "port": "9", "cidr": "2001:db8::1/128"},
        ],
        key=lambda item: json.dumps(item, sort_keys=True),
    )
    # Reserved exceptions are actual allowed destinations. No literal zero-egress
    # or untested runtime stateful guarantee follows from this local compiler.
    for invalid in (
        "192.0.2.1/32",
        "10.0.0.1/32",
        "169.254.169.254/32",
        "127.0.0.1/32",
        "0.0.0.0/0",
        "2001:db8::1/128",
        "224.0.0.0/4",
        "224.0.0.1/32",
        "0.0.0.0/1",
        "8.8.8.0/24",
        "240.0.0.1/32",
    ):
        with pytest.raises(ValueError):
            public_firewall_rules(invalid, 8443)


def test_missing_external_firewall_stops_before_any_paid_create(tmp_path):
    async def run():
        cloud = Cloud()
        gateway = TimewebGateway(settings(tmp_path), cloud=cloud, probe=Probe())
        with pytest.raises(GatewayError) as failure:
            await gateway.create(AllocateRequest(allocation_id="alloc1", purpose="baseline"))
        assert failure.value.code == "EXTERNAL_FIREWALL_PROFILE_REQUIRED" and not cloud.posts
        with gateway.journal.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM allocation").fetchone()[0] == 0

    asyncio.run(run())


def test_native_firewall_attach_has_durable_intent_and_exact_readback(tmp_path):
    from v2c.timeweb_gateway import TimewebPublicNetworkPolicy, public_firewall_rules

    async def run():
        config = settings(
            tmp_path, list_page_size=100, firewall_group_id="fw1", control_cidr="8.8.8.8/32"
        )
        calls = []
        attached = []
        cloud = Cloud()
        gateway = TimewebGateway(config, cloud=cloud, probe=Probe(), network_policy=NetworkPolicy())
        await gateway.create(AllocateRequest(allocation_id="alloc1", purpose="baseline"))

        async def request(method, path, payload=None, params=None, **kwargs):
            calls.append((method, path, params))
            if path == "/api/v1/firewall/groups/fw1":
                return {"group": {"id": "fw1", "policy": "DROP"}}
            if path.endswith("/rules"):
                return {
                    "rules": [
                        {**rule, "group_id": "fw1", "id": str(index)}
                        for index, rule in enumerate(
                            public_firewall_rules(config.control_cidr, 8443)
                        )
                    ],
                    "meta": {"total": len(public_firewall_rules(config.control_cidr, 8443))},
                }
            if method == "POST":
                with gateway.journal.connect() as db:
                    assert db.execute(
                        "SELECT 1 FROM firewall_intent WHERE allocation_id=?", ("alloc1",)
                    ).fetchone()
                assert params == {"resource_type": "server"} and payload is None
                attached.append({"id": "fw1", "policy": "DROP"})
                return {"resource": {"id": 100, "type": "server"}}
            return {"groups": attached.copy(), "meta": {"total": len(attached)}}

        cloud.request = request
        policy = TimewebPublicNetworkPolicy(config, cloud, gateway.journal)
        policy.preflight()
        assert await policy.verify_and_attach(gateway.journal.get("alloc1"), cloud.servers[100])
        assert await policy.verify_and_attach(gateway.journal.get("alloc1"), cloud.servers[100])
        assert sum(method == "POST" for method, path, params in calls) == 1

    asyncio.run(run())


def test_post_lockdown_health_failure_blocks_ready(tmp_path):
    class Lockdown:
        def preflight(self):
            return None

        async def verify_and_attach(self, row, server):
            probe.ready = False
            return True

    async def run():
        gateway = TimewebGateway(
            settings(tmp_path), cloud=Cloud(), probe=probe, network_policy=Lockdown()
        )
        result = await gateway.create(AllocateRequest(allocation_id="alloc1", purpose="baseline"))
        assert result.state == "UNKNOWN"
        assert gateway.journal.get("alloc1")["reason"] == "NETWORK_LOCKDOWN_HEALTH_FAILED"

    probe = Probe()
    asyncio.run(run())


@pytest.mark.parametrize("rejected_status", [401, 403])
def test_verified_create_auth_rejection_is_terminal_and_frees_capacity(tmp_path, rejected_status):
    async def run():
        config = settings(tmp_path)
        calls = []
        servers = {}

        def handler(request):
            calls.append((request.method, request.url.path, request.headers["Authorization"]))
            if request.method == "POST":
                if request.headers["Authorization"] == "Bearer " + config.api_token:
                    return httpx.Response(rejected_status, json={"error_code": "not_authorized"})
                payload = json.loads(request.content)
                server = {
                    "id": 123,
                    "name": payload["name"],
                    "comment": payload["comment"],
                    "status": "on",
                    "networks": [
                        {
                            "type": "public",
                            "ips": [{"type": "ipv4", "ip": "203.0.113.10", "is_main": True}],
                        }
                    ],
                }
                servers[123] = server
                return httpx.Response(201, json={"server": server})
            assert request.method == "GET"
            return httpx.Response(200, json={"server": servers[123]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            gateway = TimewebGateway(
                config,
                cloud=TimewebClient(config, http),
                probe=Probe(),
                network_policy=NetworkPolicy(),
            )
            rejected = await gateway.create(
                AllocateRequest(allocation_id="bad-auth", purpose="baseline")
            )
            assert rejected.state == "DESTROYED" and rejected.reason == "CREATE_REJECTED_AUTH"
            assert not servers and len(calls) == 1
            # Caller cleanup and duplicate delivery remain terminal and cannot
            # issue a GET/DELETE for a resource that never existed.
            assert (await gateway.destroy("bad-auth")).state == "DESTROYED"
            assert (
                await gateway.create(AllocateRequest(allocation_id="bad-auth", purpose="baseline"))
            ) == rejected
            assert len(calls) == 1
            corrected = config.model_copy(update={"api_token": "corrected-test-token-not-live"})
            restarted = TimewebGateway(
                corrected,
                cloud=TimewebClient(corrected, http),
                probe=Probe(),
                network_policy=NetworkPolicy(),
            )
            assert (
                await restarted.create(
                    AllocateRequest(allocation_id="bad-auth", purpose="baseline")
                )
            ).state == "DESTROYED"
            created = await restarted.create(
                AllocateRequest(allocation_id="corrected-auth", purpose="baseline")
            )
            assert created.state == "READY" and len(servers) == 1
            assert sum(method == "POST" for method, path, auth in calls) == 2

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "malformed_201", "provider_500"])
def test_ambiguous_create_still_reserves_capacity(tmp_path, failure):
    async def run():
        config = settings(tmp_path)
        posts = []

        def handler(request):
            if request.method == "POST":
                posts.append(request)
                if failure == "timeout":
                    raise httpx.ReadTimeout("unknown create effect", request=request)
                if failure == "provider_500":
                    return httpx.Response(500, json={"error_code": "unknown_failure"})
                return httpx.Response(201, json={"server": {"name": "incomplete server identity"}})
            return httpx.Response(200, json={"servers": [], "meta": {"total": 0}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            gateway = TimewebGateway(
                config,
                cloud=TimewebClient(config, http),
                probe=Probe(),
                network_policy=NetworkPolicy(),
            )
            request = AllocateRequest(allocation_id="uncertain", purpose="baseline")
            assert (await gateway.create(request)).state == "UNKNOWN"
            assert (await gateway.create(request)).state == "UNKNOWN"
            with pytest.raises(GatewayError) as error:
                await gateway.create(AllocateRequest(allocation_id="another", purpose="baseline"))
            assert error.value.code == "CAPACITY" and len(posts) == 1

    asyncio.run(run())
