"""
Manifest & Export Index API — Phase 2A.

Endpoints (all nested under /api/v1/channels/{channel_id}):

  GET  /manifests/{date}           — return full daily manifest JSON
  GET  /segments?date=YYYY-MM-DD   — list segment records from DB
  POST /exports/resolve-range      — resolve an export time range
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...config.settings import get_settings
from ...db.models import Channel, SegmentRecord
from ...db.session import get_db
from ...models.schemas import (
    DailyManifest,
    ResolveRangeRequest,
    ResolveRangeResponse,
    SegmentEntry,
    SegmentStatus,
)
from ...services.manifest_service import load_manifest, resolve_export_range

router = APIRouter(tags=["manifests"])

DbDep = Annotated[Session, Depends(get_db)]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_channel_or_404(channel_id: str, db: Session) -> Channel:
    ch = db.query(Channel).filter(Channel.id == channel_id).first()
    if ch is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found.")
    return ch


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/channels/{channel_id}/manifests/{date}", response_model=DailyManifest)
def get_manifest(channel_id: str, date: str, db: DbDep):
    """
    Return the full daily recording manifest for a channel and date.

    The manifest JSON is the source of truth: it lists every completed
    segment, their verified durations, and any detected gaps.

    *date* must be in YYYY-MM-DD format.
    """
    _get_channel_or_404(channel_id, db)
    settings = get_settings()
    manifest = load_manifest(channel_id, date, settings.manifests_dir)
    if manifest is None:
        raise HTTPException(
            status_code=404,
            detail=f"No manifest found for channel '{channel_id}' on {date}.",
        )
    return manifest


@router.get("/channels/{channel_id}/segments", response_model=list[SegmentEntry])
def list_segments(
    channel_id: str,
    db: DbDep,
    date: str = Query(..., description="YYYY-MM-DD"),
):
    """
    List all registered segments for a channel on a given date.

    Data is served from the DB index (fast).  For the full manifest including
    gap analysis, use GET /manifests/{date}.
    """
    _get_channel_or_404(channel_id, db)
    rows = (
        db.query(SegmentRecord)
        .filter(
            SegmentRecord.channel_id == channel_id,
            SegmentRecord.manifest_date == date,
        )
        .order_by(SegmentRecord.start_time)
        .all()
    )
    return [
        SegmentEntry(
            filename=r.filename,
            path=r.path,
            start_time=r.start_time,
            end_time=r.end_time,
            duration_seconds=r.duration_seconds,
            size_bytes=r.size_bytes,
            status=SegmentStatus(r.status),
            created_at=r.created_at,
            ffprobe_verified=r.ffprobe_verified,
        )
        for r in rows
    ]


@router.post("/channels/{channel_id}/exports/resolve-range", response_model=ResolveRangeResponse)
def resolve_range(channel_id: str, request: ResolveRangeRequest, db: DbDep):
    """
    Resolve an export time range to the required segment files.

    Accepts a date (YYYY-MM-DD), in_time (HH:MM:SS), and out_time (HH:MM:SS)
    in UTC.  Returns the ordered list of segment files that cover the range,
    the trim offset for the first segment, the total export duration, and any
    gaps detected within the range.

    Does NOT perform any actual video processing — this is the index/planning
    step only.
    """
    _get_channel_or_404(channel_id, db)
    try:
        return resolve_export_range(channel_id, request, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
