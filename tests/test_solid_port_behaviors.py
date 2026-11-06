"""Shared consumer assertions over supported adapters and deterministic doubles."""

import asyncio
import base64
import hashlib
import io

import httpx
import pytest
from test_pipeline import ORIGINAL, PATH, FixtureRunner, FixtureVMs, zip_bytes
from test_runner import ControlledBackend
from test_runner import settings as runner_settings
from test_timeweb_gateway import Cloud, NetworkPolicy, Probe
from test_timeweb_gateway import settings as gateway_settings

from v2c.adapters.runner import HTTPVMProvider, MTLSConfig, RunnerClient
from v2c.contracts.runner import CommandRequest, PrepareRequest, RunnerError, VMAllocation
from v2c.contracts.storage import StorageError
from v2c.runner_service import create_app as runner_app
from v2c.storage import LocalArtifactStore, S3ArtifactStore
from v2c.timeweb_gateway import create_app as gateway_app


def tls_config(secret):
    return MTLSConfig(
        endpoint="https://contract.test",
        ca_file="unused",
        cert_file="unused",
        key_file="unused",
        bearer_secret=secret,
        timeout_seconds=2,
        max_response_bytes=1000000,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("implementation", ["runner-http", "fixture"])
async def test_runner_identity_replay_fence_cancel_and_borrowed_client(tmp_path, implementation):
    settings = runner_settings(tmp_path)
    backend = ControlledBackend()
    backend.release.set()
    app = runner_app(settings, backend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        runner = (
            RunnerClient(tls_config(settings.bearer_secret), client)
            if implementation == "runner-http"
            else FixtureRunner(
                VMAllocation(vm_id="fixture", state="READY", runner_endpoint="https://fixture.test")
            )
        )
        archive = zip_bytes({PATH: ORIGINAL})
        request = PrepareRequest(
            workspace_id="workspace1",
            attempt_id="attempt1",
            generation=1,
            archive_b64=base64.b64encode(archive).decode(),
            archive_sha256=hashlib.sha256(archive).hexdigest(),
            input_manifest={},
        )
        assert (await runner.prepare(request))["state"] == "READY"
        command = CommandRequest(
            command_id="command1",
            workspace_id="workspace1",
            attempt_id="attempt1",
            generation=1,
            argv=["fixture-state"],
            timeout_seconds=10,
        )
        result = await runner.execute(command)
        for _ in range(100):
            if result.state not in {"ACCEPTED", "RUNNING"}:
                break
            await asyncio.sleep(0.01)
            result = await runner.status(command.command_id)
        assert result.command_id == command.command_id
        assert result.request_hash == command.content_hash()
        assert result.state == "SUCCEEDED" and result.stop_confirmed
        assert await runner.execute(command) == result
        assert await runner.status(command.command_id) == result
        assert await runner.cancel(command.command_id) == result
        checkpoint = await runner.checkpoint("workspace1")
        assert (await runner.prepare(request))["state"] == "READY"
        # ZIP timestamps need not be stable: compare decoded source bytes.
        from test_pipeline import read_zip

        assert read_zip(await runner.checkpoint("workspace1")) == read_zip(checkpoint)
        with pytest.raises(RunnerError) as input_conflict:
            await runner.prepare(request.model_copy(update={"archive_sha256": "b" * 64}))
        assert input_conflict.value.code == "CONFLICT"
        with pytest.raises(RunnerError) as conflict:
            await runner.execute(command.model_copy(update={"argv": ["different"]}))
        assert conflict.value.code == "CONFLICT"
        await runner.authorize("workspace1", "attempt2", 2)
        with pytest.raises(RunnerError) as stale:
            await runner.execute(command.model_copy(update={"command_id": "stale"}))
        assert stale.value.code == "STALE_GENERATION"
        assert (backend.calls if implementation == "runner-http" else runner.executions) == 1
        with pytest.raises(RunnerError) as absent:
            await runner.status("not-admitted")
        assert absent.value.code == "NOT_FOUND"
        assert not client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("implementation", ["timeweb-http", "fixture"])
async def test_vm_allocation_identity_readback_and_confirmed_delete(tmp_path, implementation):
    settings = gateway_settings(tmp_path)
    cloud = Cloud()
    app = gateway_app(settings, cloud=cloud, probe=Probe(), network_policy=NetworkPolicy())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        provider = (
            HTTPVMProvider(tls_config(settings.bearer_secret), client)
            if implementation == "timeweb-http"
            else FixtureVMs()
        )
        allocated = await provider.create("allocation1", "baseline")
        assert allocated.vm_id == "allocation1" and allocated.state == "READY"
        assert await provider.create("allocation1", "baseline") == allocated
        assert await provider.status(allocated.vm_id) == allocated
        with pytest.raises(RunnerError) as conflict:
            await provider.create("allocation1", "different")
        assert conflict.value.code == "CONFLICT"
        assert (await provider.destroy(allocated.vm_id)).state == "DESTROYED"
        assert (await provider.status(allocated.vm_id)).state == "DESTROYED"
        assert (await provider.destroy(allocated.vm_id)).state == "DESTROYED"
        assert (await provider.create("allocation1", "baseline")).state == "DESTROYED"
        assert (
            len(cloud.posts) if implementation == "timeweb-http" else len(provider.purposes)
        ) == 1
        assert not client.is_closed


class ObjectError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class S3Client:
    """Conditional object API, no network/S3 qualification."""

    def __init__(self):
        self.objects = {}
        self.closed = False

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        if key in self.objects:
            raise ObjectError("PreconditionFailed")
        assert kwargs["IfNoneMatch"] == "*"
        self.objects[key] = (kwargs["Body"], kwargs["Metadata"])

    def get_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise ObjectError("NoSuchKey")
        data, metadata = self.objects[kwargs["Key"]]
        return {"Body": io.BytesIO(data), "Metadata": metadata}

    def head_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise ObjectError("NoSuchKey")
        return {}


@pytest.mark.parametrize("implementation", ["local", "s3"])
def test_store_immutable_identity_readback_and_safe_errors(tmp_path, implementation):
    client = S3Client()
    store = (
        LocalArtifactStore(tmp_path)
        if implementation == "local"
        else S3ArtifactStore(client, "private")
    )
    assert not store.exists("objects/one")
    ref = store.put_bytes("objects/one", b"one")
    assert ref.sha256 == hashlib.sha256(b"one").hexdigest() and ref.size == 3
    assert store.exists(ref.key) and store.get_bytes(ref.key) == b"one"
    assert store.put_bytes(ref.key, b"one") == ref
    with pytest.raises(StorageError):
        store.put_bytes(ref.key, b"different")
    assert store.get_bytes(ref.key) == b"one"
    with pytest.raises(StorageError, match="INVALID_OBJECT_KEY"):
        store.put_bytes("../outside", b"x")
    with pytest.raises(StorageError, match="OBJECT_READ_FAILED"):
        store.get_bytes("missing")
    assert not client.closed
