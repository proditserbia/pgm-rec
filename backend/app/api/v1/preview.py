"""
Preview API — v1.

Endpoints (all nested under /api/v1/channels/{channel_id}):

  POST /preview/start    — start the preview process
  POST /preview/stop     — stop the preview process
  GET  /preview/status   — process status + stream URL
  GET  /preview/stream   — MJPEG streaming endpoint (browser-viewable)

Preview architecture
────────────────────
Each channel runs a separate, low-resource FFmpeg process that:
- reads from the same dshow/v4l2 input device as recording
- scales video down to a small resolution (e.g. 320×180)
- reduces fps (e.g. 5 fps)
- disables audio
- writes raw MJPEG frames to stdout (pipe:1)

A background daemon thread (_FrameReader) continuously reads the frames
from the pipe and stores the latest complete JPEG in memory.

GET /preview/stream returns a StreamingResponse with
``Content-Type: multipart/x-mixed-replace; boundary=pgmframe``,
which is directly viewable in modern browsers via an <img> tag:

  <img src="http://localhost:8000/api/v1/channels/rts1/preview/stream">

The polling interval inside the async generator (0.1 s → 10 fps maximum)
naturally throttles clients; the actual frame rate is controlled by FFmpeg.

Isolation
────────────────────
- Preview failure never affects recording.
- Stopping preview never stops recording.
- PreviewManager is entirely separate from ProcessManager.
"""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...db.models import Channel
from ...db.session import get_db
from ...models.schemas import ActionResponse, ChannelConfig, PreviewHealth, PreviewStatusResponse, ProcessStatus
from ...services.preview_manager import get_preview_manager

router = APIRouter(tags=["preview"])

DbDep = Annotated[Session, Depends(get_db)]

# Boundary string used for multipart MJPEG stream
_MJPEG_BOUNDARY = "pgmframe"
_MJPEG_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_channel_or_404(channel_id: str, db: Session) -> Channel:
    ch = db.query(Channel).filter(Channel.id == channel_id).first()
    if ch is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found.")
    return ch


def _preview_status_response(channel_id: str, channel_name: str) -> PreviewStatusResponse:
    pm = get_preview_manager()
    status = pm.preview_status(channel_id)
    return PreviewStatusResponse(
        channel_id=channel_id,
        running=status["running"],
        pid=status["pid"],
        started_at=status["started_at"],
        stream_url=status["stream_url"],
        health=status["health"],
    )


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/channels/{channel_id}/preview/start", response_model=PreviewStatusResponse)
def start_preview(channel_id: str, db: DbDep):
    """
    Start the preview process for a channel.

    The preview is completely isolated from recording.
    Returns the stream URL once the process has been launched.
    """
    ch = _get_channel_or_404(channel_id, db)
    pm = get_preview_manager()
    config = ChannelConfig.model_validate_json(ch.config_json)
    try:
        info = pm.start_preview(channel_id, config)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to start preview: {exc}")
    status = pm.preview_status(channel_id)
    return PreviewStatusResponse(
        channel_id=channel_id,
        running=True,
        pid=info.pid,
        started_at=info.started_at,
        stream_url=status["stream_url"],
        health=info.health,
    )


@router.post("/channels/{channel_id}/preview/stop", response_model=PreviewStatusResponse)
def stop_preview(channel_id: str, db: DbDep):
    """Stop the preview process for a channel."""
    _get_channel_or_404(channel_id, db)
    pm = get_preview_manager()
    pm.stop_preview(channel_id)
    return PreviewStatusResponse(
        channel_id=channel_id,
        running=False,
        health=PreviewHealth.UNKNOWN,
    )


@router.get("/channels/{channel_id}/preview/status", response_model=PreviewStatusResponse)
def get_preview_status(channel_id: str, db: DbDep):
    """Live preview process status and stream URL."""
    ch = _get_channel_or_404(channel_id, db)
    return _preview_status_response(channel_id, ch.name)


@router.get("/channels/{channel_id}/preview/stream")
async def stream_preview(channel_id: str, db: DbDep):
    """
    MJPEG streaming endpoint — browser-viewable.

    Returns a ``multipart/x-mixed-replace`` stream that browsers can render
    directly in an ``<img>`` tag:

      <img src="/api/v1/channels/rts1/preview/stream">

    The generator polls for the latest JPEG frame every 100 ms.  If the
    preview process is not running, a 503 is returned immediately.
    When the client disconnects, the generator exits cleanly.
    """
    _get_channel_or_404(channel_id, db)
    pm = get_preview_manager()

    if not pm.is_running(channel_id):
        raise HTTPException(
            status_code=503,
            detail=f"Preview for channel '{channel_id}' is not running. "
                   f"Start it first via POST /channels/{channel_id}/preview/start",
        )

    async def _generate():
        """Async generator that yields MJPEG multipart frames."""
        while True:
            frame = pm.get_latest_frame(channel_id)
            if frame:
                yield (
                    f"--{_MJPEG_BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode()
                yield frame
                yield b"\r\n"
            # Yield control and wait ~100 ms before next frame
            await asyncio.sleep(0.1)

    return StreamingResponse(_generate(), media_type=_MJPEG_CONTENT_TYPE)
