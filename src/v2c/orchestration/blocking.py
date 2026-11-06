"""Bounded blocking work, without abandoning a producer after cancellation.

The async owner waits for an in-flight thread before propagating cancellation.
Publication inside a blocking operation must still use the original DB fence.
No worker task can finish while its local blocking producer is unaccounted for.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def run_blocking(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    task = asyncio.create_task(asyncio.to_thread(partial(function, *args, **kwargs)))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Cancellation takes precedence, but the thread's exception is observed.
        if not task.cancelled():
            task.exception()
        raise
