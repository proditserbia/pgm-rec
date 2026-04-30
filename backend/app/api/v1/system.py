"""
System configuration API — Phase 8.

Endpoint:
  GET /api/v1/system/config   Admin-only sanitized effective configuration.
"""
from __future__ import annotations

import re

from fastapi import APIRouter

from ...config.settings import get_loaded_env_file, get_settings
from ...models.schemas import SystemConfigResponse
from .deps import AdminDep

router = APIRouter(tags=["system"])


def _mask_db_url(url: str) -> str:
    """Replace the password component of a database URL with '****'."""
    return re.sub(r"://([^:@]+):([^@]+)@", r"://\1:****@", url)


@router.get("/system/config", response_model=SystemConfigResponse)
def get_system_config(_: AdminDep) -> SystemConfigResponse:
    """
    Return sanitized effective configuration — admin only.

    The database URL password is masked.  JWT secret and admin password
    are never included in the response.
    """
    settings = get_settings()
    env_file = get_loaded_env_file()

    cors = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    ffmpeg = settings.ffmpeg_path_override or "(per-channel config)"

    return SystemConfigResponse(
        env_file=str(env_file) if env_file else None,
        data_dir=str(settings.data_dir),
        ffmpeg_path=ffmpeg,
        ffprobe_path=settings.ffprobe_path,
        database_url=_mask_db_url(settings.database_url),
        exports_dir=str(settings.exports_dir),
        preview_dir=str(settings.preview_dir),
        manifests_dir=str(settings.manifests_dir),
        cors_origins=cors,
        host=settings.host,
        port=settings.port,
        recording_root=str(settings.recording_root) if settings.recording_root else None,
    )
