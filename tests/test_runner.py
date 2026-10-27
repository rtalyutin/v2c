import base64
import hashlib
import io
import json
import sqlite3
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

from v2c.adapters.runner import (
    AuthorizationRequest,
    CommandRequest,
    CommandResult,
    PrepareRequest,
    RunnerError,
)
from v2c.runner_service import DockerBackend, Journal, RunnerService, RunnerSettings, create_app


class ControlledBackend:
    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.stopped = False
        self.observed = None

    def run(self, request, workspace):
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        return CommandResult(
            command_id=request.command_id,
            request_hash=request.content_hash(),
            state="SUCCEEDED",
            exit_code=0,
            stop_confirmed=True,
            output="fixture result",
        )

    def readback(self, request):
        return self.observed

    def stop(self, request):
        self.stopped = True
        return True


def settings(tmp_path):
    return RunnerSettings(
        data_dir=tmp_path,
        bearer_secret="test-runner-secret-value",
        image="test/image@sha256:" + "a" * 64,
        cpu_limit=1,
        memory_bytes=100000000,
        pids_limit=32,
        tmpfs_bytes=1000000,
        workspace_disk_bytes=1000000,
        max_archive_bytes=100000,
        max_expanded_bytes=100000,
        max_files=100,
        max_command_seconds=30,
        max_output_bytes=10000,
        uid=65532,
        gid=65532,
    )


