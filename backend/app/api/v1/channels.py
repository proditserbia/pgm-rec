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

import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...db.models import Channel, ProcessRecord
from ...db.session import get_db
from ...models.schemas import (
    ActionResponse,
    ChannelConfig,
    ChannelDetailResponse,
    ChannelDiagnosticsResponse,
    ChannelStatusResponse,
    ChannelSummary,
    CommandPreviewResponse,
    ConfigReloadResponse,
    HealthStatus,
    LogsResponse,
    ProcessHistoryEntry,
    ProcessStatus,
)
from ...services.ffmpeg_builder import build_ffmpeg_command, format_command_for_log
from ...services.process_manager import get_process_manager, _tail_file
from ...utils import utc_now
from ...config.settings import get_settings
from .deps import AdminDep, AnyRoleDep

router = APIRouter(prefix="/channels", tags=["channels"])

DbDep = Annotated[Session, Depends(get_db)]

logger = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _load_channel_config(ch: Channel) -> ChannelConfig:
    """
    Load channel configuration honouring PGMREC_CHANNEL_CONFIG_MODE.

    - ``"db"`` (default): read from ``ch.config_json`` stored in the DB.
    - ``"json_override_db"``: same as ``"db"`` — JSON was already synced to DB
      at startup by ``_reconcile_channel_configs``.
    - ``"json"``: read directly from the JSON file on disk; falls back to the
      DB copy if the file is missing or cannot be parsed.
    """
    settings = get_settings()
    mode = settings.channel_config_mode

    if mode == "json":
        json_path = settings.channels_config_dir / f"{ch.id}.json"
        if json_path.is_file():
            try:
                return ChannelConfig.model_validate_json(
                    json_path.read_text(encoding="utf-8")
                )
            except Exception:
                pass  # fall back to DB copy below

    return ChannelConfig.model_validate_json(ch.config_json)

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
        uptime = (utc_now() - started_at).total_seconds()
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
    config = _load_channel_config(ch)
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
    config = _load_channel_config(ch)
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
    config = _load_channel_config(ch)
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
    config = _load_channel_config(ch)
    cmd = build_ffmpeg_command(config)
    return CommandPreviewResponse(
        channel_id=channel_id,
        command=cmd,
        command_str=format_command_for_log(cmd),
    )


@router.post("/{channel_id}/reload-config", response_model=ConfigReloadResponse)
def reload_channel_config(channel_id: str, db: DbDep, _: AdminDep):
    """
    Reload channel configuration from the JSON file on disk into the DB.

    Git pull changes JSON files only; the DB is not updated automatically.
    Call this endpoint after pulling new channel configs to apply them without
    restarting the server.

    - Reads `<channels_config_dir>/<channel_id>.json`
    - Validates the JSON using ChannelConfig
    - Replaces the DB channel's config_json, name, display_name, and enabled flag
    - Returns the effective (new) config and whether the config changed
    """
    import json

    settings = get_settings()
    json_path = settings.channels_config_dir / f"{channel_id}.json"
    if not json_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"No JSON config file found for channel '{channel_id}'. "
                f"Expected: {json_path}"
            ),
        )

    try:
        raw = json_path.read_text(encoding="utf-8")
        new_config = ChannelConfig.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Channel config file '{json_path.name}' failed validation: {exc}",
        )

    if new_config.id != channel_id:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Config file id='{new_config.id}' does not match "
                f"channel_id='{channel_id}'."
            ),
        )

    ch = _get_channel_or_404(channel_id, db)
    new_json = new_config.model_dump_json()
    config_changed = ch.config_json != new_json

    ch.config_json = new_json
    ch.name = new_config.name
    ch.display_name = new_config.display_name
    ch.enabled = new_config.enabled
    db.commit()
    db.refresh(ch)

    if config_changed:
        logger.info(
            "Channel '%s' DB config reloaded from JSON (config changed).", channel_id
        )

    return ConfigReloadResponse(
        channel_id=channel_id,
        config_changed=config_changed,
        message=(
            f"Config reloaded from {json_path.name}."
            if config_changed
            else "Config unchanged — DB already matches JSON."
        ),
        config=new_config,
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


@router.get("/{channel_id}/diagnostics", response_model=ChannelDiagnosticsResponse)
def get_channel_diagnostics(channel_id: str, db: DbDep, _: AdminDep):
    """
    Deep diagnostics for a channel — Phase 9.

    Returns FFmpeg command, capture config, last 100 lines of recording stderr,
    latest segment on disk, and a device-listing hint for Decklink/dshow setups.

    Intended for in-browser admin troubleshooting of black video, device errors,
    or SDI signal issues.  Admin only.
    """
    from ...config.settings import resolve_channel_path
    from ...services.ffmpeg_builder import _build_input_specifier

    ch = _get_channel_or_404(channel_id, db)
    pm = get_process_manager()
    config = _load_channel_config(ch)

    cmd = build_ffmpeg_command(config)
    cmd_str = format_command_for_log(cmd)

    # Latest segment in record_dir
    record_dir = resolve_channel_path(config.paths.record_dir)
    latest_path: str | None = None
    latest_size: int | None = None
    latest_mtime: datetime | None = None
    try:
        mp4s = sorted(record_dir.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
        if mp4s:
            f = mp4s[0]
            st = f.stat()
            latest_path = str(f)
            latest_size = st.st_size
            latest_mtime = datetime.utcfromtimestamp(st.st_mtime)
    except OSError:
        pass

    # Stderr tail from current or most recent log
    log_path = pm.get_log_path(channel_id)
    stderr_tail = _tail_file(log_path, lines=100) if log_path else []

    # Phase 17 — for from_udp mode, build an ffplay diagnostic hint.
    ffplay_hint: str | None = None
    if getattr(config.preview, "input_mode", "") == "from_udp":
        rpo = config.recording_preview_output
        if rpo:
            udp_url = rpo.listen_url if rpo.listen_url else rpo.url
            ffplay_hint = (
                f'ffplay "{udp_url}"  '
                "(run on the recording machine to verify the UDP stream)"
            )

    return ChannelDiagnosticsResponse(
        channel_id=channel_id,
        ffmpeg_command=cmd_str,
        ffmpeg_command_list=cmd,
        device_type=config.capture.device_type,
        input_specifier=_build_input_specifier(config),
        resolution=config.capture.resolution,
        framerate=config.capture.framerate,
        record_dir=str(record_dir),
        latest_segment_path=latest_path,
        latest_segment_size_bytes=latest_size,
        latest_segment_mtime=latest_mtime,
        stderr_tail=stderr_tail,
        ffplay_hint=ffplay_hint,
    )
