"""
Simple asyncio-based background task scheduler.

Each registered job is a zero-argument coroutine factory (callable that returns
a coroutine).  Jobs run on a fixed interval: sleep → execute → repeat.
Exceptions inside a job are caught and logged so one failing job doesn't kill
the others.

Usage::

    scheduler = BackgroundScheduler()
    scheduler.add("file_mover", 30, my_async_fn)
    await scheduler.start()
    # ... server is running ...
    await scheduler.stop()
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _Job:
    name: str
    interval: float
    fn: Callable[[], Awaitable[Any]]
    task: asyncio.Task | None = field(default=None, init=False)


class BackgroundScheduler:
    """Non-blocking asyncio periodic task scheduler."""

    def __init__(self) -> None:
        self._jobs: list[_Job] = []

    def add(self, name: str, interval: float, fn: Callable[[], Awaitable[Any]]) -> None:
        """Register a job.  Must be called before :meth:`start`."""
        self._jobs.append(_Job(name=name, interval=interval, fn=fn))

    async def start(self) -> None:
        """Create asyncio tasks for all registered jobs."""
        for job in self._jobs:
            job.task = asyncio.create_task(
                self._periodic(job), name=f"pgmrec-{job.name}"
            )
            logger.info("Scheduler: started job '%s' (every %.0fs).", job.name, job.interval)

    async def stop(self) -> None:
        """Cancel all running tasks and wait for them to finish."""
        for job in self._jobs:
            if job.task and not job.task.done():
                job.task.cancel()
                try:
                    await job.task
                except asyncio.CancelledError:
                    pass
                job.task = None
        logger.info("Scheduler: all jobs stopped.")

    @staticmethod
    async def _periodic(job: _Job) -> None:
        """Run *job.fn* every *job.interval* seconds, forever."""
        while True:
            await asyncio.sleep(job.interval)
            try:
                await job.fn()
            except Exception:
                logger.exception("Scheduler: unhandled error in job '%s'.", job.name)


# ─── Module-level singleton ────────────────────────────────────────────────────

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler()
    return _scheduler
