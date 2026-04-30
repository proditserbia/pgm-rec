"""
Export Worker — Phase 2B.

Implements an asyncio-based background worker that processes queued export
jobs.  A single global ``ExportWorker`` instance is created at startup and
shared across API handlers.

Design
──────
- A single asyncio ``Task`` runs ``_loop()``, which polls for QUEUED jobs
  from the DB every *poll_interval_seconds* seconds.
- Each job is run inside ``asyncio.create_task()`` so multiple jobs can run
  concurrently, gated by an ``asyncio.Semaphore``.
- Running subprocess handles are stored in ``_processes`` so the cancel
  endpoint can send SIGTERM immediately without waiting for the poll cycle.
- Jobs moved to CANCELLED status before the worker picks them up are silently
  skipped.

Public API
──────────
- ``get_export_worker()``   → the singleton ExportWorker
- ``ExportWorker.start()``  → launch the background loop (called from lifespan)
- ``ExportWorker.stop()``   → cancel the loop (called on shutdown)
- ``ExportWorker.enqueue(job_id)``   → wake the worker immediately
- ``ExportWorker.cancel_job(job_id)`` → cancel a queued or running job
- ``ExportWorker.register_process(job_id, proc)``   → called by run_export_job
- ``ExportWorker.unregister_process(job_id)``        → called by run_export_job
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 5.0  # seconds between DB polls when idle


class ExportWorker:
    """
    Background worker that drains the QUEUED export jobs from the DB.

    Thread-safety: intended to run entirely within a single asyncio event loop.
    """

    def __init__(self, max_concurrent: int = 2) -> None:
        self._max_concurrent = max_concurrent
        self._semaphore: asyncio.Semaphore | None = None
        self._loop_task: asyncio.Task | None = None
        # job_id → running subprocess (for immediate cancel)
        self._processes: dict[int, asyncio.subprocess.Process] = {}
        # job_id → asyncio Task (for task-level cancel)
        self._job_tasks: dict[int, asyncio.Task] = {}
        # wake-up event: set when a new job is enqueued
        self._wake: asyncio.Event | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the background polling loop.  Call once from app lifespan."""
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._wake = asyncio.Event()
        self._loop_task = asyncio.create_task(self._loop(), name="pgmrec-export-worker")
        logger.info("[export-worker] Started (max_concurrent=%d).", self._max_concurrent)

    async def stop(self) -> None:
        """Cancel the background loop gracefully."""
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        # Cancel any running jobs
        for job_id, proc in list(self._processes.items()):
            logger.info("[export-worker] Terminating job %d on shutdown.", job_id)
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        logger.info("[export-worker] Stopped.")

    # ── Job management ────────────────────────────────────────────────────

    def enqueue(self, job_id: int) -> None:
        """Wake the worker immediately so it picks up the new job without delay."""
        if self._wake:
            self._wake.set()
        logger.debug("[export-worker] Enqueued job %d.", job_id)

    def cancel_job(self, job_id: int) -> bool:
        """
        Attempt to cancel *job_id*.

        - If the job has a running subprocess, send SIGTERM.
        - If the job has a running asyncio Task, cancel it.
        - Returns True if a cancellation signal was sent; False if nothing was
          running (the DB status update is the caller's responsibility for
          QUEUED jobs).
        """
        sent = False
        proc = self._processes.get(job_id)
        if proc is not None:
            try:
                proc.terminate()
                sent = True
                logger.info("[export-worker] Sent SIGTERM to job %d (pid=%d).", job_id, proc.pid)
            except (ProcessLookupError, OSError):
                pass

        task = self._job_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            sent = True
            logger.info("[export-worker] Cancelled asyncio task for job %d.", job_id)

        return sent

    def register_process(self, job_id: int, proc: asyncio.subprocess.Process) -> None:
        self._processes[job_id] = proc

    def unregister_process(self, job_id: int) -> None:
        self._processes.pop(job_id, None)

    # ── Internal loop ─────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """
        Continuously poll the DB for QUEUED jobs and dispatch them.

        Wakes immediately on ``enqueue()`` or after *_POLL_INTERVAL* seconds,
        whichever comes first.
        """
        assert self._wake is not None
        assert self._semaphore is not None

        while True:
            # Wait for a wake signal or the poll interval, then clear the event
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

            await self._dispatch_queued()

    async def _dispatch_queued(self) -> None:
        """Fetch all QUEUED jobs from the DB and start tasks for each."""
        from ..db.session import get_session_factory
        from ..db.models import ExportJob
        from ..models.schemas import ExportJobStatus

        SessionLocal = get_session_factory()
        try:
            with SessionLocal() as db:
                jobs = (
                    db.query(ExportJob)
                    .filter(ExportJob.status == ExportJobStatus.QUEUED)
                    .order_by(ExportJob.created_at)
                    .all()
                )
                job_ids = [j.id for j in jobs]
        except Exception as exc:
            logger.error("[export-worker] DB query failed: %s", exc)
            return

        for job_id in job_ids:
            if job_id in self._job_tasks and not self._job_tasks[job_id].done():
                continue  # already running
            task = asyncio.create_task(
                self._run_with_semaphore(job_id),
                name=f"pgmrec-export-job-{job_id}",
            )
            self._job_tasks[job_id] = task

    async def _run_with_semaphore(self, job_id: int) -> None:
        """Acquire the concurrency semaphore, run the job, release on exit."""
        assert self._semaphore is not None
        async with self._semaphore:
            try:
                from .export_service import run_export_job
                await run_export_job(job_id)
            except asyncio.CancelledError:
                logger.info("[export-worker] Job %d task cancelled.", job_id)
            except Exception as exc:
                logger.error("[export-worker] Unhandled error in job %d: %s", job_id, exc)
            finally:
                self._job_tasks.pop(job_id, None)


# ─── Singleton ────────────────────────────────────────────────────────────────

_worker: ExportWorker | None = None


def get_export_worker() -> ExportWorker:
    """Return the global ExportWorker singleton (created on first call)."""
    global _worker
    if _worker is None:
        from ..config.settings import get_settings
        settings = get_settings()
        _worker = ExportWorker(max_concurrent=settings.max_concurrent_exports)
    return _worker
