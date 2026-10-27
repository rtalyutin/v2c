"""Bounded source intake; never installs dependencies or executes project code."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Protocol
from urllib.parse import urlsplit


class IntakeError(ValueError):
    """Stable codes, intentionally excluding source contents and credentials."""


@dataclass(frozen=True)
class ImportLimits:
    max_archive_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_file_bytes: int = 16 * 1024 * 1024
    max_files: int = 20000
    max_path_length: int = 240
    max_compression_ratio: int = 200
    git_timeout_seconds: int = 90


@dataclass(frozen=True)
class RepositorySnapshot:
    name: str
    path: str
    source_sha: str
    sha256: str
    files: tuple[dict, ...]
    source: str = "archive"


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    project_id: str
    root: Path
    repositories: tuple[RepositorySnapshot, ...]
    manifest: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "project_id": self.project_id,
            "root": str(self.root),
            "repositories": [asdict(r) for r in self.repositories],
            "manifest": self.manifest,
        }

    @classmethod
    def load(cls, root: Path | str) -> Snapshot:
        root = Path(root).resolve()
        value = json.loads((root / "snapshot.json").read_text())
        repos = tuple(
            RepositorySnapshot(**{**r, "files": tuple(r["files"])}) for r in value["repositories"]
        )
        snapshot = cls(value["snapshot_id"], value["project_id"], root, repos, value)
        inspect_snapshot(snapshot)  # changed bytes do not inherit old provenance
        return snapshot


@dataclass(frozen=True)
class RepositoryInput:
    name: str
    url: str
    revision: str
    secret_ref: str | None = None


class GitFetcher(Protocol):
    def fetch(self, source: RepositoryInput, destination: Path, limits: ImportLimits) -> None: ...


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def safe_relative_path(name: str, limits: ImportLimits | None = None) -> str:
    limits = limits or ImportLimits()
    if not isinstance(name, str) or "\\" in name or "\x00" in name or ":" in name:
        raise IntakeError("UNSAFE_PATH")
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    parts = name.split("/")
    if (
        not name
        or len(name) > limits.max_path_length
        or name.startswith("/")
        or any(p in {"", ".", ".."} for p in parts)
        or any(p.casefold() == ".git" for p in parts)
        or any(p.endswith((" ", ".")) for p in parts)
        or any(any(ord(c) < 32 or ord(c) == 127 for c in p) for p in parts)
        or any(re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", p, re.I) for p in parts)
    ):
        raise IntakeError("UNSAFE_PATH")
    return PurePosixPath(name).as_posix()


def _native_binary(data: bytes) -> bool:
    return data.startswith(
        (
            b"\x7fELF",
            b"MZ",
            b"\xfe\xed\xfa\xce",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe",
        )
    )


def git_environment() -> dict[str, str]:
    # Do not inherit Git config, proxies, askpass programs or infrastructure secrets.
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_AUTHOR_NAME": "v2c intake",
        "GIT_AUTHOR_EMAIL": "intake@v2c.local",
        "GIT_COMMITTER_NAME": "v2c intake",
        "GIT_COMMITTER_EMAIL": "intake@v2c.local",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
    }


_git_environment = git_environment  # Compatibility for existing internal consumers.


def git_command(root: Path, *args: str, timeout: int = 30, env: dict | None = None) -> str:
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "protocol.ext.allow=never",
        "-C",
        str(root),
        *args,
    ]
    try:
        result = subprocess.run(
            command,
            env=env or git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise IntakeError("GIT_OPERATION_FAILED") from None
    return result.stdout.decode("utf-8", errors="replace").strip()


def inventory_files(root: Path, limits: ImportLimits) -> tuple[dict, ...]:
    files, total = [], 0
    seen = set()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if ".git" in path.relative_to(root).parts:
            continue
        relative = safe_relative_path(relative, limits)
        identity = unicodedata.normalize("NFC", relative).casefold()
        if identity in seen:
            raise IntakeError("AMBIGUOUS_PATH")
        seen.add(identity)
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise IntakeError("UNSUPPORTED_LINK_OR_SPECIAL_FILE")
        if path.is_dir():
            continue
        size = path.stat().st_size
        total += size
        if (
            size > limits.max_file_bytes
            or total > limits.max_total_bytes
            or len(files) >= limits.max_files
        ):
            raise IntakeError("SOURCE_LIMIT_EXCEEDED")
        with path.open("rb") as source:
            if _native_binary(source.read(8)):
                raise IntakeError("NATIVE_BINARY_UNSUPPORTED")
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": size,
                "executable": bool(path.stat().st_mode & 0o111),
            }
        )
    if not files:
        raise IntakeError("EMPTY_PROJECT")
    if any(f["path"] == ".gitmodules" for f in files):
        raise IntakeError("SUBMODULES_REQUIRE_EXPLICIT_REPOSITORY_INPUTS")
    return tuple(files)


_inventory = inventory_files  # Compatibility for callers of the old private name.


def _repository(root: Path, name: str, source: str, limits: ImportLimits) -> RepositorySnapshot:
    files = inventory_files(root, limits)
    sha = git_command(root, "rev-parse", "HEAD")
    return RepositorySnapshot(
        name, f"repositories/{name}", sha, hashlib.sha256(_json(files)).hexdigest(), files, source
    )


def _finish_snapshot(
    workspace: Path,
    destination: Path,
    project_id: str,
    repositories: list[RepositorySnapshot],
    source: dict,
) -> Snapshot:
    identity = {
        "project_id": project_id,
        "repositories": [asdict(r) for r in repositories],
        "source": source,
    }
    snapshot_id = hashlib.sha256(_json(identity)).hexdigest()
    manifest = {
        "version": 1,
        **identity,
        "snapshot_id": snapshot_id,
        "workspace_root": str(destination.resolve()),
    }
    (workspace / "snapshot.json").write_bytes(_json(manifest))
    # Parent directory is trusted. Destination is a unique server-assigned path.
    if destination.exists():
        raise IntakeError("DESTINATION_ALREADY_EXISTS")
    workspace.rename(destination)
    return Snapshot(snapshot_id, project_id, destination.resolve(), tuple(repositories), manifest)


class _Extractor:
    def __init__(self, destination: Path, limits: ImportLimits):
        self.destination, self.limits, self.total, self.count = destination, limits, 0, 0
        self.seen: dict[str, bool] = {}

    def add(
        self,
        name: str,
        is_directory: bool,
        size: int,
        source: BinaryIO | None,
        *,
        executable: bool = False,
    ):
        path = safe_relative_path(name, self.limits)
        identity = unicodedata.normalize("NFC", path).casefold()
        if identity in self.seen:
            raise IntakeError("DUPLICATE_PATH")
        # Also reject parent-as-file before creating anything.
        for parent in PurePosixPath(identity).parents:
            if str(parent) in self.seen and not self.seen[str(parent)]:
                raise IntakeError("AMBIGUOUS_PATH")
        if not is_directory and any(p.startswith(identity + "/") for p in self.seen):
            raise IntakeError("AMBIGUOUS_PATH")
        self.seen[identity] = is_directory
        self.count += 1
        if self.count > self.limits.max_files:
            raise IntakeError("SOURCE_LIMIT_EXCEEDED")
        target = self.destination / path
        if is_directory:
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            return
        if size > self.limits.max_file_bytes or size < 0:
            raise IntakeError("SOURCE_LIMIT_EXCEEDED")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        actual = 0
        with target.open("xb") as output:
            while True:
                data = source.read(min(65536, self.limits.max_file_bytes - actual + 1))
                if not data:
                    break
                if actual == 0 and _native_binary(data):
                    raise IntakeError("NATIVE_BINARY_UNSUPPORTED")
                actual += len(data)
                self.total += len(data)
                if actual > self.limits.max_file_bytes or self.total > self.limits.max_total_bytes:
                    raise IntakeError("SOURCE_LIMIT_EXCEEDED")
                output.write(data)
        if actual != size:
            raise IntakeError("ARCHIVE_SIZE_MISMATCH")
        # Preserve only the Git-significant executable bit; never suid/sgid/sticky.
        target.chmod(0o700 if executable else 0o600)


def safe_import_archive(
    archive: Path | str,
    destination: Path | str,
    *,
    project_id: str,
    limits: ImportLimits | None = None,
) -> Snapshot:
    limits, archive, destination = limits or ImportLimits(), Path(archive), Path(destination)
    if archive.stat().st_size > limits.max_archive_bytes:
        raise IntakeError("ARCHIVE_LIMIT_EXCEEDED")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = Path(tempfile.mkdtemp(prefix=".intake-", dir=destination.parent))
    try:
        repo = workspace / "repositories" / "main"
        repo.mkdir(parents=True)
        extractor = _Extractor(repo, limits)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as source:
                if len(source.infolist()) > limits.max_files:
                    raise IntakeError("SOURCE_LIMIT_EXCEEDED")
                for item in source.infolist():
                    mode = item.external_attr >> 16
                    if (
                        item.flag_bits & 1
                        or stat.S_ISLNK(mode)
                        or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
                    ):
                        raise IntakeError("UNSUPPORTED_LINK_OR_ENCRYPTED_FILE")
                    if item.file_size > max(1, item.compress_size) * limits.max_compression_ratio:
                        raise IntakeError("ARCHIVE_COMPRESSION_LIMIT_EXCEEDED")
                    if item.is_dir():
                        extractor.add(item.orig_filename, True, 0, None)
                    else:
                        with source.open(item) as content:
                            extractor.add(
                                item.orig_filename,
                                False,
                                item.file_size,
                                content,
                                executable=bool(mode & 0o111),
                            )
        else:
            try:
                with tarfile.open(archive, "r|*") as source:
                    for item in source:
                        if item.name in {".", "./"} and item.isdir():
                            continue
                        if not (item.isfile() or item.isdir()):
                            raise IntakeError("UNSUPPORTED_LINK_OR_SPECIAL_FILE")
                        if item.isdir():
                            extractor.add(item.name, True, 0, None)
                        else:
                            with source.extractfile(item) as content:
                                extractor.add(
                                    item.name,
                                    False,
                                    item.size,
                                    content,
                                    executable=bool(item.mode & 0o111),
                                )
            except tarfile.TarError:
                raise IntakeError("UNSUPPORTED_ARCHIVE") from None
        git_command(repo, "init", "--initial-branch=main", "--template=")
        # No hooks, global filters, or filesystem .git input; literal pathspec handles hostile filenames.
        git_command(repo, "--literal-pathspecs", "add", "--force", "--all", "--", ".")
        git_command(repo, "-c", "commit.gpgsign=false", "commit", "-m", "Imported immutable source")
        repository = _repository(repo, "main", "archive", limits)
        return _finish_snapshot(
            workspace,
            destination,
            project_id,
            [repository],
            {"kind": "archive", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()},
        )
    except (zipfile.BadZipFile, OSError, EOFError):
        raise IntakeError("INVALID_ARCHIVE") from None
    finally:
        if workspace.exists():
            shutil.rmtree(workspace)


class HTTPSGitFetcher:
    """Fetch only a full pinned commit from a configured trusted HTTPS host.

    Host allowlist is an operator decision, not extracted from project instructions.
    DNS must resolve to public addresses. Redirects, hooks, recursive submodules,
    project filters and all other protocols are disabled. Secrets never enter URL.
    """

    def __init__(
        self, allowed_hosts: tuple[str, ...], secret_resolver: Callable[[str], str] | None = None
    ):
        self.allowed_hosts, self.secret_resolver = set(allowed_hosts), secret_resolver

    def fetch(self, source: RepositoryInput, destination: Path, limits: ImportLimits) -> None:
        parsed = urlsplit(source.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname not in self.allowed_hosts
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
            or not parsed.path
            or "\\" in source.url
            or any(ord(c) < 33 for c in source.url)
        ):
            raise IntakeError("REPOSITORY_URL_NOT_ALLOWED")
        if not re.fullmatch(r"[0-9a-f]{40}", source.revision):
            raise IntakeError("PINNED_SHA_REQUIRED")
        try:
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        except OSError:
            raise IntakeError("REPOSITORY_DNS_FAILED") from None
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise IntakeError("REPOSITORY_ADDRESS_NOT_ALLOWED")
        destination.mkdir(parents=True)
        git_command(destination, "init", "--initial-branch=main", "--template=")
        env = git_environment()
        if source.secret_ref:
            if self.secret_resolver is None:
                raise IntakeError("REPOSITORY_SECRET_UNAVAILABLE")
            token = self.secret_resolver(source.secret_ref)
            if not token or any(c in token for c in "\r\n\x00"):
                raise IntakeError("REPOSITORY_SECRET_INVALID")
            # Environment configuration is not persisted into .git/config or command arguments.
            import base64

            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": "Authorization: Basic "
                    + base64.b64encode(("x-access-token:" + token).encode()).decode(),
                }
            )
        command = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "http.followRedirects=false",
            "-c",
            "http.sslVerify=true",
            "-c",
            "credential.helper=",
            "-C",
            str(destination),
            "fetch",
            "--quiet",
            "--depth=1",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            source.url,
            source.revision,
        ]
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(
                    command, env=env, stdout=output, stderr=output, start_new_session=True
                )
                started = time.monotonic()
                while process.poll() is None:
                    used = sum(p.stat().st_size for p in destination.rglob("*") if p.is_file())
                    if (
                        used > limits.max_total_bytes
                        or time.monotonic() - started > limits.git_timeout_seconds
                    ):
                        import signal

                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        raise IntakeError("REPOSITORY_FETCH_LIMIT_EXCEEDED")
                    time.sleep(0.05)
                if process.returncode != 0:
                    raise IntakeError("REPOSITORY_FETCH_FAILED")
            except OSError:
                raise IntakeError("REPOSITORY_FETCH_FAILED") from None
        git_command(destination, "update-ref", "refs/heads/main", source.revision)
        git_command(destination, "checkout", "--quiet", "main")
        if git_command(destination, "rev-parse", "HEAD") != source.revision:
            raise IntakeError("REPOSITORY_REVISION_MISMATCH")


def import_repositories(
    repositories: list[RepositoryInput],
    destination: Path | str,
    *,
    project_id: str,
    fetcher: GitFetcher,
    limits: ImportLimits | None = None,
) -> Snapshot:
    limits, destination = limits or ImportLimits(), Path(destination)
    if not repositories or len(repositories) > 20:
        raise IntakeError("INVALID_REPOSITORY_COUNT")
    names = [r.name for r in repositories]
    if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", n) for n in names) or len(
        {n.casefold() for n in names}
    ) != len(names):
        raise IntakeError("INVALID_REPOSITORY_NAME")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = Path(tempfile.mkdtemp(prefix=".intake-", dir=destination.parent))
    try:
        imported = []
        total_bytes = 0
        for source in repositories:
            if not re.fullmatch(r"[0-9a-f]{40}", source.revision):
                raise IntakeError("PINNED_SHA_REQUIRED")
            root = workspace / "repositories" / source.name
            fetcher.fetch(source, root, limits)
            repository = _repository(root, source.name, source.url, limits)
            if repository.source_sha != source.revision:
                raise IntakeError("REPOSITORY_REVISION_MISMATCH")
            total_bytes += sum(f["size"] for f in repository.files)
            if total_bytes > limits.max_total_bytes:
                raise IntakeError("SOURCE_LIMIT_EXCEEDED")
            imported.append(repository)
        return _finish_snapshot(
            workspace, destination, project_id, imported, {"kind": "repositories"}
        )
    finally:
        if workspace.exists():
            shutil.rmtree(workspace)


def inspect_snapshot(snapshot: Snapshot | Path | str) -> dict:
    if not isinstance(snapshot, Snapshot):
        return Snapshot.load(Path(snapshot)).manifest
    identity = {
        "project_id": snapshot.project_id,
        "repositories": [asdict(r) for r in snapshot.repositories],
        "source": snapshot.manifest.get("source"),
    }
    if hashlib.sha256(_json(identity)).hexdigest() != snapshot.snapshot_id:
        raise IntakeError("SNAPSHOT_ID_MISMATCH")
    inspected = []
    for repository in snapshot.repositories:
        root = snapshot.root / repository.path
        if not root.resolve().is_relative_to(snapshot.root.resolve()):
            raise IntakeError("SNAPSHOT_PATH_MISMATCH")
        actual = _repository(root, repository.name, repository.source, ImportLimits())
        if actual.sha256 != repository.sha256 or actual.source_sha != repository.source_sha:
            raise IntakeError("SNAPSHOT_INTEGRITY_MISMATCH")
        markers = [
            f["path"]
            for f in actual.files
            if Path(f["path"]).name
            in {
                "pyproject.toml",
                "requirements.txt",
                "package.json",
                "Cargo.toml",
                "go.mod",
                "Dockerfile",
                "compose.yaml",
            }
        ]
        inspected.append({**asdict(actual), "dependency_markers": markers})
    return {
        **snapshot.manifest,
        "workspace_root": str(snapshot.root),
        "repositories": inspected,
        "execution": "NOT_EXECUTED",
        "commands": {},
        "unknowns": ["Build/test commands and runtime behavior require isolated reproduction."],
    }


def restore_snapshot_archive(
    archive: Path | bytes,
    destination: Path | str,
    *,
    original_snapshot: Snapshot,
    limits: ImportLimits | None = None,
) -> Snapshot:
    """Validate runner output and attach its bytes to the original internal history.

    Checkpoint archive layout is repositories/<known repository>/...; .git is
    never accepted from the runner. Test-service state is owned by runner recovery,
    not inferred from the source archive by this function.
    """
    limits, destination = limits or ImportLimits(), Path(destination)
    inspect_snapshot(original_snapshot)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=destination.parent) as temporary:
        scratch = Path(temporary)
        if isinstance(archive, bytes):
            archive_path = scratch / "checkpoint.archive"
            archive_path.write_bytes(archive)
        else:
            archive_path = Path(archive)
        imported = safe_import_archive(
            archive_path,
            scratch / "imported",
            project_id=original_snapshot.project_id,
            limits=limits,
        )
        extracted = imported.root / "repositories" / "main"
        if any(path.name not in {"repositories", ".git"} for path in extracted.iterdir()):
            raise IntakeError("CHECKPOINT_LAYOUT_MISMATCH")
        incoming = extracted / "repositories"
        names = {r.name for r in original_snapshot.repositories}
        if not incoming.is_dir() or {p.name for p in incoming.iterdir()} != names:
            raise IntakeError("CHECKPOINT_REPOSITORY_MISMATCH")
        workspace = scratch / "result"
        result_repos = []
        for previous in original_snapshot.repositories:
            source = incoming / previous.name
            if not source.is_dir():
                raise IntakeError("CHECKPOINT_REPOSITORY_MISMATCH")
            repo = workspace / previous.path
            shutil.copytree(source, repo)
            shutil.copytree(original_snapshot.root / previous.path / ".git", repo / ".git")
            git_command(repo, "--literal-pathspecs", "add", "--force", "--all", "--", ".")
            if git_command(repo, "status", "--porcelain"):
                git_command(
                    repo,
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    "Verified runner candidate bytes",
                )
            result_repos.append(_repository(repo, previous.name, previous.source, limits))
        return _finish_snapshot(
            workspace,
            destination,
            original_snapshot.project_id,
            result_repos,
            {"kind": "runner-checkpoint", "original_snapshot_id": original_snapshot.snapshot_id},
        )


def snapshot_archive_bytes(snapshot: Snapshot) -> bytes:
    """Verified code-only runner input; internal Git and control metadata stay outside VM."""
    import io

    inspect_snapshot(snapshot)
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for repository in snapshot.repositories:
            for entry in repository.files:
                content = (snapshot.root / repository.path / entry["path"]).read_bytes()
                name = f"repositories/{repository.name}/{entry['path']}"
                info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (
                    0o100755 if entry.get("executable", False) else 0o100644
                ) << 16
                archive.writestr(info, content)
    return body.getvalue()


def source_git_bundles(
    snapshot: Snapshot, *, limits: ImportLimits | None = None
) -> dict[str, bytes]:
    """Portable internal source history; upload every object before snapshot DB publication."""
    limits = limits or ImportLimits()
    inspect_snapshot(snapshot)
    output = {}
    total = 0
    with tempfile.TemporaryDirectory(prefix="v2c-bundles-") as temporary:
        for repository in snapshot.repositories:
            target = Path(temporary) / (repository.name + ".bundle")
            git_command(snapshot.root / repository.path, "bundle", "create", str(target), "HEAD")
            size = target.stat().st_size
            total += size
            if size > limits.max_total_bytes or total > limits.max_total_bytes:
                raise IntakeError("SOURCE_BUNDLE_LIMIT_EXCEEDED")
            output[repository.name] = target.read_bytes()
    return output


def restore_source_snapshot(
    manifest: dict,
    archive: bytes,
    bundles: dict[str, bytes],
    destination: Path | str,
    *,
    limits: ImportLimits | None = None,
) -> Snapshot:
    """Rebuild source workspace from checksum-verified objects after control-volume loss.

    Exact original source SHAs are retained. No filesystem paths or Git metadata
    supplied by a project are trusted. Caller must verify object refs against its
    database manifest; this function additionally verifies code, commits and IDs.
    """
    limits, destination = limits or ImportLimits(), Path(destination)
    repositories = tuple(
        RepositorySnapshot(**{**row, "files": tuple(row["files"])})
        for row in manifest["repositories"]
    )
    names = {repository.name for repository in repositories}
    if (
        set(bundles) != names
        or len(names) != len(repositories)
        or any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name) for name in names)
        or sum(len(body) for body in bundles.values()) > limits.max_total_bytes
    ):
        raise IntakeError("SOURCE_BUNDLE_MANIFEST_MISMATCH")
    identity = {
        "project_id": manifest["project_id"],
        "repositories": [asdict(r) for r in repositories],
        "source": manifest["source"],
    }
    if hashlib.sha256(_json(identity)).hexdigest() != manifest["snapshot_id"]:
        raise IntakeError("SNAPSHOT_ID_MISMATCH")
    if (
        manifest.get("archive_sha256")
        and hashlib.sha256(archive).hexdigest() != manifest["archive_sha256"]
    ):
        raise IntakeError("SOURCE_ARCHIVE_CHECKSUM_MISMATCH")
    references = manifest.get("source_git_bundles", {})
    for name, body in bundles.items():
        if name in references:
            ref = references[name]
            if ref.get("sha256") != hashlib.sha256(body).hexdigest() or ref.get("size") != len(
                body
            ):
                raise IntakeError("SOURCE_BUNDLE_CHECKSUM_MISMATCH")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        raise IntakeError("DESTINATION_ALREADY_EXISTS")
    with tempfile.TemporaryDirectory(
        prefix=".source-restore-", dir=destination.parent
    ) as temporary:
        temporary = Path(temporary)
        archive_path = temporary / "source.zip"
        archive_path.write_bytes(archive)
        imported = safe_import_archive(
            archive_path, temporary / "import", project_id=manifest["project_id"], limits=limits
        )
        extracted = imported.root / "repositories/main"
        if any(path.name not in {"repositories", ".git"} for path in extracted.iterdir()):
            raise IntakeError("CHECKPOINT_LAYOUT_MISMATCH")
        incoming = extracted / "repositories"
        if not incoming.is_dir() or {p.name for p in incoming.iterdir()} != names:
            raise IntakeError("CHECKPOINT_REPOSITORY_MISMATCH")
        workspace = temporary / "workspace"
        for repository in repositories:
            if repository.path != f"repositories/{repository.name}" or not re.fullmatch(
                r"[0-9a-f]{40}", repository.source_sha
            ):
                raise IntakeError("SOURCE_BUNDLE_MANIFEST_MISMATCH")
            root = workspace / repository.path
            shutil.copytree(incoming / repository.name, root)
            git_command(root, "init", "--initial-branch=main", "--template=")
            bundle = temporary / f"{repository.name}.bundle"
            bundle.write_bytes(bundles[repository.name])
            git_command(root, "bundle", "verify", str(bundle))
            git_command(root, "bundle", "unbundle", str(bundle))
            git_command(root, "update-ref", "refs/heads/main", repository.source_sha)
            git_command(root, "reset", "--mixed", "HEAD")
            actual = _repository(root, repository.name, repository.source, limits)
            if actual.sha256 != repository.sha256 or actual.files != repository.files:
                raise IntakeError("SNAPSHOT_INTEGRITY_MISMATCH")
        local_manifest = {**manifest, "workspace_root": str(destination.resolve())}
        (workspace / "snapshot.json").write_bytes(_json(local_manifest))
        workspace.rename(destination)
        return Snapshot(
            manifest["snapshot_id"],
            manifest["project_id"],
            destination.resolve(),
            repositories,
            local_manifest,
        )
