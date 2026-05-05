"""
Monitoring API — v1.

Endpoints:
  GET /api/v1/channels/{id}/watchdog      recent watchdog events + health
  GET /api/v1/channels/{id}/anomalies     segment anomaly history
  GET /api/v1/channels/{id}/debug         detailed real-time diagnostics (Phase 1.6)
  GET /api/v1/system/health               aggregated health of all channels
  GET /api/v1/system/disk                 disk usage for the recording storage (Phase 3.5)
"""
from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...config.settings import get_settings, resolve_channel_path, resolve_date_folder
from ...db.models import Channel, SegmentAnomaly, WatchdogEvent
from ...db.session import get_db
from ...models.schemas import (
    ChannelDebugResponse,
    ChannelHealthResponse,
    DiskUsageResponse,
    HealthStatus,
    ProcessStatus,
    SegmentAnomalyResponse,
    SystemHealthResponse,
    WatchdogEventResponse,
)
from ...services.process_manager import get_process_manager
from .deps import AdminDep, AnyRoleDep

router = APIRouter(tags=["monitoring"])

DbDep = Annotated[Session, Depends(get_db)]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_channel_or_404(channel_id: str, db: Session) -> Channel:
    ch = db.query(Channel).filter(Channel.id == channel_id).first()
    if ch is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found.")
    return ch


def _channel_health_response(ch: Channel, db: Session) -> ChannelHealthResponse:
    pm = get_process_manager()
    events = (
        db.query(WatchdogEvent)
        .filter(WatchdogEvent.channel_id == ch.id)
        .order_by(WatchdogEvent.id.desc())
        .limit(5)
        .all()
    )
    return ChannelHealthResponse(
        channel_id=ch.id,
        channel_name=ch.name,
        status=pm.get_status(ch.id),
        health=pm.get_health(ch.id),
        pid=pm.get_pid(ch.id),
        last_seen_alive=pm.get_last_seen_alive(ch.id),
        recent_events=[
            WatchdogEventResponse(
                id=e.id,
                channel_id=e.channel_id,
                event_type=e.event_type,
                detected_at=e.detected_at,
                details=e.details,
                alert_type=e.alert_type,
                severity=e.severity if e.severity is not None else 0,
            )
            for e in events
        ],
    )


def _resolve_active_record_dir(paths) -> "Path | None":
    """
    Return the active recording directory for *paths* as a resolved ``Path``.

    * Date-based layout (Phase 23+): ``{record_root}/{today}/``
    * Legacy layout: ``paths.record_dir`` resolved via
      :func:`resolve_channel_path`.

    Returns ``None`` when neither is configured.
    """
    from pathlib import Path as _Path
    if paths.effective_use_date_folders and paths.record_root:
        return resolve_date_folder(paths.record_root, paths.date_folder_format)
    if paths.record_dir:
        return resolve_channel_path(paths.record_dir)
    return None


def _newest_mp4_mtime(paths) -> datetime | None:
    """Return the mtime of the newest *.mp4 in the active recording directory."""
    d = _resolve_active_record_dir(paths)
    if d is None or not d.exists():
        return None
    try:
        mp4s = list(d.glob("*.mp4"))
        if not mp4s:
            return None
        mtime = max(f.stat().st_mtime for f in mp4s)
        return datetime.utcfromtimestamp(mtime)
    except OSError:
        return None


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/channels/{channel_id}/watchdog", response_model=ChannelHealthResponse)
def get_watchdog_status(
    channel_id: str,
    db: DbDep,
    _: AnyRoleDep,
):
    """Watchdog health state and recent events for a channel."""
    ch = _get_channel_or_404(channel_id, db)
    return _channel_health_response(ch, db)


@router.get(
    "/channels/{channel_id}/anomalies", response_model=list[SegmentAnomalyResponse]
)
def get_segment_anomalies(
    channel_id: str,
    db: DbDep,
    _: AnyRoleDep,
    limit: int = Query(default=50, ge=1, le=500),
    resolved: bool | None = Query(default=None),
):
    """Segment anomaly history for a channel."""
    _get_channel_or_404(channel_id, db)
    q = (
        db.query(SegmentAnomaly)
        .filter(SegmentAnomaly.channel_id == channel_id)
        .order_by(SegmentAnomaly.id.desc())
    )
    if resolved is not None:
        q = q.filter(SegmentAnomaly.resolved == resolved)
    rows = q.limit(limit).all()
    return [
        SegmentAnomalyResponse(
            id=r.id,
            channel_id=r.channel_id,
            detected_at=r.detected_at,
            last_segment_time=r.last_segment_time,
            expected_interval_seconds=r.expected_interval_seconds,
            actual_gap_seconds=r.actual_gap_seconds,
            resolved=r.resolved,
        )
        for r in rows
    ]


