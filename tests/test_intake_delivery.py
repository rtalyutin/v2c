import hashlib
import io
import json
import re
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from v2c.delivery import DeliveryError, SecretScanError, package_delivery
from v2c.intake import (
    HTTPSGitFetcher,
    ImportLimits,
    IntakeError,
    RepositoryInput,
    Snapshot,
    git_command,
    import_repositories,
    inspect_snapshot,
    restore_snapshot_archive,
    safe_import_archive,
)
from v2c.storage import LocalArtifactStore, S3ArtifactStore, StorageError


def make_zip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def import_sample(tmp_path, files=None, target="snapshot"):
    archive = make_zip(
        tmp_path / (target + ".zip"),
        files
        or {
            "app.py": "def answer():\n    return 42\n",
            "tests/test_app.py": "def test_answer(): assert 42 == 42\n",
            ".gitignore": "generated.txt\n",
            "generated.txt": "must be in original Git too\n",
        },
    )
    return safe_import_archive(archive, tmp_path / target, project_id="project-1")


def test_archive_creates_git_and_verified_snapshot(tmp_path):
    snapshot = import_sample(tmp_path)
    root = snapshot.root / snapshot.repositories[0].path
    assert git_command(root, "show", "HEAD:generated.txt") == "must be in original Git too"
    assert snapshot.repositories[0].source_sha == git_command(root, "rev-parse", "HEAD")
    assert Snapshot.load(snapshot.root).snapshot_id == snapshot.snapshot_id
    assert inspect_snapshot(snapshot)["execution"] == "NOT_EXECUTED"
    (root / "app.py").write_text("changed")
    with pytest.raises(IntakeError, match="SNAPSHOT_INTEGRITY_MISMATCH"):
        Snapshot.load(snapshot.root)


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        "/escape.py",
        "x/../../escape",
        "C:/windows",
        "a\\evil",
        ".git/config",
        "aux.txt",
        "x/.. /a",
        "foo/./bar",
    ],
)
def test_bad_archive_paths_leave_no_partial_snapshot(tmp_path, path):
    archive = make_zip(tmp_path / "bad.zip", {path: "x"})
    with pytest.raises(IntakeError):
        safe_import_archive(archive, tmp_path / "snapshot", project_id="p")
    assert not (tmp_path / "snapshot").exists()
    assert not list(tmp_path.glob(".intake-*"))


def test_zip_symlink_casecollision_bomb_and_binary_rejected(tmp_path):
    path = tmp_path / "bad.zip"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, "../outside")
    with pytest.raises(IntakeError, match="UNSUPPORTED_LINK"):
        safe_import_archive(path, tmp_path / "out", project_id="p")
    for files, error in [
        ({"APP.py": "a", "app.py": "b"}, "DUPLICATE_PATH"),
        ({"huge.txt": "A" * 100000}, "COMPRESSION_LIMIT"),
        ({"binary": b"\x7fELF1234"}, "NATIVE_BINARY"),
    ]:
        make_zip(path, files)
        with pytest.raises(IntakeError, match=error):
            safe_import_archive(path, tmp_path / "out", project_id="p")


def test_tar_hardlink_and_actual_unpacked_limit(tmp_path):
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        item = tarfile.TarInfo("alias")
        item.type, item.linkname = tarfile.LNKTYPE, "/etc/passwd"
        archive.addfile(item)
    with pytest.raises(IntakeError, match="UNSUPPORTED_LINK"):
        safe_import_archive(path, tmp_path / "out", project_id="p")
    with tarfile.open(path, "w") as archive:
        for name in ("a.txt", "b.txt"):
            data = b"0123456789"
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    with pytest.raises(IntakeError, match="SOURCE_LIMIT"):
        safe_import_archive(
            path, tmp_path / "out", project_id="p", limits=ImportLimits(max_total_bytes=15)
        )


