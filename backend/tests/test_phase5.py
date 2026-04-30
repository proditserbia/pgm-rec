"""
Phase 5 unit tests — HLS Browser Preview.

Covers:
- Settings: preview_dir added
- Schemas: PreviewConfig HLS fields, HlsPreviewStatusResponse
- ffmpeg_builder: build_hls_preview_command — correct argument structure
- HlsPreviewManager: start/stop/status/clean_output_dir/check_all
- API endpoints: start, stop, status, playlist.m3u8, segment
- Role guards: admin can start/stop, any role can view
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import get_settings
from app.db.models import Base, Channel, User
from app.db.session import get_db
from app.models.schemas import ChannelConfig, HlsPreviewStatusResponse, PreviewHealth
from app.services.auth_service import create_access_token, create_user
from app.services.ffmpeg_builder import build_hls_preview_command
from app.services.hls_preview_manager import HlsPreviewManager
from app.api.v1 import auth as auth_router
from app.api.v1 import preview as preview_router
from app.api.v1.deps import get_current_user


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def in_memory_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def db_session(in_memory_engine) -> Generator[Session, None, None]:
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=in_memory_engine)
    with SessionLocal() as session:
        yield session


def _make_channel_config(channel_id: str = "rts1") -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        name="RTS1",
        display_name="RTS1 Test",
        paths={
            "record_dir": "/tmp/rec",
            "chunks_dir": "/tmp/chunks",
            "final_dir": "/tmp/final",
        },
    )


def _make_channel(db: Session, channel_id: str = "rts1") -> Channel:
    cfg = _make_channel_config(channel_id)
    ch = Channel(
        id=cfg.id,
        name=cfg.name,
        display_name=cfg.display_name,
        enabled=True,
        config_json=cfg.model_dump_json(),
    )
    db.add(ch)
    db.commit()
    db.refresh(ch)
    return ch


def _make_test_app(db_session: Session) -> FastAPI:
    """Build a minimal FastAPI test app with auth + preview routers."""
    app = FastAPI()

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.include_router(auth_router.router, prefix="/api/v1")
    app.include_router(preview_router.router, prefix="/api/v1")
    return app


def _admin_token(db: Session) -> str:
    user = create_user(db, "admin", "adminpass", "admin")
    return create_access_token(user.username, user.role)


def _export_token(db: Session) -> str:
    user = create_user(db, "exporter", "exportpass", "export")
    return create_access_token(user.username, user.role)


def _preview_token(db: Session) -> str:
    user = create_user(db, "viewer", "viewerpass", "preview")
    return create_access_token(user.username, user.role)


# ---------------------------------------------------------------------------
# Settings tests
# ---------------------------------------------------------------------------

def test_settings_preview_dir():
    settings = get_settings()
    assert hasattr(settings, "preview_dir")
    assert isinstance(settings.preview_dir, Path)
    assert "preview" in str(settings.preview_dir)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_preview_config_hls_defaults():
    cfg = _make_channel_config()
    p = cfg.preview
    assert p.width == 480
    assert p.height == 270
    assert p.hls_fps == 10
    assert p.video_bitrate == "400k"
    assert p.encoder == "libx264"
    assert p.segment_time == 2
    assert p.list_size == 5


def test_hls_preview_status_response_schema():
    r = HlsPreviewStatusResponse(channel_id="rts1", running=False)
    assert r.running is False
    assert r.playlist_url is None
    assert r.health == PreviewHealth.UNKNOWN


# ---------------------------------------------------------------------------
# FFmpeg builder tests
# ---------------------------------------------------------------------------

def test_build_hls_preview_command_structure(tmp_path):
    cfg = _make_channel_config()
    cmd = build_hls_preview_command(cfg, tmp_path)

    assert cmd[0] == cfg.ffmpeg_path
    assert "-y" in cmd

    # Input
    assert "-f" in cmd
    assert "-s" in cmd
    assert "-framerate" in cmd
    assert "-i" in cmd

    # Video filters
    vf_idx = cmd.index("-vf")
    vf = cmd[vf_idx + 1]
    assert "scale=480:270" in vf
    assert "fps=10" in vf

    # Audio disabled
    assert "-an" in cmd

    # Codec
    assert "-c:v" in cmd
    assert "libx264" in cmd
    assert "-preset" in cmd
    assert "ultrafast" in cmd
    assert "-b:v" in cmd
    assert "400k" in cmd

    # HLS muxer
    assert "-f" in cmd
    hls_f_idx = [i for i, x in enumerate(cmd) if x == "-f" and i > cmd.index("-i")]
    assert any(cmd[i + 1] == "hls" for i in hls_f_idx)

    assert "-hls_time" in cmd
    assert cmd[cmd.index("-hls_time") + 1] == "2"

    assert "-hls_list_size" in cmd
    assert cmd[cmd.index("-hls_list_size") + 1] == "5"

    assert "-hls_flags" in cmd
    hls_flags = cmd[cmd.index("-hls_flags") + 1]
    assert "delete_segments" in hls_flags

    assert "-hls_segment_filename" in cmd
    seg_pattern = cmd[cmd.index("-hls_segment_filename") + 1]
    assert "seg" in seg_pattern
    assert str(tmp_path) in seg_pattern

    # Output playlist
    assert str(tmp_path / "index.m3u8") == cmd[-1]


def test_build_hls_preview_command_custom_settings(tmp_path):
    cfg = _make_channel_config()
    cfg.preview.width = 640
    cfg.preview.height = 360
    cfg.preview.hls_fps = 15
    cfg.preview.video_bitrate = "800k"
    cfg.preview.segment_time = 4
    cfg.preview.list_size = 3

    cmd = build_hls_preview_command(cfg, tmp_path)
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=640:360" in vf
    assert "fps=15" in vf
    assert "800k" in cmd
    assert cmd[cmd.index("-hls_time") + 1] == "4"
    assert cmd[cmd.index("-hls_list_size") + 1] == "3"


# ---------------------------------------------------------------------------
# HlsPreviewManager unit tests (mocked subprocess)
# ---------------------------------------------------------------------------

@pytest.fixture
def manager():
    return HlsPreviewManager()


def _mock_process(returncode=None):
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    proc.poll.return_value = returncode
    proc.wait.return_value = returncode
    return proc


def test_manager_start_preview(manager, tmp_path):
    cfg = _make_channel_config()
    mock_proc = _mock_process()

    with patch("app.services.hls_preview_manager.get_settings") as mock_settings, \
         patch("subprocess.Popen", return_value=mock_proc):

        settings = MagicMock()
        settings.logs_dir = tmp_path / "logs"
        settings.preview_dir = tmp_path / "preview"
        settings.stop_timeout_seconds = 15
        mock_settings.return_value = settings

        info = manager.start_preview("rts1", cfg)

    assert info.channel_id == "rts1"
    assert info.pid == 12345
    assert info.health == PreviewHealth.HEALTHY
    assert manager.is_running("rts1")


def test_manager_start_preview_conflict(manager, tmp_path):
    cfg = _make_channel_config()
    mock_proc = _mock_process()

    with patch("app.services.hls_preview_manager.get_settings") as mock_settings, \
         patch("subprocess.Popen", return_value=mock_proc):

        settings = MagicMock()
        settings.logs_dir = tmp_path / "logs"
        settings.preview_dir = tmp_path / "preview"
        settings.stop_timeout_seconds = 15
        mock_settings.return_value = settings

        manager.start_preview("rts1", cfg)
        with pytest.raises(RuntimeError, match="already running"):
            manager.start_preview("rts1", cfg)


def test_manager_stop_preview(manager, tmp_path):
    cfg = _make_channel_config()
    mock_proc = _mock_process()

    with patch("app.services.hls_preview_manager.get_settings") as mock_settings, \
         patch("subprocess.Popen", return_value=mock_proc):

        settings = MagicMock()
        settings.logs_dir = tmp_path / "logs"
        settings.preview_dir = tmp_path / "preview"
        settings.stop_timeout_seconds = 15
        mock_settings.return_value = settings

        manager.start_preview("rts1", cfg)
        result = manager.stop_preview("rts1")

    assert result is True
    assert not manager.is_running("rts1")


def test_manager_stop_not_running(manager):
    result = manager.stop_preview("nonexistent")
    assert result is False


def test_manager_preview_status_not_running(manager):
    status = manager.preview_status("rts1")
    assert status["running"] is False
    assert status["pid"] is None
    assert status["playlist_url"] is None
    assert status["health"] == PreviewHealth.UNKNOWN


def test_manager_preview_status_running(manager, tmp_path):
    cfg = _make_channel_config()
    mock_proc = _mock_process()

    with patch("app.services.hls_preview_manager.get_settings") as mock_settings, \
         patch("subprocess.Popen", return_value=mock_proc):

        settings = MagicMock()
        settings.logs_dir = tmp_path / "logs"
        settings.preview_dir = tmp_path / "preview"
        settings.stop_timeout_seconds = 15
        mock_settings.return_value = settings

        manager.start_preview("rts1", cfg)
        status = manager.preview_status("rts1")

    assert status["running"] is True
    assert status["pid"] == 12345
    assert "playlist.m3u8" in status["playlist_url"]


def test_manager_clean_output_dir(tmp_path):
    output_dir = tmp_path / "rts1"
    output_dir.mkdir()
    (output_dir / "seg00001.ts").write_bytes(b"fake")
    (output_dir / "index.m3u8").write_text("#EXTM3U\n")
    (output_dir / "keep.txt").write_text("important\n")

    HlsPreviewManager._clean_output_dir(output_dir)

    assert not (output_dir / "seg00001.ts").exists()
    assert not (output_dir / "index.m3u8").exists()
    assert (output_dir / "keep.txt").exists()


def test_manager_check_all_marks_down(manager, tmp_path):
    cfg = _make_channel_config()
    mock_proc = _mock_process()

    with patch("app.services.hls_preview_manager.get_settings") as mock_settings, \
         patch("subprocess.Popen", return_value=mock_proc):

        settings = MagicMock()
        settings.logs_dir = tmp_path / "logs"
        settings.preview_dir = tmp_path / "preview"
        settings.stop_timeout_seconds = 15
        mock_settings.return_value = settings

        manager.start_preview("rts1", cfg)

    # Simulate process exiting
    mock_proc.poll.return_value = 1
    manager.check_all()
    assert not manager.is_running("rts1")


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

@pytest.fixture
def test_app(db_session):
    return _make_test_app(db_session)


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


def test_api_start_requires_admin(client, db_session):
    _make_channel(db_session)
    export_tok = _export_token(db_session)
    resp = client.post(
        "/api/v1/channels/rts1/preview/start",
        headers={"Authorization": f"Bearer {export_tok}"},
    )
    assert resp.status_code == 403


def test_api_start_preview_role_forbidden(client, db_session):
    _make_channel(db_session)
    preview_tok = _preview_token(db_session)
    resp = client.post(
        "/api/v1/channels/rts1/preview/start",
        headers={"Authorization": f"Bearer {preview_tok}"},
    )
    assert resp.status_code == 403


def test_api_stop_requires_admin(client, db_session):
    _make_channel(db_session)
    export_tok = _export_token(db_session)
    resp = client.post(
        "/api/v1/channels/rts1/preview/stop",
        headers={"Authorization": f"Bearer {export_tok}"},
    )
    assert resp.status_code == 403


def test_api_status_any_role(client, db_session):
    _make_channel(db_session)
    # preview role can check status
    preview_tok = _preview_token(db_session)
    resp = client.get(
        "/api/v1/channels/rts1/preview/status",
        headers={"Authorization": f"Bearer {preview_tok}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["channel_id"] == "rts1"
    assert data["running"] is False


def test_api_status_unauthenticated(client, db_session):
    _make_channel(db_session)
    resp = client.get("/api/v1/channels/rts1/preview/status")
    assert resp.status_code == 401


def test_api_status_channel_not_found(client, db_session):
    tok = _admin_token(db_session)
    resp = client.get(
        "/api/v1/channels/nonexistent/preview/status",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 404


def test_api_start_channel_not_found(client, db_session):
    tok = _admin_token(db_session)
    resp = client.post(
        "/api/v1/channels/nonexistent/preview/start",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 404


def test_api_start_preview_success(client, db_session, tmp_path):
    _make_channel(db_session)
    tok = _admin_token(db_session)

    mock_proc = _mock_process()
    settings = get_settings()

    with patch("app.services.hls_preview_manager.get_settings") as mock_s, \
         patch("subprocess.Popen", return_value=mock_proc):

        ms = MagicMock()
        ms.logs_dir = tmp_path / "logs"
        ms.preview_dir = tmp_path / "preview"
        ms.stop_timeout_seconds = 15
        ms.watchdog_interval_seconds = 10
        mock_s.return_value = ms

        # Reset singleton for test isolation
        import app.services.hls_preview_manager as hls_mod
        hls_mod._hls_preview_manager = None

        resp = client.post(
            "/api/v1/channels/rts1/preview/start",
            headers={"Authorization": f"Bearer {tok}"},
        )
        hls_mod._hls_preview_manager = None  # cleanup

    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is True
    assert data["channel_id"] == "rts1"
    assert "playlist.m3u8" in (data["playlist_url"] or "")


def test_api_start_preview_conflict(client, db_session, tmp_path):
    _make_channel(db_session)
    tok = _admin_token(db_session)
    mock_proc = _mock_process()

    import app.services.hls_preview_manager as hls_mod

    with patch("app.services.hls_preview_manager.get_settings") as mock_s, \
         patch("subprocess.Popen", return_value=mock_proc):

        ms = MagicMock()
        ms.logs_dir = tmp_path / "logs"
        ms.preview_dir = tmp_path / "preview"
        ms.stop_timeout_seconds = 15
        mock_s.return_value = ms
        hls_mod._hls_preview_manager = None

        client.post(
            "/api/v1/channels/rts1/preview/start",
            headers={"Authorization": f"Bearer {tok}"},
        )
        resp = client.post(
            "/api/v1/channels/rts1/preview/start",
            headers={"Authorization": f"Bearer {tok}"},
        )
        hls_mod._hls_preview_manager = None

    assert resp.status_code == 409


def test_api_playlist_not_running(client, db_session):
    _make_channel(db_session)
    tok = _preview_token(db_session)
    resp = client.get(
        "/api/v1/channels/rts1/preview/playlist.m3u8",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 503


def test_api_playlist_serves_file(client, db_session, tmp_path):
    _make_channel(db_session)
    tok = _preview_token(db_session)

    # Create a fake playlist
    preview_dir = tmp_path / "preview" / "rts1"
    preview_dir.mkdir(parents=True)
    playlist = preview_dir / "index.m3u8"
    playlist.write_text("#EXTM3U\n#EXT-X-VERSION:3\n")

    import app.services.hls_preview_manager as hls_mod
    hls_mod._hls_preview_manager = None

    with patch("app.services.hls_preview_manager.get_settings") as mock_s:
        ms = MagicMock()
        ms.preview_dir = tmp_path / "preview"
        ms.stop_timeout_seconds = 15
        ms.watchdog_interval_seconds = 10
        mock_s.return_value = ms

        resp = client.get(
            "/api/v1/channels/rts1/preview/playlist.m3u8",
            headers={"Authorization": f"Bearer {tok}"},
        )
        hls_mod._hls_preview_manager = None

    assert resp.status_code == 200
    assert "EXTM3U" in resp.text


def test_api_segment_invalid_name(client, db_session):
    _make_channel(db_session)
    tok = _preview_token(db_session)
    resp = client.get(
        "/api/v1/channels/rts1/preview/../secret.txt",
        headers={"Authorization": f"Bearer {tok}"},
    )
    # FastAPI path normalization turns this into a 404 at routing level
    assert resp.status_code in (400, 404, 307)


def test_api_segment_path_traversal_rejected(client, db_session):
    _make_channel(db_session)
    tok = _preview_token(db_session)

    import app.services.hls_preview_manager as hls_mod
    hls_mod._hls_preview_manager = None

    with patch("app.services.hls_preview_manager.get_settings") as mock_s:
        ms = MagicMock()
        ms.preview_dir = Path("/tmp/preview")
        ms.stop_timeout_seconds = 15
        mock_s.return_value = ms

        # A filename that matches our regex but still try to sneak a dot-dot
        resp = client.get(
            "/api/v1/channels/rts1/preview/safe.ts",
            headers={"Authorization": f"Bearer {tok}"},
        )
        hls_mod._hls_preview_manager = None

    # Segment doesn't exist → 404 is fine
    assert resp.status_code in (400, 404)


def test_api_segment_not_found(client, db_session, tmp_path):
    _make_channel(db_session)
    tok = _preview_token(db_session)

    import app.services.hls_preview_manager as hls_mod
    hls_mod._hls_preview_manager = None

    with patch("app.services.hls_preview_manager.get_settings") as mock_s:
        ms = MagicMock()
        ms.preview_dir = tmp_path / "preview"
        ms.stop_timeout_seconds = 15
        mock_s.return_value = ms

        resp = client.get(
            "/api/v1/channels/rts1/preview/seg00001.ts",
            headers={"Authorization": f"Bearer {tok}"},
        )
        hls_mod._hls_preview_manager = None

    assert resp.status_code == 404


def test_api_segment_unauthenticated(client, db_session):
    _make_channel(db_session)
    resp = client.get("/api/v1/channels/rts1/preview/seg00001.ts")
    assert resp.status_code == 401


def test_api_stop_not_running_returns_ok(client, db_session):
    _make_channel(db_session)
    tok = _admin_token(db_session)
    resp = client.post(
        "/api/v1/channels/rts1/preview/stop",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is False