@router.get("/channels/{channel_id}/debug", response_model=ChannelDebugResponse)
def get_channel_debug(channel_id: str, db: DbDep, _: AdminDep):
    """
    Detailed real-time diagnostics for a channel — Phase 1.6.

    Exposes: health, restart history, cooldown state, stall tracking,
    last segment time (derived from newest mp4 mtime in 1_record).
    """
    from ...models.schemas import ChannelConfig
    ch = _get_channel_or_404(channel_id, db)
    pm = get_process_manager()

    # Derive last_segment_time from the filesystem (independent of stall state)
    config = ChannelConfig.model_validate_json(ch.config_json)
    last_segment_time = _newest_mp4_mtime(config.paths)

    # Stall info
    stall_secs = pm.get_stall_seconds(channel_id)

    return ChannelDebugResponse(
        channel_id=ch.id,
        health=pm.get_health(ch.id),
        pid=pm.get_pid(ch.id),
        last_restart_time=pm.get_last_restart_time(ch.id),
        restart_count_window=pm.get_restart_count_window(ch.id),
        cooldown_remaining_seconds=pm.get_cooldown_remaining(ch.id),
        last_segment_time=last_segment_time,
        last_file_size=pm.get_last_file_size(ch.id),
        last_file_size_change_at=pm.get_last_file_size_change_at(ch.id),
        stall_seconds=stall_secs,
    )


@router.get("/system/health", response_model=SystemHealthResponse)
def get_system_health(db: DbDep, _: AnyRoleDep):
    """Aggregated health summary for all channels."""
    pm = get_process_manager()
    channels = db.query(Channel).order_by(Channel.id).all()

    channel_responses = [_channel_health_response(ch, db) for ch in channels]

    running = sum(1 for r in channel_responses if r.status == ProcessStatus.RUNNING)
    healthy = sum(1 for r in channel_responses if r.health == HealthStatus.HEALTHY)
    unhealthy = sum(1 for r in channel_responses if r.health == HealthStatus.UNHEALTHY)
    degraded = sum(1 for r in channel_responses if r.health == HealthStatus.DEGRADED)
    cooldown = sum(1 for r in channel_responses if r.health == HealthStatus.COOLDOWN)
    unknown = sum(1 for r in channel_responses if r.health == HealthStatus.UNKNOWN)

    return SystemHealthResponse(
        channels=channel_responses,
        total=len(channels),
        running=running,
        healthy=healthy,
        unhealthy=unhealthy,
        degraded=degraded,
        cooldown=cooldown,
        unknown=unknown,
    )


@router.get("/system/disk", response_model=DiskUsageResponse)
def get_disk_usage(_: AnyRoleDep) -> DiskUsageResponse:
    """Disk usage for the filesystem where recordings are stored.

    Priority:
      1. ``PGMREC_RECORDING_ROOT`` — the actual recording disk (preferred).
      2. ``data_dir`` — application data directory (fallback when
         ``PGMREC_RECORDING_ROOT`` is not configured).

    When ``PGMREC_RECORDING_ROOT`` is set but the directory does not yet
    exist, PGMRec attempts to create it.  If creation fails, the response
    includes a ``warning`` field and falls back to ``data_dir``.
    """
    settings = get_settings()
    warning: str | None = None
    disk_path: str

    if settings.recording_root is not None:
        rec_root = Path(settings.recording_root)
        created_or_exists = True
        if not rec_root.exists():
            try:
                rec_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                warning = (
                    f"PGMREC_RECORDING_ROOT '{rec_root}' does not exist and "
                    f"could not be created: {exc}. Disk usage reported for "
                    f"'{settings.data_dir}' instead."
                )
                created_or_exists = False
        disk_path = str(rec_root) if created_or_exists else str(settings.data_dir)
    else:
        disk_path = str(settings.data_dir)

    try:
        usage = shutil.disk_usage(disk_path)
    except OSError:
        disk_path = "/"
        usage = shutil.disk_usage(disk_path)
    total = usage.total
    used = usage.used
    free = usage.free
    percent = round(used / total * 100, 1) if total > 0 else 0.0
    return DiskUsageResponse(
        path_checked=disk_path,
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
        percent_used=percent,
        warning=warning,
    )
