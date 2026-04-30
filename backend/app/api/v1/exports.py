"""
Export API — Phase 2B / 2C.

Endpoints:

    POST /api/v1/channels/{channel_id}/exports
        Create and enqueue a new export job.
        Phase 2C: validates in_time < out_time, rejects future dates,
        enforces max_export_duration_seconds.

    GET  /api/v1/exports/{job_id}
        Get the status / details of a single export job.

    GET  /api/v1/exports
        List all export jobs (optionally filtered by channel_id, status).

    POST /api/v1/exports/{job_id}/cancel
        Cancel a queued or running export job.

    GET  /api/v1/exports/{job_id}/logs       — Phase 2C
        Return the raw FFmpeg stderr log for a job.

    GET  /api/v1/exports/{job_id}/download   — Phase 2C
        Download the exported video file (completed jobs only).
"""
from __future__ import annotations

import logging
from datetime import date as _date
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
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
from ...utils import utc_now
from .deps import ExportDep

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
        actual_duration_seconds=job.actual_duration_seconds,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        preroll_seconds=job.preroll_seconds,
        postroll_seconds=job.postroll_seconds,
        never_expires=job.never_expires,
    )


def _require_channel(channel_id: str, db: Session) -> Channel:
    ch = db.query(Channel).filter(Channel.id == channel_id).first()
    if ch is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found.")
    return ch


def _parse_hms(t: str, field: str) -> tuple[int, int, int]:
    """Parse HH:MM:SS, raising 400 on invalid input."""
    try:
        parts = t.split(":")
        if len(parts) != 3:
            raise ValueError
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field} format '{t}'. Expected HH:MM:SS.",
        )


def _validate_export_request(body: ExportJobRequest) -> None:
    """
    Phase 2C API safety checks applied before any DB work.

    Raises HTTPException 400 on:
    - Malformed time strings
    - in_time >= out_time
    - date is in the future
    - Export duration exceeds max_export_duration_seconds (when > 0)
    """
    settings = get_settings()

    # Parse and validate date
    try:
        req_date = datetime.strptime(body.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format '{body.date}'. Expected YYYY-MM-DD.",
        )

    today = utc_now().date()
    if req_date > today:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Export date {body.date} is in the future. "
                "Only past and present dates are allowed."
            ),
        )

    # Parse times
    in_h, in_m, in_s = _parse_hms(body.in_time, "in_time")
    out_h, out_m, out_s = _parse_hms(body.out_time, "out_time")

    in_total = in_h * 3600 + in_m * 60 + in_s
    out_total = out_h * 3600 + out_m * 60 + out_s

    if out_total <= in_total:
        raise HTTPException(
            status_code=400,
            detail=(
                f"out_time ({body.out_time}) must be strictly after "
                f"in_time ({body.in_time})."
            ),
        )

    duration = out_total - in_total
    max_dur = settings.max_export_duration_seconds
    if max_dur > 0 and duration > max_dur:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Requested export duration {duration}s exceeds the "
                f"configured maximum of {max_dur}s "
                f"({max_dur / 3600:.1f} hours)."
            ),
        )

    # Phase 7 — validate pre/post roll
    if body.preroll_seconds < 0:
        raise HTTPException(
            status_code=400,
            detail="preroll_seconds must be non-negative.",
        )
    if body.postroll_seconds < 0:
        raise HTTPException(
            status_code=400,
            detail="postroll_seconds must be non-negative.",
        )


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
    _: ExportDep,
    db: Session = Depends(get_db),
):
    """
    Validate the time range, resolve required segments, and create a queued
    export job.

    Phase 2C validations:
    - in_time must be strictly before out_time
    - date must not be in the future
    - duration must not exceed max_export_duration_seconds (when configured)

    If gaps are detected in the resolved range a warning is embedded in the
    job record but the job is still created (unless ``allow_gaps=false``).
    """
    # Phase 2C: strict input validation before any DB/resolver work
    _validate_export_request(body)

    _require_channel(channel_id, db)

    # Validate the range and detect gaps via the Phase 2A resolver
    request = ResolveRangeRequest(
        date=body.date,
        in_time=body.in_time,
        out_time=body.out_time,
        preroll_seconds=body.preroll_seconds,
        postroll_seconds=body.postroll_seconds,
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
        preroll_seconds=body.preroll_seconds,
        postroll_seconds=body.postroll_seconds,
        never_expires=body.never_expires,
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
    _: ExportDep,
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
    _: ExportDep,
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
    _: ExportDep,
    db: Session = Depends(get_db),
):
    """
    Cancel a queued or running export job.

    - QUEUED jobs: status set to CANCELLED immediately.
    - RUNNING jobs: the subprocess is sent SIGTERM, partial output is removed
      by the worker coroutine, status set to CANCELLED.
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
    job.completed_at = utc_now()
    db.commit()
    db.refresh(job)

    logger.info("[export-api] Job %d cancelled.", job_id)
    return _job_to_response(job)


# ─── GET /exports/{job_id}/logs — Phase 2C ────────────────────────────────────

@router.get(
    "/exports/{job_id}/logs",
    response_class=PlainTextResponse,
    summary="Get FFmpeg log for an export job",
)
def get_export_job_logs(
    job_id: int,
    _: ExportDep,
    db: Session = Depends(get_db),
):
    """
    Return the raw FFmpeg stderr log captured during this export job.

    Available as soon as the job has started (the log file may grow while
    the job is still running).  Returns 404 if the job or log file does
    not exist yet.
    """
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail=f"Export job {job_id} not found.")

    if job.log_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"No log file recorded for job {job_id} yet.",
        )

    log_path = Path(job.log_path)
    if not log_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Log file not found on disk: {job.log_path}",
        )

    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not read log file: {exc}",
        )

    return PlainTextResponse(content=content)


# ─── GET /exports/{job_id}/download — Phase 2C ────────────────────────────────

@router.get(
    "/exports/{job_id}/download",
    summary="Download the exported video file",
)
def download_export_job(
    job_id: int,
    _: ExportDep,
    db: Session = Depends(get_db),
):
    """
    Stream the exported MP4 file to the client.

    Only available for **completed** jobs whose output file still exists on
    disk.  Returns:
    - 404 if the job does not exist
    - 409 if the job is not completed
    - 404 if the output file has been deleted (e.g. by retention cleanup)
    """
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail=f"Export job {job_id} not found.")

    if job.status != ExportJobStatus.COMPLETED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job {job_id} is not completed (status='{job.status}'). "
                "Only completed jobs can be downloaded."
            ),
        )

    if job.output_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"No output path recorded for job {job_id}.",
        )

    output_path = Path(job.output_path)
    if not output_path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"Output file no longer exists on disk: {job.output_path}. "
                "It may have been removed by the retention cleaner."
            ),
        )

    return FileResponse(
        path=str(output_path),
        media_type="video/mp4",
        filename=output_path.name,
    )

