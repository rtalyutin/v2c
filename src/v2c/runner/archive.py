"""Bounded extraction of untrusted project archives."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path, PurePosixPath

from ..adapters.runner import RunnerError
from .settings import RunnerSettings


def _path(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if "\\" in name or "\0" in name or p.is_absolute() or ".." in p.parts or not p.parts:
        raise RunnerError("INVALID_INPUT", "unsafe archive/workspace path")
    return p


def safe_extract(archive: bytes, destination: Path, settings: RunnerSettings) -> None:
    if len(archive) > settings.max_archive_bytes:
        raise RunnerError("LIMIT", "archive exceeds compressed limit")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            infos = z.infolist()
            if (
                len(infos) > settings.max_files
                or sum(i.file_size for i in infos) > settings.max_expanded_bytes
            ):
                raise RunnerError("LIMIT", "archive exceeds expanded limits")
            seen: set[str] = set()
            for info in infos:
                p = _path(info.filename)
                if str(p) in seen:
                    raise RunnerError("INVALID_INPUT", "duplicate archive path")
                seen.add(str(p))
                mode = info.external_attr >> 16
                # Reject links, devices and all non-file/directory Unix modes.
                kind = mode & 0o170000
                if kind not in (0, 0o100000, 0o040000):
                    raise RunnerError("INVALID_INPUT", "archive contains a nonregular entry")
                target = destination.joinpath(*p.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as source, target.open("xb") as output:
                        copied = 0
                        while chunk := source.read(min(65536, settings.max_expanded_bytes + 1)):
                            copied += len(chunk)
                            if copied > info.file_size:
                                raise RunnerError("LIMIT", "archive size mismatch")
                            output.write(chunk)
                    target.chmod(0o755 if mode & 0o111 else 0o644)
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        raise RunnerError("INVALID_INPUT", "invalid project archive") from error