def test_storage_is_immutable_and_checks_s3_readback(tmp_path):
    store = LocalArtifactStore(tmp_path / "objects")
    ref = store.put_bytes("p/a", b"one")
    assert ref.sha256 == hashlib.sha256(b"one").hexdigest()
    assert store.put_bytes("p/a", b"one") == ref
    with pytest.raises(StorageError, match="IMMUTABLE"):
        store.put_bytes("p/a", b"two")
    with pytest.raises(StorageError, match="INVALID_OBJECT_KEY"):
        store.put_bytes("../escape", b"one")

    class FakeS3:
        def put_object(self, **kwargs):
            self.put = kwargs

        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(b"corrupt")}

    fake = FakeS3()
    with pytest.raises(StorageError, match="CHECKSUM"):
        S3ArtifactStore(fake, "private").put_bytes("p/a", b"one")
    assert fake.put["IfNoneMatch"] == "*"
    assert "ACL" not in fake.put  # bucket policy owns privacy; no public ACL


def test_delivery_git_bundle_skills_links_and_candidate_only(tmp_path):
    original = import_sample(tmp_path)
    checkpoint = make_zip(
        tmp_path / "checkpoint.zip",
        {
            "repositories/main/app.py": "def answer():\n    return 43\n",
            "repositories/main/tests/test_app.py": "def test_answer(): assert 43 == 43\n",
        },
    )
    final = restore_snapshot_archive(checkpoint, tmp_path / "final", original_snapshot=original)
    bundle = package_delivery(
        final,
        tmp_path / "delivery.zip",
        original_snapshot=original,
        project_manifest={
            "commands": {"test": [{"argv": ["python", "-m", "pytest"], "evidence_id": "ev-1"}]},
            "examples": [{"file": "main/app.py", "description": "Application function"}],
            "environment_recipe": {"python": "3.12"},
        },
        verification={"status": "PARTIAL", "hidden_answers": "DO NOT COPY"},
        store=LocalArtifactStore(tmp_path / "objects"),
    )
    assert bundle.status == "CANDIDATE" and bundle.artifact
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(bundle.path) as archive:
        archive.extractall(extracted)
    assert not any(".git/" in p["path"] for p in bundle.manifest["files"])
    assert "hidden_answers" not in (extracted / "docs/verification.json").read_text()
    assert len(list((extracted / "project-skills").glob("*/SKILL.md"))) == 6
    for skill in (extracted / "project-skills").glob("*/SKILL.md"):
        for link in re.findall(r"\]\(([^)]+)\)", skill.read_text()):
            assert (skill.parent / link).is_file(), link
    git_bundle = extracted / "git/main.bundle"
    history = tmp_path / "restored-git"
    history.mkdir()
    git_command(history, "init", "--template=")
    git_command(history, "bundle", "verify", str(git_bundle))
    refs = git_command(history, "bundle", "list-heads", str(git_bundle))
    assert original.repositories[0].source_sha + " refs/v2c/source" in refs
    assert "refs/v2c/final" in refs
    assert (original.root / "repositories/main/app.py").read_text().endswith("return 42\n")


def test_secrets_deleted_from_candidate_still_block_git_export(tmp_path):
    original = import_sample(
        tmp_path, {"app.py": "print('hello')", "secret.txt": "API_KEY = 'abcdefghijklmnop12345'"}
    )
    checkpoint = make_zip(
        tmp_path / "checkpoint.zip", {"repositories/main/app.py": "print('hello')"}
    )
    final = restore_snapshot_archive(checkpoint, tmp_path / "final", original_snapshot=original)
    with pytest.raises(SecretScanError, match="EXPORT_SECRET_DETECTED") as error:
        package_delivery(
            final,
            tmp_path / "out.zip",
            original_snapshot=original,
            project_manifest={},
            verification={},
        )
    assert not (tmp_path / "out.zip").exists()
    assert "abcdefghijklmnop12345" not in str(error.value)


def test_multirepo_pins_and_checkpoint_layout(tmp_path):
    first = import_sample(tmp_path, {"app.py": "print('a')"}, target="first")
    second = import_sample(tmp_path, {"api.py": "print('b')"}, target="second")
    copies = {"web": first.root / "repositories/main", "api": second.root / "repositories/main"}

    class LocalTestFetcher:
        def fetch(self, source, destination, limits):
            import shutil

            shutil.copytree(copies[source.name], destination)

    specs = [
        RepositoryInput(
            name, "https://example.invalid/" + name, git_command(root, "rev-parse", "HEAD")
        )
        for name, root in copies.items()
    ]
    snapshot = import_repositories(
        specs, tmp_path / "multi", project_id="p", fetcher=LocalTestFetcher()
    )
    assert {r.name for r in snapshot.repositories} == {"web", "api"}
    bad = make_zip(tmp_path / "checkpoint.zip", {"repositories/unknown/app.py": "no"})
    with pytest.raises(IntakeError, match="CHECKPOINT_REPOSITORY"):
        restore_snapshot_archive(bad, tmp_path / "restored", original_snapshot=snapshot)


