"""
Channel API — v1.

Endpoints:
  GET  /api/v1/channels/                    list all channels
  GET  /api/v1/channels/{id}                channel detail + config + status
  GET  /api/v1/channels/{id}/status         live process status
  POST /api/v1/channels/{id}/start          start recording
  POST /api/v1/channels/{id}/stop           stop recording
  POST /api/v1/channels/{id}/restart        restart recording
  GET  /api/v1/channels/{id}/logs           tail the FFmpeg log
  GET  /api/v1/channels/{id}/command        preview the FFmpeg command (dry-run)
  GET  /api/v1/channels/{id}/history        recent process records from DB
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...db.models import Channel, ProcessRecord
from ...db.session import get_db
from ...models.schemas import (
    ActionResponse,
    ChannelConfig,
    ChannelDetailResponse,
    ChannelStatusResponse,
    ChannelSummary,
    CommandPreviewResponse,
    HealthStatus,
    LogsResponse,
    ProcessHistoryEntry,
    ProcessStatus,
)
from ...services.ffmpeg_builder import build_ffmpeg_command, format_command_for_log
from ...services.process_manager import get_process_manager
from .deps import AdminDep, AnyRoleDep

router = APIRouter(prefix="/channels", tags=["channels"])

DbDep = Annotated[Session, Depends(get_db)]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_channel_or_404(channel_id: str, db: Session) -> Channel:
    ch = db.query(Channel).filter(Channel.id == channel_id).first()
    if ch is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found.")
    return ch


def _status_response(ch: Channel) -> ChannelStatusResponse:
    pm = get_process_manager()
    proc_status = pm.get_status(ch.id)
    pid = pm.get_pid(ch.id)
    started_at = pm.get_started_at(ch.id)
    log_path = pm.get_log_path(ch.id)
    uptime: float | None = None
    if started_at and proc_status == ProcessStatus.RUNNING:
        uptime = (datetime.now(timezone.utc) - started_at).total_seconds()
    return ChannelStatusResponse(
        channel_id=ch.id,
        channel_name=ch.name,
        status=proc_status,
        health=pm.get_health(ch.id),
        pid=pid,
        started_at=started_at,
        uptime_seconds=uptime,
        last_seen_alive=pm.get_last_seen_alive(ch.id),
        log_path=str(log_path) if log_path else None,
    )


def _summary(ch: Channel) -> ChannelSummary:
    pm = get_process_manager()
    return ChannelSummary(
        id=ch.id,
        name=ch.name,
        display_name=ch.display_name,
        enabled=ch.enabled,
        status=pm.get_status(ch.id),
        health=pm.get_health(ch.id),
        pid=pm.get_pid(ch.id),
    )


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[ChannelSummary])
def list_channels(db: DbDep, _: AnyRoleDep):
    """List all configured channels with live status."""
    return [_summary(ch) for ch in db.query(Channel).order_by(Channel.id).all()]


@router.get("/{channel_id}", response_model=ChannelDetailResponse)
def get_channel(channel_id: str, db: DbDep, _: AnyRoleDep):
    """Full channel details including config and live process status."""
    ch = _get_channel_or_404(channel_id, db)
    config = ChannelConfig.model_validate_json(ch.config_json)
    return ChannelDetailResponse(
        summary=_summary(ch),
        config=config,
        status=_status_response(ch),
    )


@router.get("/{channel_id}/status", response_model=ChannelStatusResponse)
def get_status(channel_id: str, db: DbDep, _: AnyRoleDep):
    """Live recording status (PID, uptime, log path)."""
    ch = _get_channel_or_404(channel_id, db)
    return _status_response(ch)


@router.post("/{channel_id}/start", response_model=ActionResponse)
def start_channel(channel_id: str, db: DbDep, _: AdminDep):
    """Start recording for a channel."""
    ch = _get_channel_or_404(channel_id, db)
    if not ch.enabled:
        raise HTTPException(status_code=400, detail=f"Channel '{channel_id}' is disabled.")
    pm = get_process_manager()
    config = ChannelConfig.model_validate_json(ch.config_json)
    try:
        info = pm.start(channel_id, config, db)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to start FFmpeg: {exc}")
    return ActionResponse(
        success=True,
        message=f"Recording started (PID {info.pid}).",
        channel_id=channel_id,
        status=ProcessStatus.RUNNING,
    )


@router.post("/{channel_id}/stop", response_model=ActionResponse)
def stop_channel(channel_id: str, db: DbDep, _: AdminDep):
    """Stop recording for a channel."""
    ch = _get_channel_or_404(channel_id, db)
    pm = get_process_manager()
    stopped = pm.stop(channel_id, db)
    if not stopped:
        return ActionResponse(
            success=False,
            message="Channel was not recording.",
            channel_id=channel_id,
            status=ProcessStatus.STOPPED,
        )
    return ActionResponse(
        success=True,
        message="Recording stopped.",
        channel_id=channel_id,
        status=ProcessStatus.STOPPED,
    )


@router.post("/{channel_id}/restart", response_model=ActionResponse)
def restart_channel(channel_id: str, db: DbDep, _: AdminDep):
    """Stop then restart recording for a channel."""
    ch = _get_channel_or_404(channel_id, db)
    if not ch.enabled:
        raise HTTPException(status_code=400, detail=f"Channel '{channel_id}' is disabled.")
    pm = get_process_manager()
    config = ChannelConfig.model_validate_json(ch.config_json)
    try:
        info = pm.restart(channel_id, config, db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to restart FFmpeg: {exc}")
    return ActionResponse(
        success=True,
        message=f"Recording restarted (PID {info.pid}).",
        channel_id=channel_id,
        status=ProcessStatus.RUNNING,
    )


@router.get("/{channel_id}/logs", response_model=LogsResponse)
def get_logs(
    channel_id: str,
    db: DbDep,
    _: AdminDep,
    lines: int = Query(default=100, ge=1, le=5000),
):
    """Tail the FFmpeg stderr log for a channel."""
    _get_channel_or_404(channel_id, db)
    pm = get_process_manager()
    log_path = pm.get_log_path(channel_id)
    return LogsResponse(
        channel_id=channel_id,
        log_path=str(log_path) if log_path else None,
        lines=pm.get_log_tail(channel_id, lines=lines),
    )


@router.get("/{channel_id}/command", response_model=CommandPreviewResponse)
def preview_command(channel_id: str, db: DbDep, _: AdminDep):
    """
    Dry-run: return the exact FFmpeg command that would be executed for this channel.
    Useful for auditing and debugging without actually starting recording.
    """
    ch = _get_channel_or_404(channel_id, db)
    config = ChannelConfig.model_validate_json(ch.config_json)
    cmd = build_ffmpeg_command(config)
    return CommandPreviewResponse(
        channel_id=channel_id,
        command=cmd,
        command_str=format_command_for_log(cmd),
    )


@router.get("/{channel_id}/history", response_model=list[ProcessHistoryEntry])
def get_history(
    channel_id: str,
    db: DbDep,
    _: AdminDep,
    limit: int = Query(default=20, ge=1, le=200),
):
    """Recent process start/stop records for a channel."""
    _get_channel_or_404(channel_id, db)
    records = (
        db.query(ProcessRecord)
        .filter(ProcessRecord.channel_id == channel_id)
        .order_by(ProcessRecord.id.desc())
        .limit(limit)
        .all()
    )
    return [
        ProcessHistoryEntry(
            id=r.id,
            pid=r.pid,
            status=r.status,
            started_at=r.started_at,
            stopped_at=r.stopped_at,
            exit_code=r.exit_code,
            log_path=r.log_path,
            adopted=r.adopted,
        )
        for r in records
    ]
