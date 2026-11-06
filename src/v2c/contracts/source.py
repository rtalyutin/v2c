"""Neutral source input values, path safety and fetch protocol."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol


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
class RepositoryInput:
    name: str
    url: str
    revision: str
    secret_ref: str | None = None


class GitFetcher(Protocol):
    def fetch(self, source: RepositoryInput, destination: Path, limits: ImportLimits) -> None: ...


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
