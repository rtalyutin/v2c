"""Separate worker process; the API never keeps a request open for a run."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid

from .engine import DomainError

logger = logging.getLogger(__name__)


async def run_once(engine, pipeline, worker_id: str, *, lease_seconds: int = 60) -> bool:
    for run in engine.list_pending_cancel_runs():
        await pipeline.cancel_run(run["id"])
    claim = engine.claim_next(worker_id, lease_seconds=lease_seconds)
    recovered = False
    if claim is None:
        for run in engine.list_recoverable_runs():
            try:
                claim = engine.recover_expired(run["id"], worker_id, lease_seconds=lease_seconds)
                recovered = True
                break
            except DomainError as error:
                if error.code not in {
                    "LEASE_NOT_EXPIRED",
                    "RUN_NOT_RECOVERABLE",
                    "OTHER_RUN_ACTIVE",
                }:
                    raise
    if claim is None:
        return False

    async def heartbeat():
        while True:
            await asyncio.sleep(max(0.05, lease_seconds / 3))
            engine.heartbeat(
                claim["attempt"]["id"], claim["attempt"]["generation"], worker_id, lease_seconds
            )

    async def work():
        if recovered and not await pipeline.reconcile(claim):
            return
        return await pipeline.execute(claim)

    task = asyncio.create_task(work())
    pulse = asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait({task, pulse}, return_when=asyncio.FIRST_COMPLETED)
        if pulse in done:
            # Lost lease / explicit cancel: stop this local producer. Remote VM
            # stop must still be read back before the run can become CANCELLED.
            exception = pulse.exception()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            run = engine.get_run(claim["run"]["id"])
            if run["cancel_requested"]:
                await pipeline.cancel_run(run["id"])
                return True
            if exception is not None:
                raise exception
        await task
        return True
    finally:
        if not pulse.done():
            pulse.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pulse


async def serve(
    engine,
    pipeline,
    *,
    worker_id: str | None = None,
    lease_seconds: int = 60,
    idle_seconds: float = 1.0,
    stop: asyncio.Event | None = None,
) -> None:
    worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    while not stop.is_set():
        try:
            worked = await run_once(engine, pipeline, worker_id, lease_seconds=lease_seconds)
        except Exception:
            # No exception payload: provider/HTTP exceptions can include secrets.
            logger.error("Worker operation failed; durable state requires recovery", exc_info=False)
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=idle_seconds)
            except TimeoutError:
                pass


def main() -> None:
    from .config import Settings, build_pipeline
    from .engine import Engine

    settings = Settings.from_env()
    engine = Engine(settings.database_url)
    pipeline = build_pipeline(settings, engine)
    asyncio.run(serve(engine, pipeline, lease_seconds=settings.lease_seconds))


if __name__ == "__main__":
    main()