def archive(files=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, content in (files or {"src/app.py": 'print("hello")'}).items():
            z.writestr(name, content)
    return out.getvalue()


def prepare(data=None, **updates):
    data = data or archive()
    return PrepareRequest(
        workspace_id="run1",
        attempt_id="attempt1",
        generation=1,
        archive_b64=base64.b64encode(data).decode(),
        archive_sha256=hashlib.sha256(data).hexdigest(),
        input_manifest={"source": "test"},
        **updates,
    )


def command(**updates):
    data = {
        "command_id": "cmd1",
        "workspace_id": "run1",
        "attempt_id": "attempt1",
        "generation": 1,
        "argv": ["python3", "app.py"],
        "timeout_seconds": 10,
    }
    data.update(updates)
    return CommandRequest(**data)


def service(tmp_path):
    backend = ControlledBackend()
    runner = RunnerService(settings(tmp_path), backend)
    runner.prepare(prepare())
    return runner, backend


def test_acceptance_duplicate_conflict_and_readback_do_not_rerun(tmp_path):
    runner, backend = service(tmp_path)
    first = runner.execute(command())
    assert first.state == "ACCEPTED"
    assert backend.started.wait(1)
    second = runner.execute(command())
    assert second.request_hash == first.request_hash
    with pytest.raises(RunnerError) as failure:
        runner.execute(command(argv=["other"]))
    assert failure.value.code == "CONFLICT"
    assert runner.status("cmd1").state == "RUNNING"
    assert backend.calls == 1
    backend.release.set()
    runner.threads["cmd1"].join(2)
    assert runner.status("cmd1").state == "SUCCEEDED"
    assert runner.execute(command()).state == "SUCCEEDED" and backend.calls == 1


def test_stale_generation_cannot_start_new_work_or_reuse_new_id(tmp_path):
    runner, backend = service(tmp_path)
    runner.authorize(AuthorizationRequest(workspace_id="run1", attempt_id="attempt2", generation=2))
    with pytest.raises(RunnerError) as failure:
        runner.execute(command())
    assert failure.value.code == "STALE_GENERATION" and backend.calls == 0
    with pytest.raises(RunnerError):
        runner.authorize(
            AuthorizationRequest(workspace_id="run1", attempt_id="attempt1", generation=1)
        )
    assert backend.calls == 0


def test_restart_after_admission_before_effect_never_replays(tmp_path):
    runner, backend = service(tmp_path)
    accepted, new = runner.journal.admit(command())
    assert new and accepted.state == "ACCEPTED"
    after = RunnerService(settings(tmp_path), ControlledBackend())
    assert after.status("cmd1").state == "UNKNOWN"
    assert after.execute(command()).state == "UNKNOWN"
    assert after.backend.calls == 0
    with pytest.raises(RunnerError):
        after.execute(command(command_id="cmd2"))
    assert after.backend.calls == 0


def test_restart_recovers_remote_running_then_terminal_without_reexecution(tmp_path):
    runner, backend = service(tmp_path)
    runner.journal.admit(command())
    after = RunnerService(settings(tmp_path), ControlledBackend())
    after.backend.observed = CommandResult(
        command_id="cmd1", request_hash=command().content_hash(), state="RUNNING"
    )
    assert after.status("cmd1").state == "RUNNING"
    after.backend.observed = CommandResult(
        command_id="cmd1",
        request_hash=command().content_hash(),
        state="SUCCEEDED",
        exit_code=0,
        stop_confirmed=True,
    )
    assert after.status("cmd1").state == "SUCCEEDED"
    assert after.backend.calls == 0


def test_cancel_waits_for_stop_confirmation_and_late_success_cannot_overwrite(tmp_path):
    runner, backend = service(tmp_path)
    runner.execute(command())
    assert backend.started.wait(1)
    result = runner.cancel("cmd1")
    assert result.state == "CANCELLED" and result.stop_confirmed and backend.stopped
    backend.release.set()
    runner.threads["cmd1"].join(2)
    assert runner.status("cmd1").state == "CANCELLED"


def test_cancel_unconfirmed_stays_unknown(tmp_path):
    runner, backend = service(tmp_path)
    backend.stop = lambda request: False
    runner.execute(command())
    assert backend.started.wait(1)
    result = runner.cancel("cmd1")
    assert result.state == "UNKNOWN" and not result.stop_confirmed
    with pytest.raises(RunnerError):
        runner.checkpoint("run1")
    backend.release.set()
    runner.threads["cmd1"].join(2)


def test_safe_archive_checkpoint_and_auth(tmp_path):
    config = settings(tmp_path)
    backend = ControlledBackend()
    with TestClient(create_app(config, backend)) as client:
        assert client.post("/v1/workspaces", json=prepare().model_dump()).status_code == 401
        headers = {"Authorization": "Bearer " + config.bearer_secret}
        assert (
            client.post("/v1/workspaces", json=prepare().model_dump(), headers=headers).status_code
            == 200
        )
        checkpoint = client.get("/v1/workspaces/run1/checkpoint", headers=headers)
        assert checkpoint.status_code == 200
        with zipfile.ZipFile(io.BytesIO(checkpoint.content)) as z:
            assert z.read("src/app.py") == b'print("hello")'
        response = client.delete("/v1/workspaces/run1", headers=headers)
        assert response.json()["vm_destroyed"] is False


@pytest.mark.parametrize("name", ["../outside", "/absolute", "nested/../../outside", "a\\b"])
def test_reject_unsafe_archive_paths(tmp_path, name):
    runner = RunnerService(settings(tmp_path), ControlledBackend())
    with pytest.raises(RunnerError):
        runner.prepare(prepare(archive({name: "bad"})))
    assert not (tmp_path.parent / "outside").exists()


def test_reject_symlinks_and_changed_snapshot(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        entry = zipfile.ZipInfo("link")
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        z.writestr(entry, "/etc/passwd")
    runner = RunnerService(settings(tmp_path), ControlledBackend())
    with pytest.raises(RunnerError):
        runner.prepare(prepare(stream.getvalue()))
    runner2, backend = service(tmp_path / "other")
    with pytest.raises(RunnerError):
        runner2.prepare(prepare(archive({"other": "contents"})))


def test_docker_command_has_enforced_boundaries(tmp_path):
    config = settings(tmp_path)
    # Only inspect argv. No Docker daemon or local project code is invoked.
    backend = object.__new__(DockerBackend)
    backend.settings = config
    args = backend.argv(
        command(argv=["python3", "-c", "print(1)"]), tmp_path / "workspaces" / "run1"
    )
    assert "--network=none" in args and "--read-only" in args and "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges" in args
    assert "--pull=never" in args and "--entrypoint" in args
    assert "--privileged" not in args
    assert not any("docker.sock" in arg for arg in args)
    assert args[-3:] == [config.image, "-c", "print(1)"]


class ServiceBackend(ControlledBackend):
    def __init__(self):
        super().__init__()
        self.prepared = []
        self.destroyed = []
        self.owner = None

    def prepare_runtime(self, runtime):
        # The service/credential/network intent must already be durable before
        # the first external effect is requested.
        with self.owner.journal.connect() as db:
            saved = db.execute(
                "SELECT definition_json,state FROM runtime WHERE workspace_id=?",
                (runtime["workspace_id"],),
            ).fetchone()
        assert saved["state"] == "PREPARING"
        assert json.loads(saved["definition_json"]) == runtime
        self.prepared.append(runtime)

    def destroy_runtime(self, runtime, commands):
        self.destroyed.append((runtime, commands))


def postgres_settings(tmp_path):
    return settings(tmp_path).model_copy(
        update={
            "postgres_image": "operator/postgres@sha256:" + "b" * 64,
            "postgres_data_bytes": 1024 * 1024,
            "postgres_ready_seconds": 2,
            "postgres_uid": 999,
            "postgres_gid": 999,
        }
    )


def postgres_manifest(config):
    return {
        "environment_recipe": {
            "services": [
                {
                    "name": "testdb",
                    "kind": "postgres",
                    "database": "synthetic",
                    "user": "testowner",
                    "env_var": "DATABASE_URL",
                }
            ],
            "state_strategy": "rebuild_from_fixtures",
            "restore": [{"id": "fixtures"}],
            "recovery_checks": [{"id": "schema"}],
        },
        "execution_limits": {
            "image": config.image,
            "cpus": 0.5,
            "memory_mb": 32,
            "pids": 16,
            "disk_mb": 4,
            "max_output_bytes": 1024,
            "network": "none",
        },
    }


def test_postgres_lifecycle_intent_dedup_credentials_and_resource_bounds(tmp_path):
    config = postgres_settings(tmp_path)
    backend = ServiceBackend()
    runner = RunnerService(config, backend)
    backend.owner = runner
    req = prepare().model_copy(update={"input_manifest": postgres_manifest(config)})
    assert runner.prepare(req)["state"] == "READY"
    assert runner.prepare(req)["state"] == "READY"
    assert len(backend.prepared) == 1
    runtime = backend.prepared[0]
    assert runtime["network"].startswith("v2c-net-")
    assert runtime["environment"]["DATABASE_URL"].startswith("postgresql://testowner:")
    assert runtime["password"] not in runner.prepare(req).__str__()
    assert runtime["limits"]["cpus"] + runtime["service_limits"]["cpus"] == 0.5
    assert (
        runtime["limits"]["memory_bytes"] + runtime["service_limits"]["memory_bytes"]
        == 32 * 1024 * 1024
    )
    assert runtime["limits"]["pids"] + runtime["service_limits"]["pids"] == 16
    assert runtime["limits"]["max_output_bytes"] == 1024
    runner.destroy_workspace("run1")
    runner.destroy_workspace("run1")
    assert len(backend.destroyed) == 1


def test_postgres_profile_is_explicit_and_rejects_other_services(tmp_path):
    backend = ServiceBackend()
    runner = RunnerService(settings(tmp_path), backend)
    backend.owner = runner
    manifest = postgres_manifest(settings(tmp_path))
    with pytest.raises(RunnerError) as failure:
        runner.prepare(prepare().model_copy(update={"input_manifest": manifest}))
    assert failure.value.code == "UNSUPPORTED" and not backend.prepared
    manifest["environment_recipe"]["services"][0]["kind"] = "redis"
    with pytest.raises(RunnerError) as failure:
        runner.prepare(prepare().model_copy(update={"input_manifest": manifest}))
    assert failure.value.code == "UNSUPPORTED"
    assert not backend.prepared


@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "untrusted/image"),
        ("network", "bridge"),
        ("disk_mb", 0.01),
        ("cpus", float("inf")),
    ],
)
def test_requested_execution_profile_is_enforced_before_effect(tmp_path, field, value):
    config = postgres_settings(tmp_path)
    backend = ServiceBackend()
    runner = RunnerService(config, backend)
    backend.owner = runner
    manifest = postgres_manifest(config)
    manifest["execution_limits"][field] = value
    with pytest.raises(RunnerError):
        runner.prepare(prepare().model_copy(update={"input_manifest": manifest}))
    assert not backend.prepared
    with runner.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM workspace").fetchone()[0] == 0


