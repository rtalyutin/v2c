"""Shared command admission rules. Validation grants no execution authority."""

from .source import safe_relative_path


def command_arguments(values: list[str]) -> list[str]:
    if any("\x00" in value for value in values) or sum(len(v.encode()) for v in values) > 100_000:
        raise ValueError("invalid command arguments")
    return values


def command_directory(value: str) -> str:
    if value != ".":
        safe_relative_path(value)
    return value
