"""Bounded, deterministic source archive and patch operations."""

from __future__ import annotations

import base64
import hashlib
import io
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any

from ..contracts.workflow import JsonObject
from ..delivery import contains_secret
from ..intake import Snapshot, inspect_snapshot, safe_relative_path, snapshot_archive_bytes
from .common import InputRequired, StepFailure, json_bytes
from .schemas import PatchProposal


def code_zip(files: dict[str, bytes], *, executable_paths: set[str] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, body in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.external_attr = (
                0o100755 if name in (executable_paths or set()) else 0o100644
            ) << 16
            archive.writestr(info, body)
    return output.getvalue()


def archive_executables(data: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {
            safe_relative_path(item.filename)
            for item in archive.infolist()
            if not item.is_dir() and (item.external_attr >> 16) & 0o111
        }


def checked_archive_files(
    data: bytes, *, max_file_bytes: int = 16 * 1024 * 1024
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            name = safe_relative_path(item.filename)
            if name in files:
                raise InputRequired("CHECKPOINT_DUPLICATE_PATH")
            total += item.file_size
            if item.file_size > max_file_bytes or total > 256 * 1024 * 1024 or len(files) >= 20000:
                raise InputRequired("CHECKPOINT_SIZE_LIMIT")
            files[name] = archive.read(item)
    return files


def assert_source_unchanged(expected: bytes, observed: bytes) -> bytes:
    """Tests may create outputs, but cannot silently replace the candidate code."""
    source, actual = checked_archive_files(expected), checked_archive_files(observed)
    changed = [path for path, body in source.items() if actual.get(path) != body]
    if changed:
        raise StepFailure("SOURCE_MUTATED_BY_EXECUTION", {"files": changed})
    expected_modes = archive_executables(expected)
    actual_modes = archive_executables(observed) & source.keys()
    if expected_modes != actual_modes:
        raise StepFailure(
            "SOURCE_MODE_MUTATED_BY_EXECUTION", {"files": sorted(expected_modes ^ actual_modes)}
        )
    # Reproducible dependencies/build/test outputs are recreated by the recipe;
    # only explicitly accepted source files become the delivered code version.
    return code_zip({path: actual[path] for path in source}, executable_paths=expected_modes)


_SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bsk-[A-Za-z0-9_-]{20,}\b"
)


def screened_text(path: str, data: bytes) -> str | None:
    """Conservative context screen; deliberately not a security certification."""
    name = PurePosixPath(path).name.casefold()
    if name == ".env" or (
        name.startswith(".env.") and not name.endswith(("example", "sample", "template"))
    ):
        raise InputRequired("SOURCE_SECRET_REVIEW_REQUIRED", {"path": path})
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if _SECRET.search(text) or contains_secret(data):
        raise InputRequired("SOURCE_SECRET_REVIEW_REQUIRED", {"path": path})
    return text


def snapshot_archive(snapshot: Snapshot) -> bytes:
    """Export inspected code, without host paths or Git implementation files."""
    inspect_snapshot(snapshot)
    for repo in snapshot.repositories:
        for item in repo.files:
            path = repo.path + "/" + item["path"]
            screened_text(path, (snapshot.root / path).read_bytes())
    return snapshot_archive_bytes(snapshot)


def archive_context(
    data: bytes, *, max_bytes: int, selected: list[str] | None = None
) -> JsonObject:
    """Bounded selected source, with explicit omitted paths instead of silent RAG."""
    result: dict[str, Any] = {"files": [], "omitted_files": []}
    remaining = max_bytes
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = sorted(item.filename for item in archive.infolist() if not item.is_dir())
        if selected is not None:
            priority = [name for name in names if name in selected]
            names = priority + [name for name in names if name not in priority]
        for name in names:
            safe_relative_path(name)
            info = archive.getinfo(name)
            if info.file_size > remaining or info.file_size > max_bytes:
                result["omitted_files"].append(name)
                continue
            body = archive.read(name)
            text = screened_text(name, body)
            if text is None:
                result["omitted_files"].append(name)
                continue
            encoded_size = len(json_bytes({"path": name, "text": text}))
            if encoded_size > remaining:
                result["omitted_files"].append(name)
                continue
            result["files"].append(
                {"path": name, "sha256": hashlib.sha256(body).hexdigest(), "text": text}
            )
            remaining -= encoded_size
    return result


_APPLY_FILES = r"""
import base64, hashlib, json, pathlib, sys
root = pathlib.Path.cwd().resolve()
items = json.loads(base64.b64decode(sys.argv[1]))
for item in items:
    path = root / item['path']
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('unsafe patch path')
    current = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    if current != item['old_sha256']:
        raise ValueError('patch base mismatch')
for item in items:
    path = root / item['path']
    if item['content_b64'] is None:
        path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(item['content_b64'], validate=True))
print('v2c file operation completed')
"""


def patched_archive(archive: bytes, patch: PatchProposal) -> bytes:
    files = checked_archive_files(archive)
    executable_paths = archive_executables(archive)
    for change in patch.files:
        if change.content_b64 is None:
            del files[change.path]
            executable_paths.discard(change.path)
        else:
            files[change.path] = base64.b64decode(change.content_b64, validate=True)
    return code_zip(files, executable_paths=executable_paths)
