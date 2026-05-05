"""
Recording Retention API — Phase 25.

Endpoints:
  POST /api/v1/retention/run   Admin-only endpoint to trigger or preview
                                recording segment retention cleanup.

Usage:
    Dry run (default) — see what would be deleted:
        POST /api/v1/retention/run
        { "dry_run": true }

    Live run for all channels:
        POST /api/v1/retention/run
        { "dry_run": false }

    Live run for one channel:
        POST /api/v1/retention/run
        { "channel_id": "rts1", "dry_run": false }
"""
from __future__ import annotations

from fastapi import APIRouter

from ...models.schemas import RetentionRunRequest, RetentionRunResponse
from ...services.retention import run_channel_retention
from .deps import AdminDep

router = APIRouter(tags=["retention"])


@router.post("/retention/run", response_model=RetentionRunResponse)
async def trigger_retention_run(
    request: RetentionRunRequest,
    _: AdminDep,
) -> RetentionRunResponse:
    """
    Trigger a recording-segment retention cleanup run.

    - ``dry_run=true`` (default): returns what *would* be deleted without
      modifying any files or DB records.  Safe to call at any time.
    - ``dry_run=false``: actually deletes eligible segment files.  Subject
      to all the same safety rules as the automatic scheduler (current-day
      protection, ``never_expires`` guard, etc.).

    Only ``admin`` role may call this endpoint.
    """
    return await run_channel_retention(
        channel_id=request.channel_id,
        dry_run=request.dry_run,
    )
