"""
Preview API — v1 (Phase 5: HLS).

Endpoints (all nested under /api/v1/channels/{channel_id}):

  POST /preview/start          — start the HLS preview process  (admin only)
  POST /preview/stop           — stop the HLS preview process   (admin only)
  GET  /preview/status         — process status + playlist URL  (any role)
  GET  /preview/playlist.m3u8  — serve HLS playlist             (any role)
  GET  /preview/{segment}      — serve HLS .ts segment          (any role)

HLS preview architecture (Phase 5)
────────────────────────────────────
Each channel runs a separate, low-resource FFmpeg process that:
- reads from the same capture device as recording
- scales video down (default 480×270) and reduces fps (default 10)
- disables audio
- writes HLS output: index.m3u8 + seg*.ts files to
  data/preview/{channel_id}/

The playlist and segments are served via FastAPI FileResponse endpoints
protected by JWT authentication (any authenticated role can view).

hls.js (frontend) fetches the playlist via XHR with a Bearer token
injected by the player component, so auth works end-to-end.

Isolation
────────────────────────────────────
- HLS preview failure never affects recording.
- Stopping HLS preview never stops recording.
- HlsPreviewManager is entirely separate from ProcessManager.

Legacy MJPEG stream endpoint (Phase 2 /preview/stream) is kept for
backward compatibility but marked as deprecated.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ...config.settings import get_settings
from ...db.models import Channel
from ...db.session import get_db
from ...models.schemas import (
    ActionResponse,
    ChannelConfig,
    HlsPreviewStatusResponse,
    PreviewHealth,
    PreviewStatusResponse,
    ProcessStatus,
)
from ...services.hls_preview_manager import get_hls_preview_manager
from ...api.v1.deps import AdminDep, AnyRoleDep

router = APIRouter(tags=["preview"])

DbDep = Annotated[Session, Depends(get_db)]

# Allowed segment filename pattern — prevents path traversal
_SEGMENT_RE = re.compile(r'^[\w\-]+\.ts$')


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_channel_or_404(channel_id: str, db: Session) -> Channel:
    ch = db.query(Channel).filter(Channel.id == channel_id).first()
    if ch is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found.")
    return ch


def _hls_status_response(channel_id: str) -> HlsPreviewStatusResponse:
    pm = get_hls_preview_manager()
    status = pm.preview_status(channel_id)
    return HlsPreviewStatusResponse(
        channel_id=channel_id,
        running=status["running"],
        pid=status["pid"],
        started_at=status["started_at"],
        playlist_url=status["playlist_url"],
        health=status["health"],
    )


# ─── HLS routes (Phase 5) ─────────────────────────────────────────────────────

@router.post(
    "/channels/{channel_id}/preview/start",
    response_model=HlsPreviewStatusResponse,
)
def start_preview(channel_id: str, db: DbDep, _: AdminDep):
    """
    Start the HLS preview process for a channel.  Admin only.

    The preview is completely isolated from recording.
    Returns the playlist URL once the process has been launched.
    """
    ch = _get_channel_or_404(channel_id, db)
    pm = get_hls_preview_manager()
    config = ChannelConfig.model_validate_json(ch.config_json)
    try:
        info = pm.start_preview(channel_id, config)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to start preview: {exc}")
    status = pm.preview_status(channel_id)
    return HlsPreviewStatusResponse(
        channel_id=channel_id,
        running=True,
        pid=info.pid,
        started_at=info.started_at,
        playlist_url=status["playlist_url"],
        health=info.health,
    )


@router.post(
    "/channels/{channel_id}/preview/stop",
    response_model=HlsPreviewStatusResponse,
)
def stop_preview(channel_id: str, db: DbDep, _: AdminDep):
    """Stop the HLS preview process for a channel.  Admin only."""
    _get_channel_or_404(channel_id, db)
    pm = get_hls_preview_manager()
    pm.stop_preview(channel_id)
    return HlsPreviewStatusResponse(
        channel_id=channel_id,
        running=False,
        health=PreviewHealth.UNKNOWN,
    )


@router.get(
    "/channels/{channel_id}/preview/status",
    response_model=HlsPreviewStatusResponse,
)
def get_preview_status(channel_id: str, db: DbDep, _: AnyRoleDep):
    """HLS preview process status and playlist URL.  Any authenticated role."""
    _get_channel_or_404(channel_id, db)
    return _hls_status_response(channel_id)


@router.get("/channels/{channel_id}/preview/playlist.m3u8")
def get_hls_playlist(channel_id: str, db: DbDep, _: AnyRoleDep):
    """
    Serve the HLS playlist file.  Any authenticated role.

    Returns 503 if the preview process is not running or the playlist
    has not been created yet.
    """
    _get_channel_or_404(channel_id, db)
    pm = get_hls_preview_manager()
    output_dir = pm.get_output_dir(channel_id)
    playlist = output_dir / "index.m3u8"
    if not playlist.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                f"HLS playlist for channel '{channel_id}' is not available. "
                f"Start preview first via POST /channels/{channel_id}/preview/start"
            ),
        )
    return FileResponse(
        path=str(playlist),
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/channels/{channel_id}/preview/{segment}")
def get_hls_segment(channel_id: str, segment: str, db: DbDep, _: AnyRoleDep):
    """
    Serve a single HLS .ts segment file.  Any authenticated role.

    The segment name must match ``^[\\w\\-]+\\.ts$`` to prevent path traversal.
    Returns 404 if the segment does not exist.
    """
    _get_channel_or_404(channel_id, db)

    if not _SEGMENT_RE.match(segment):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid segment name: '{segment}'.",
        )

    pm = get_hls_preview_manager()
    output_dir = pm.get_output_dir(channel_id)
    segment_path = output_dir / segment

    # Extra safety: resolve and verify the path stays inside output_dir
    try:
        resolved = segment_path.resolve()
        output_dir_resolved = output_dir.resolve()
        if not str(resolved).startswith(str(output_dir_resolved)):
            raise HTTPException(status_code=400, detail="Invalid segment path.")
    except OSError:
        raise HTTPException(status_code=404, detail="Segment not found.")

    if not segment_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Segment '{segment}' not found for channel '{channel_id}'.",
        )
    return FileResponse(
        path=str(segment_path),
        media_type="video/MP2T",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )

