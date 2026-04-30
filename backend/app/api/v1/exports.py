"""
Export API — Phase 2B.

Endpoints:

    POST /api/v1/channels/{channel_id}/exports
        Create and enqueue a new export job.

    GET  /api/v1/exports/{job_id}
        Get the status / details of a single export job.

    GET  /api/v1/exports
        List all export jobs (optionally filtered by channel_id, status).

    POST /api/v1/exports/{job_id}/cancel
        Cancel a queued or running export job.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...config.settings import get_settings
from ...db.models import Channel, ExportJob
from ...db.session import get_db
from ...models.schemas import (
    ExportJobRequest,
    ExportJobResponse,
    ExportJobStatus,
    ResolveRangeRequest,
)
from ...services.export_worker import get_export_worker
from ...services.manifest_service import resolve_export_range

logger = logging.getLogger(__name__)

router = APIRouter(tags=["exports"])


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _job_to_response(job: ExportJob) -> ExportJobResponse:
    return ExportJobResponse(
        id=job.id,
        channel_id=job.channel_id,
        date=job.date,
        in_time=job.in_time,
        out_time=job.out_time,
        status=ExportJobStatus(job.status),
        progress_percent=job.progress_percent,
        output_path=job.output_path,
        log_path=job.log_path,
        error_message=job.error_message,
        has_gaps=job.has_gaps,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def _require_channel(channel_id: str, db: Session) -> Channel:
    ch = db.query(Channel).filter(Channel.id == channel_id).first()
    if ch is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found.")
    return ch


# ─── POST /channels/{channel_id}/exports ──────────────────────────────────────

@router.post(
    "/channels/{channel_id}/exports",
    response_model=ExportJobResponse,
    status_code=201,
    summary="Create and enqueue an export job",
)
def create_export_job(
    channel_id: str,
    body: ExportJobRequest,
    db: Session = Depends(get_db),
):
    """
    Validate the time range, resolve required segments, and create a queued
    export job.

    If gaps are detected in the resolved range a warning is embedded in the
    job record but the job is still created (unless ``allow_gaps=false``).
    """
    _require_channel(channel_id, db)

    # Validate the range and detect gaps via the Phase 2A resolver
    request = ResolveRangeRequest(
        date=body.date,
        in_time=body.in_time,
        out_time=body.out_time,
    )
    try:
        resolve = resolve_export_range(channel_id, request, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if resolve.has_gaps and not body.allow_gaps:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Gaps detected in export range ({len(resolve.gaps)} gap(s)). "
                "Set allow_gaps=true to create the job anyway."
            ),
        )

    if not resolve.segments:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No recorded segments found for channel '{channel_id}' on "
                f"{body.date} between {body.in_time} and {body.out_time}. "
                "Ensure segments have been registered in the manifest."
            ),
        )

    job = ExportJob(
        channel_id=channel_id,
        date=body.date,
        in_time=body.in_time,
        out_time=body.out_time,
        status=ExportJobStatus.QUEUED,
        progress_percent=0.0,
        has_gaps=resolve.has_gaps,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Wake the worker so it picks up this job without waiting for the next poll
    get_export_worker().enqueue(job.id)

    logger.info(
        "[export-api] Created job %d for %s %s %s–%s (gaps=%s).",
        job.id, channel_id, body.date, body.in_time, body.out_time, resolve.has_gaps,
    )
    return _job_to_response(job)


# ─── GET /exports/{job_id} ────────────────────────────────────────────────────

@router.get(
    "/exports/{job_id}",
    response_model=ExportJobResponse,
    summary="Get export job details",
)
def get_export_job(
    job_id: int,
    db: Session = Depends(get_db),
):
    """Return the current status and metadata for one export job."""
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail=f"Export job {job_id} not found.")
    return _job_to_response(job)


# ─── GET /exports ─────────────────────────────────────────────────────────────

@router.get(
    "/exports",
    response_model=list[ExportJobResponse],
    summary="List export jobs",
)
def list_export_jobs(
    channel_id: Optional[str] = Query(None, description="Filter by channel ID"),
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """
    Return a paginated list of export jobs, newest first.

    Optional query params:
    - ``channel_id`` — filter to one channel
    - ``status``     — one of: queued | running | completed | failed | cancelled
    - ``limit``      — max results (default 50, max 500)
    """
    query = db.query(ExportJob)
    if channel_id:
        query = query.filter(ExportJob.channel_id == channel_id)
    if status:
        try:
            ExportJobStatus(status)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{status}'. "
                       "Must be one of: queued, running, completed, failed, cancelled.",
            )
        query = query.filter(ExportJob.status == status)
    jobs = query.order_by(ExportJob.created_at.desc()).limit(limit).all()
    return [_job_to_response(j) for j in jobs]


# ─── POST /exports/{job_id}/cancel ───────────────────────────────────────────

@router.post(
    "/exports/{job_id}/cancel",
    response_model=ExportJobResponse,
    summary="Cancel an export job",
)
def cancel_export_job(
    job_id: int,
    db: Session = Depends(get_db),
):
    """
    Cancel a queued or running export job.

    - QUEUED jobs: status set to CANCELLED immediately.
    - RUNNING jobs: the subprocess is sent SIGTERM, status set to CANCELLED.
    - COMPLETED / FAILED / CANCELLED jobs: 409 Conflict.
    """
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail=f"Export job {job_id} not found.")

    if job.status in (
        ExportJobStatus.COMPLETED,
        ExportJobStatus.FAILED,
        ExportJobStatus.CANCELLED,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Job {job_id} is already in terminal state '{job.status}'.",
        )

    # Signal the worker (no-op if not running yet)
    get_export_worker().cancel_job(job_id)

    job.status = ExportJobStatus.CANCELLED
    job.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)

    logger.info("[export-api] Job %d cancelled.", job_id)
    return _job_to_response(job)
