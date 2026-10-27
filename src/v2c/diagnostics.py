"""Safe correlation for control-plane failures, never exception payloads.

Exception messages, locals, source lines and request bodies are deliberately
excluded. Only application-owned identifiers and stack locations are retained.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import TypeAlias

ContextValue: TypeAlias = str | int | None
_FIELDS = frozenset(
    {"worker_id", "run_id", "step_id", "attempt_id", "generation", "command_id", "allocation_id"}
)
_context: ContextVar[dict[str, ContextValue]] = ContextVar("v2c_diagnostic_context", default={})


def reset_context(*, worker_id: str) -> None:
    _context.set({"worker_id": worker_id})


def bind_context(**values: ContextValue) -> None:
    _context.set({**_context.get(), **_allowed(values)})


def _allowed(values: dict[str, ContextValue]) -> dict[str, ContextValue]:
    return {
        key: value
        for key, value in values.items()
        if key in _FIELDS and isinstance(value, (str, int))
    }


def annotate_exception(error: Exception, **values: ContextValue) -> None:
    """Carry known operation identity from an asyncio task to its supervisor."""
    previous = getattr(error, "_v2c_diagnostic_context", {})
    if not isinstance(previous, dict):
        previous = {}
    error._v2c_diagnostic_context = {**previous, **_allowed(values)}  # type: ignore[attr-defined]


def failure_record(error: Exception, *, worker_id: str) -> dict[str, object]:
    locations: list[dict[str, str | int]] = []
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = str(frame.f_globals.get("__name__", "unknown"))
        # Only application frames. No absolute host paths or third-party values.
        if module == "v2c" or module.startswith("v2c."):
            locations.append(
                {"module": module, "function": frame.f_code.co_name, "line": traceback.tb_lineno}
            )
        traceback = traceback.tb_next
    attached = getattr(error, "_v2c_diagnostic_context", {})
    context = _allowed(attached) if isinstance(attached, dict) else {}
    return {
        "event": "worker_operation_failed",
        **_context.get(),
        **context,
        "worker_id": worker_id,
        "error_class": type(error).__name__,
        "locations": locations,
        "recovery": "read_durable_state_before_retry",
    }


def log_failure(logger: logging.Logger, error: Exception, *, worker_id: str) -> None:
    logger.error(
        json.dumps(failure_record(error, worker_id=worker_id), sort_keys=True), exc_info=False
    )