def test_remote_import_refuses_untrusted_urls_before_network(tmp_path):
    fetcher = HTTPSGitFetcher(("github.com",))
    for url in [
        "file:///etc",
        "ssh://github.com/r/r",
        "https://u:secret@github.com/r/r",
        "https://127.0.0.1/r",
        "https://github.com/r?token=secret",
    ]:
        with pytest.raises(IntakeError, match="URL_NOT_ALLOWED"):
            fetcher.fetch(RepositoryInput("r", url, "1" * 40), tmp_path / "out", ImportLimits())


def test_code_only_snapshot_archive_roundtrip(tmp_path):
    from v2c.intake import snapshot_archive_bytes

    source = import_sample(tmp_path)
    body = snapshot_archive_bytes(source)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert all(n.startswith("repositories/main/") for n in archive.namelist())
        assert not any("/.git/" in n for n in archive.namelist())
        assert "snapshot.json" not in archive.namelist()
    restored = restore_snapshot_archive(body, tmp_path / "restored", original_snapshot=source)
    assert source.repositories[0].files == restored.repositories[0].files
    assert source.repositories[0].source_sha == restored.repositories[0].source_sha


def test_restore_source_after_control_workspace_loss(tmp_path):
    import shutil
    from v2c.intake import source_git_bundles, snapshot_archive_bytes, restore_source_snapshot

    source = import_sample(tmp_path)
    code, bundles = snapshot_archive_bytes(source), source_git_bundles(source)
    manifest = {
        **source.manifest,
        "archive_sha256": hashlib.sha256(code).hexdigest(),
        "source_git_bundles": {
            name: {"key": name, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
            for name, body in bundles.items()
        },
    }
    shutil.rmtree(source.root)
    restored = restore_source_snapshot(manifest, code, bundles, tmp_path / "recovered")
    assert restored.snapshot_id == source.snapshot_id
    assert restored.repositories == source.repositories
    assert Snapshot.load(restored.root).repositories == source.repositories
    with pytest.raises(IntakeError, match="SOURCE_BUNDLE_CHECKSUM"):
        restore_source_snapshot(manifest, code, {"main": b"broken"}, tmp_path / "invalid")


def test_delivery_recipe_executes_from_packaged_cwd_and_maps_source_paths(tmp_path):
    import sys

    source = import_sample(tmp_path, {"src/check.py": "print('portable-owner-check')\n"})
    recipe = {
        "runtime": "Python",
        "setup": [],
        "restore": [],
        "recovery_checks": [],
        "tests": [
            {
                "id": "check",
                "argv": [sys.executable, "check.py"],
                "cwd": "repositories/main/src",
                "timeout_seconds": 5,
            }
        ],
    }
    passport = {
        "environment_recipe": recipe,
        "commands": {
            "test": [
                {
                    "argv": [sys.executable, "check.py"],
                    "cwd": "repositories/main/src",
                    "evidence_id": "check",
                }
            ],
            "root-test": [{"argv": [sys.executable, "repositories/main/src/check.py"], "cwd": "."}],
        },
        "map": {
            "repositories": [{"name": "main", "path": "repositories/main"}],
            "entrypoint": "repositories/main/src/check.py",
        },
    }
    delivery = package_delivery(
        source, tmp_path / "portable.zip", project_manifest=passport, verification={}
    )
    output = tmp_path / "portable"
    with zipfile.ZipFile(delivery.path) as archive:
        archive.extractall(output)
    environment = json.loads((output / "docs/environment.json").read_text())
    system_map = json.loads((output / "docs/system-map.json").read_text())
    assert environment["path_base"] == "delivery_root"
    assert system_map["repositories"][0]["path"] == "code/main"
    assert (output / system_map["entrypoint"]).is_file()
    assert environment["recipe"]["tests"][0]["cwd"] == "code/main/src"
    # Exercise the exported argv/cwd with known authored fixture code; no model or
    # uploaded code is executed by this platform test.
    for command in [environment["recipe"]["tests"][0], environment["commands"]["root-test"][0]]:
        result = subprocess.run(
            command["argv"],
            cwd=output / command["cwd"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=5,
        )
        assert result.stdout == "portable-owner-check\n"
    skills = (output / "project-skills/verify/SKILL.md").read_text()
    assert "cd code/main/src && " in skills
    assert "из корня распакованной поставки" in skills
    assert "repositories/" not in skills
    # Caller-owned runner recipe remains valid for its original environment.
    assert recipe["tests"][0]["cwd"] == "repositories/main/src"


def test_delivery_rejects_unknown_or_escaping_command_cwd(tmp_path):
    source = import_sample(tmp_path)
    for number, cwd in enumerate(
        ("repositories/unknown", "repositories/main/../../outside", "repositories/main/missing")
    ):
        with pytest.raises((DeliveryError, IntakeError)):
            package_delivery(
                source,
                tmp_path / f"bad-{number}.zip",
                project_manifest={
                    "commands": {"test": [{"argv": ["python", "app.py"], "cwd": cwd}]}
                },
                verification={},
            )
        assert not (tmp_path / f"bad-{number}.zip").exists()


def test_executable_mode_survives_import_runner_recovery_and_delivery(tmp_path):
    from v2c.intake import snapshot_archive_bytes, source_git_bundles, restore_source_snapshot

    archive = tmp_path / "script.zip"
    info = zipfile.ZipInfo("scripts/check.sh")
    info.create_system = 3
    # Privilege bits in an untrusted archive must be discarded, while the
    # ordinary executable bit is behavior that must survive handover.
    info.external_attr = (stat.S_IFREG | stat.S_ISUID | stat.S_ISGID | 0o755) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(info, "#!/bin/sh\nprintf 'executable-owner-check\\n'\n")
    source = safe_import_archive(archive, tmp_path / "source-exec", project_id="exec-project")
    file = source.repositories[0].files[0]
    assert file["executable"] is True
    root = source.root / "repositories/main"
    assert (root / "scripts/check.sh").stat().st_mode & 0o111
    assert not ((root / "scripts/check.sh").stat().st_mode & (stat.S_ISUID | stat.S_ISGID))
    assert git_command(root, "ls-tree", "HEAD", "scripts/check.sh").startswith("100755 ")
    body = snapshot_archive_bytes(source)
    with zipfile.ZipFile(io.BytesIO(body)) as output:
        assert output.getinfo("repositories/main/scripts/check.sh").external_attr >> 16 == 0o100755
    restored = restore_snapshot_archive(body, tmp_path / "restored-exec", original_snapshot=source)
    assert restored.repositories == source.repositories
    recovered = restore_source_snapshot(
        source.manifest, body, source_git_bundles(source), tmp_path / "recovered-exec"
    )
    assert recovered.repositories == source.repositories
    delivery = package_delivery(
        restored,
        tmp_path / "exec.zip",
        original_snapshot=source,
        project_manifest={
            "commands": {"test": [{"argv": ["./scripts/check.sh"], "cwd": "repositories/main"}]}
        },
        verification={},
    )
    extracted = tmp_path / "exec-delivery"
    with zipfile.ZipFile(delivery.path) as output:
        name = "code/main/scripts/check.sh"
        assert output.getinfo(name).external_attr >> 16 & 0o111
        output.extractall(extracted)
        # stdlib ZipFile deliberately ignores executable permissions on extract;
        # emulate an owner extractor that honors the supplied Unix permissions.
        script = extracted / name
        script.chmod((output.getinfo(name).external_attr >> 16) & 0o777)
    environment = json.loads((extracted / "docs/environment.json").read_text())
    command = environment["commands"]["test"][0]
    result = subprocess.run(
        command["argv"],
        cwd=extracted / command["cwd"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        check=True,
    )
    assert result.stdout == "executable-owner-check\n"
    # Metadata-only mutation is an integrity change, even with identical bytes.
    (root / "scripts/check.sh").chmod(0o600)
    with pytest.raises(IntakeError, match="SNAPSHOT_INTEGRITY"):
        Snapshot.load(source.root)