def test_postgres_docker_calls_internal_only_bounded_and_generated_env(tmp_path):
    config = postgres_settings(tmp_path)
    backend = ServiceBackend()
    service = RunnerService(config, backend)
    backend.owner = service
    service.prepare(prepare().model_copy(update={"input_manifest": postgres_manifest(config)}))
    runtime = backend.prepared[0]
    docker = object.__new__(DockerBackend)
    docker.settings = config
    docker.journal = service.journal
    calls = []

    def record(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": b"fixture"})()

    docker._docker = record
    docker.prepare_runtime(runtime)
    network = calls[0][0]
    assert network[:3] == ["network", "create", "--internal"]
    db_args, db_kwargs = calls[1]
    assert "--network=" + runtime["network"] in db_args
    assert "--read-only" in db_args and "--cap-drop=ALL" in db_args
    assert "--user" in db_args and "999:999" in db_args
    assert "--publish" not in db_args and "-p" not in db_args
    assert not any("docker.sock" in value for value in db_args)
    assert config.postgres_image == db_args[-1]
    assert any("/pgdata:rw,nosuid,nodev,noexec,size=1048576" in value for value in db_args)
    assert runtime["password"] not in " ".join(db_args)
    assert db_kwargs["environment"]["POSTGRES_PASSWORD"] == runtime["password"]
    assert calls[-1][0][0:3] == ["exec", runtime["container"], "pg_isready"]
    project_args = docker.argv(command(), tmp_path / "workspaces" / "run1")
    assert "--network=" + runtime["network"] in project_args
    assert "DATABASE_URL" in project_args
    assert runtime["password"] not in " ".join(project_args)


def test_settings_from_env_and_pinned_pg_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("V2C_RUNNER_SETTINGS_FILE", str(tmp_path / "settings.json"))
    config = postgres_settings(tmp_path)
    (tmp_path / "settings.json").write_text(config.model_dump_json())
    assert RunnerSettings.from_env().postgres_image == config.postgres_image
    with pytest.raises(ValueError):
        RunnerSettings.model_validate({**config.model_dump(), "postgres_image": "postgres:latest"})
    with pytest.raises(ValueError):
        RunnerSettings.model_validate({**config.model_dump(), "postgres_data_bytes": None})


def test_archive_executable_boolean_roundtrip_strips_special_bits(tmp_path):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for path, mode in [("bin/start", 0o104755), ("src/code.py", 0o106644)]:
            info = zipfile.ZipInfo(path)
            info.create_system = 3
            info.external_attr = mode << 16
            z.writestr(info, "example")
    runner = RunnerService(settings(tmp_path), ControlledBackend())
    runner.prepare(prepare(out.getvalue()))
    assert (tmp_path / "workspaces/run1/bin/start").stat().st_mode & 0o7777 == 0o755
    assert (tmp_path / "workspaces/run1/src/code.py").stat().st_mode & 0o7777 == 0o644
    with zipfile.ZipFile(io.BytesIO(runner.checkpoint("run1"))) as z:
        assert z.getinfo("bin/start").external_attr >> 16 & 0o7777 == 0o755
        assert z.getinfo("src/code.py").external_attr >> 16 & 0o7777 == 0o644


def test_health_attests_computed_execution_profile_and_separate_allocation_identity(tmp_path):
    config = settings(tmp_path).model_copy(update={"allocation_id": "allocation1"})
    moved = config.model_copy(
        update={
            "data_dir": tmp_path / "elsewhere",
            "bearer_secret": "different-safe-test-secret",
            "docker_binary": "/another/docker",
            "allocation_id": "allocation2",
        }
    )
    assert config.profile_sha256() == moved.profile_sha256()
    assert (
        config.profile_sha256()
        != config.model_copy(update={"memory_bytes": config.memory_bytes + 1}).profile_sha256()
    )
    assert (
        config.profile_sha256()
        != config.model_copy(update={"image": "another@sha256:" + "c" * 64}).profile_sha256()
    )
    with TestClient(create_app(config, ControlledBackend())) as client:
        response = client.get(
            "/health", headers={"Authorization": "Bearer " + config.bearer_secret}
        )
        assert response.json()["allocation_id"] == "allocation1"
        assert response.json()["profile_sha256"] == config.profile_sha256()
        assert config.bearer_secret not in response.text and "data_dir" not in response.text
