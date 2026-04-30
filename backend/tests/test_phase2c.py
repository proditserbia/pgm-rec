"""
Phase 2C unit tests — Export Hardening & Verification.

Covers:
- Settings: new Phase 2C fields (export_retention_days, max_export_duration_seconds,
                                  export_duration_tolerance_seconds)
- DB model: ExportJob.actual_duration_seconds new column
- Schema: ExportJobResponse.actual_duration_seconds
- export_service: verify_export_output() — file missing, empty, ffprobe ok,
                  duration mismatch, ffprobe unavailable
- export_retention: _delete_old_files, _prune_empty_dirs, _run_export_retention_sync
- API validation:
  - _validate_export_request: in_time >= out_time → 400
  - _validate_export_request: future date → 400
  - _validate_export_request: duration > max → 400
  - _validate_export_request: valid request → no error
- API: GET /exports/{id}/logs — found, missing file, no log yet
- API: GET /exports/{id}/download — completed ok, non-completed → 409,
        file missing → 404
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
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
from app.db.models import Base, Channel, ExportJob
from app.db.session import get_db
from app.models.schemas import (
    ChannelConfig,
    ExportJobRequest,
    ExportJobResponse,
    ExportJobStatus,
)
from app.services.export_retention import (
    _delete_old_files,
    _prune_empty_dirs,
    _run_export_retention_sync,
)
from app.services.export_service import (
    verify_export_output,
)
from app.api.v1.exports import _validate_export_request


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
        timezone="Europe/Belgrade",
        paths={
            "record_dir": "/tmp/record",
            "chunks_dir": "/tmp/chunks",
            "final_dir": "/tmp/final",
        },
    )


def _seed_channel(db: Session, channel_id: str = "rts1") -> Channel:
    config = _make_channel_config(channel_id)
    ch = Channel(
        id=channel_id,
        name="RTS1",
        display_name="RTS1 Test",
        enabled=True,
        config_json=config.model_dump_json(),
    )
    db.add(ch)
    db.commit()
    return ch


@pytest.fixture
def app_client(in_memory_engine):
    """Test client with overridden DB."""
    from fastapi import FastAPI
    from app.api.v1 import exports as exports_router_mod

    test_app = FastAPI()
    test_app.include_router(exports_router_mod.router, prefix="/api/v1")

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=in_memory_engine)

    def override_get_db():
        with SessionLocal() as db:
            yield db

    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(test_app) as client:
        with SessionLocal() as db:
            _seed_channel(db)
        yield client, SessionLocal


# ---------------------------------------------------------------------------
# Settings — Phase 2C fields
# ---------------------------------------------------------------------------

def test_settings_phase2c_defaults():
    s = get_settings()
    assert s.export_retention_days == 30
    assert s.max_export_duration_seconds == 7200
    assert s.export_duration_tolerance_seconds == 5.0


# ---------------------------------------------------------------------------
# DB model — actual_duration_seconds column
# ---------------------------------------------------------------------------

def test_export_job_actual_duration_default_null(db_session):
    job = ExportJob(
        channel_id="rts1", date="2026-04-01",
        in_time="14:00:00", out_time="14:05:00",
        status="queued", progress_percent=0.0, has_gaps=False,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    assert job.actual_duration_seconds is None


def test_export_job_actual_duration_set(db_session):
    job = ExportJob(
        channel_id="rts1", date="2026-04-01",
        in_time="14:00:00", out_time="14:05:00",
        status="completed", progress_percent=100.0, has_gaps=False,
        actual_duration_seconds=298.4,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    assert job.actual_duration_seconds == pytest.approx(298.4, abs=0.01)


# ---------------------------------------------------------------------------
# Schema — ExportJobResponse.actual_duration_seconds
# ---------------------------------------------------------------------------

def test_export_job_response_actual_duration_optional():
    resp = ExportJobResponse(
        id=1, channel_id="rts1", date="2026-04-01",
        in_time="14:00:00", out_time="14:05:00",
        status=ExportJobStatus.COMPLETED, progress_percent=100.0,
        has_gaps=False, created_at=datetime.now(timezone.utc),
    )
    assert resp.actual_duration_seconds is None


def test_export_job_response_actual_duration_set():
    resp = ExportJobResponse(
        id=1, channel_id="rts1", date="2026-04-01",
        in_time="14:00:00", out_time="14:05:00",
        status=ExportJobStatus.COMPLETED, progress_percent=100.0,
        has_gaps=False, actual_duration_seconds=300.0,
        created_at=datetime.now(timezone.utc),
    )
    assert resp.actual_duration_seconds == 300.0


# ---------------------------------------------------------------------------
# verify_export_output
# ---------------------------------------------------------------------------

def test_verify_output_file_missing(tmp_path):
    out = tmp_path / "nonexistent.mp4"
    ok, dur, err = verify_export_output(out, 300.0)
    assert ok is False
    assert dur is None
    assert "does not exist" in err


def test_verify_output_file_empty(tmp_path):
    out = tmp_path / "empty.mp4"
    out.write_bytes(b"")
    ok, dur, err = verify_export_output(out, 300.0)
    assert ok is False
    assert dur is None
    assert "empty" in err


def test_verify_output_ffprobe_ok(tmp_path):
    out = tmp_path / "good.mp4"
    out.write_bytes(b"\x00" * 1024)

    with patch("app.services.export_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="298.4\n", stderr="")
        ok, dur, err = verify_export_output(out, 300.0, ffprobe_path="ffprobe", tolerance=5.0)

    assert ok is True
    assert dur == pytest.approx(298.4, abs=0.01)
    assert err is None


def test_verify_output_duration_within_tolerance(tmp_path):
    out = tmp_path / "ok.mp4"
    out.write_bytes(b"\x00" * 1024)

    with patch("app.services.export_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="297.0\n", stderr="")
        ok, dur, err = verify_export_output(out, 300.0, tolerance=5.0)

    assert ok is True  # diff = 3.0 < 5.0


def test_verify_output_duration_exceeds_tolerance(tmp_path):
    out = tmp_path / "bad.mp4"
    out.write_bytes(b"\x00" * 1024)

    with patch("app.services.export_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="290.0\n", stderr="")
        ok, dur, err = verify_export_output(out, 300.0, tolerance=5.0)

    assert ok is False
    assert dur == pytest.approx(290.0, abs=0.01)
    assert "mismatch" in err


def test_verify_output_ffprobe_unavailable(tmp_path):
    """If ffprobe cannot run, treat as soft warning (ok=True, dur=None)."""
    out = tmp_path / "ok_no_probe.mp4"
    out.write_bytes(b"\x00" * 1024)

    with patch("app.services.export_service.subprocess.run", side_effect=OSError("not found")):
        ok, dur, err = verify_export_output(out, 300.0, ffprobe_path="ffprobe", tolerance=5.0)

    assert ok is True
    assert dur is None
    assert err is None


def test_verify_output_ffprobe_nonzero_exit(tmp_path):
    """ffprobe exits non-zero → treat as unavailable (soft warning)."""
    out = tmp_path / "ok_probe_fail.mp4"
    out.write_bytes(b"\x00" * 1024)

    with patch("app.services.export_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        ok, dur, err = verify_export_output(out, 300.0)

    assert ok is True
    assert dur is None


def test_verify_output_tolerance_zero_exact_match(tmp_path):
    out = tmp_path / "exact.mp4"
    out.write_bytes(b"\x00" * 1024)

    with patch("app.services.export_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="300.0\n", stderr="")
        ok, dur, err = verify_export_output(out, 300.0, tolerance=0.0)

    assert ok is True


def test_verify_output_tolerance_zero_tiny_diff(tmp_path):
    out = tmp_path / "tiny_diff.mp4"
    out.write_bytes(b"\x00" * 1024)

    with patch("app.services.export_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="300.1\n", stderr="")
        ok, dur, err = verify_export_output(out, 300.0, tolerance=0.0)

    assert ok is False


# ---------------------------------------------------------------------------
# API validation — _validate_export_request
# ---------------------------------------------------------------------------

def test_validate_valid_request():
    """No exception for a valid past-date request."""
    # Use a clearly past date
    req = ExportJobRequest(date="2026-01-01", in_time="14:00:00", out_time="14:05:00")
    _validate_export_request(req)  # should not raise


def test_validate_future_date():
    from fastapi import HTTPException
    req = ExportJobRequest(date="2099-01-01", in_time="14:00:00", out_time="14:05:00")
    with pytest.raises(HTTPException) as exc_info:
        _validate_export_request(req)
    assert exc_info.value.status_code == 400
    assert "future" in exc_info.value.detail.lower()


def test_validate_out_before_in():
    from fastapi import HTTPException
    req = ExportJobRequest(date="2026-01-01", in_time="15:00:00", out_time="14:00:00")
    with pytest.raises(HTTPException) as exc_info:
        _validate_export_request(req)
    assert exc_info.value.status_code == 400
    assert "out_time" in exc_info.value.detail.lower() or "after" in exc_info.value.detail.lower()


def test_validate_equal_times():
    from fastapi import HTTPException
    req = ExportJobRequest(date="2026-01-01", in_time="14:00:00", out_time="14:00:00")
    with pytest.raises(HTTPException) as exc_info:
        _validate_export_request(req)
    assert exc_info.value.status_code == 400


def test_validate_exceeds_max_duration():
    from fastapi import HTTPException
    from unittest.mock import patch
    from app.config.settings import Settings

    req = ExportJobRequest(date="2026-01-01", in_time="00:00:00", out_time="03:00:00")  # 3h
    # Max is 7200s (2h) by default — but let's patch to be sure
    with patch("app.api.v1.exports.get_settings") as mock_s:
        mock_settings = MagicMock()
        mock_settings.max_export_duration_seconds = 3600  # 1 hour
        mock_settings.export_duration_tolerance_seconds = 5.0
        mock_s.return_value = mock_settings
        with pytest.raises(HTTPException) as exc_info:
            _validate_export_request(req)
    assert exc_info.value.status_code == 400
    assert "maximum" in exc_info.value.detail.lower()


def test_validate_max_duration_zero_unlimited():
    """max_export_duration_seconds=0 means unlimited — no exception."""
    with patch("app.api.v1.exports.get_settings") as mock_s:
        mock_settings = MagicMock()
        mock_settings.max_export_duration_seconds = 0
        mock_s.return_value = mock_settings
        req = ExportJobRequest(date="2026-01-01", in_time="00:00:00", out_time="23:59:59")
        _validate_export_request(req)  # should not raise


def test_validate_malformed_date():
    from fastapi import HTTPException
    req = ExportJobRequest(date="not-a-date", in_time="14:00:00", out_time="14:05:00")
    with pytest.raises(HTTPException) as exc_info:
        _validate_export_request(req)
    assert exc_info.value.status_code == 400
    assert any(w in exc_info.value.detail.lower() for w in ("date", "format", "invalid"))


def test_validate_malformed_time():
    from fastapi import HTTPException
    req = ExportJobRequest(date="2026-01-01", in_time="bad", out_time="14:05:00")
    with pytest.raises(HTTPException) as exc_info:
        _validate_export_request(req)
    assert exc_info.value.status_code == 400


def test_validate_today_allowed():
    """A job for today (not future) should be accepted."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    req = ExportJobRequest(date=today, in_time="00:00:00", out_time="00:05:00")
    _validate_export_request(req)  # should not raise


# ---------------------------------------------------------------------------
# API: GET /exports/{id}/logs
# ---------------------------------------------------------------------------

def test_get_logs_not_found(app_client):
    client, _ = app_client
    resp = client.get("/api/v1/exports/99999/logs")
    assert resp.status_code == 404


def test_get_logs_no_log_yet(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="14:00:00", out_time="14:05:00",
            status="queued", progress_percent=0.0, has_gaps=False,
            log_path=None,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.get(f"/api/v1/exports/{job_id}/logs")
    assert resp.status_code == 404
    assert "No log file" in resp.text


def test_get_logs_file_missing_on_disk(app_client, tmp_path):
    client, SessionLocal = app_client
    nonexistent = str(tmp_path / "export_999.log")
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="14:00:00", out_time="14:05:00",
            status="failed", progress_percent=0.0, has_gaps=False,
            log_path=nonexistent,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.get(f"/api/v1/exports/{job_id}/logs")
    assert resp.status_code == 404
    assert "not found on disk" in resp.text


def test_get_logs_success(app_client, tmp_path):
    client, SessionLocal = app_client
    log_file = tmp_path / "export_1.log"
    log_file.write_text("ffmpeg stderr output line 1\nline 2\n", encoding="utf-8")

    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="14:00:00", out_time="14:05:00",
            status="completed", progress_percent=100.0, has_gaps=False,
            log_path=str(log_file),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.get(f"/api/v1/exports/{job_id}/logs")
    assert resp.status_code == 200
    assert "ffmpeg stderr output line 1" in resp.text
    assert resp.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# API: GET /exports/{id}/download
# ---------------------------------------------------------------------------

def test_download_not_found(app_client):
    client, _ = app_client
    resp = client.get("/api/v1/exports/99999/download")
    assert resp.status_code == 404


def test_download_not_completed(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="14:00:00", out_time="14:05:00",
            status="running", progress_percent=50.0, has_gaps=False,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.get(f"/api/v1/exports/{job_id}/download")
    assert resp.status_code == 409
    assert "not completed" in resp.text.lower() or "running" in resp.text.lower()


def test_download_queued_job(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="14:00:00", out_time="14:05:00",
            status="queued", progress_percent=0.0, has_gaps=False,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.get(f"/api/v1/exports/{job_id}/download")
    assert resp.status_code == 409


def test_download_file_missing_on_disk(app_client, tmp_path):
    client, SessionLocal = app_client
    missing = str(tmp_path / "nonexistent.mp4")
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="14:00:00", out_time="14:05:00",
            status="completed", progress_percent=100.0, has_gaps=False,
            output_path=missing,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.get(f"/api/v1/exports/{job_id}/download")
    assert resp.status_code == 404
    assert "no longer exists" in resp.text.lower() or "retention" in resp.text.lower()


def test_download_success(app_client, tmp_path):
    client, SessionLocal = app_client
    mp4_file = tmp_path / "out.mp4"
    mp4_file.write_bytes(b"\x00" * 512)  # minimal fake mp4

    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="14:00:00", out_time="14:05:00",
            status="completed", progress_percent=100.0, has_gaps=False,
            output_path=str(mp4_file),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.get(f"/api/v1/exports/{job_id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert len(resp.content) == 512


def test_download_no_output_path(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="14:00:00", out_time="14:05:00",
            status="completed", progress_percent=100.0, has_gaps=False,
            output_path=None,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.get(f"/api/v1/exports/{job_id}/download")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Export retention
# ---------------------------------------------------------------------------

def test_delete_old_files_deletes_aged(tmp_path):
    mp4 = tmp_path / "old.mp4"
    mp4.write_bytes(b"\x00" * 64)
    # Back-date mtime to 40 days ago
    old_mtime = time.time() - 40 * 86400
    import os
    os.utime(mp4, (old_mtime, old_mtime))

    count = _delete_old_files(tmp_path, "*.mp4", max_age_seconds=30 * 86400)
    assert count == 1
    assert not mp4.exists()


def test_delete_old_files_keeps_recent(tmp_path):
    mp4 = tmp_path / "recent.mp4"
    mp4.write_bytes(b"\x00" * 64)
    # File is freshly created — mtime is now

    count = _delete_old_files(tmp_path, "*.mp4", max_age_seconds=30 * 86400)
    assert count == 0
    assert mp4.exists()


def test_delete_old_files_nonexistent_root(tmp_path):
    result = _delete_old_files(tmp_path / "nonexistent", "*.mp4", max_age_seconds=0)
    assert result == 0


def test_prune_empty_dirs(tmp_path):
    subdir = tmp_path / "2026-04-01"
    subdir.mkdir()
    # subdir is empty → should be removed

    _prune_empty_dirs(tmp_path)
    assert not subdir.exists()


def test_prune_empty_dirs_keeps_nonempty(tmp_path):
    subdir = tmp_path / "2026-04-01"
    subdir.mkdir()
    (subdir / "file.mp4").write_bytes(b"\x00")

    _prune_empty_dirs(tmp_path)
    assert subdir.exists()


def test_run_export_retention_disabled(tmp_path):
    """export_retention_days=0 → nothing deleted."""
    old_mp4 = tmp_path / "old.mp4"
    old_mp4.write_bytes(b"\x00" * 64)
    import os
    os.utime(old_mp4, (0, 0))  # epoch → very old

    with patch("app.services.export_retention.get_settings") as mock_s:
        mock_settings = MagicMock()
        mock_settings.export_retention_days = 0
        mock_settings.exports_dir = tmp_path
        mock_settings.export_logs_dir = tmp_path
        mock_s.return_value = mock_settings
        _run_export_retention_sync()

    assert old_mp4.exists()  # still there


def test_run_export_retention_deletes_old(tmp_path):
    exports = tmp_path / "exports"
    logs = tmp_path / "logs"
    exports.mkdir()
    logs.mkdir()

    old_mp4 = exports / "old.mp4"
    old_log = logs / "export_1.log"
    old_mp4.write_bytes(b"\x00" * 64)
    old_log.write_text("log content")

    import os
    age = time.time() - 35 * 86400  # 35 days old
    os.utime(old_mp4, (age, age))
    os.utime(old_log, (age, age))

    with patch("app.services.export_retention.get_settings") as mock_s:
        mock_settings = MagicMock()
        mock_settings.export_retention_days = 30
        mock_settings.exports_dir = exports
        mock_settings.export_logs_dir = logs
        mock_s.return_value = mock_settings
        _run_export_retention_sync()

    assert not old_mp4.exists()
    assert not old_log.exists()


def test_run_export_retention_keeps_fresh(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()

    fresh_mp4 = exports / "new.mp4"
    fresh_mp4.write_bytes(b"\x00" * 64)
    # mtime is now — fresh

    with patch("app.services.export_retention.get_settings") as mock_s:
        mock_settings = MagicMock()
        mock_settings.export_retention_days = 30
        mock_settings.exports_dir = exports
        mock_settings.export_logs_dir = tmp_path / "logs"
        mock_s.return_value = mock_settings
        _run_export_retention_sync()

    assert fresh_mp4.exists()


# ---------------------------------------------------------------------------
# API: validation integrated into POST /channels/{id}/exports
# ---------------------------------------------------------------------------

def test_post_export_future_date_rejected(app_client):
    client, _ = app_client
    worker_mock = MagicMock()
    with patch("app.api.v1.exports.get_export_worker", return_value=worker_mock):
        resp = client.post(
            "/api/v1/channels/rts1/exports",
            json={"date": "2099-01-01", "in_time": "14:00:00", "out_time": "14:05:00"},
        )
    assert resp.status_code == 400
    assert "future" in resp.json()["detail"].lower()


def test_post_export_out_before_in_rejected(app_client):
    client, _ = app_client
    worker_mock = MagicMock()
    with patch("app.api.v1.exports.get_export_worker", return_value=worker_mock):
        resp = client.post(
            "/api/v1/channels/rts1/exports",
            json={"date": "2026-01-01", "in_time": "15:00:00", "out_time": "14:00:00"},
        )
    assert resp.status_code == 400


def test_post_export_equal_times_rejected(app_client):
    client, _ = app_client
    worker_mock = MagicMock()
    with patch("app.api.v1.exports.get_export_worker", return_value=worker_mock):
        resp = client.post(
            "/api/v1/channels/rts1/exports",
            json={"date": "2026-01-01", "in_time": "14:00:00", "out_time": "14:00:00"},
        )
    assert resp.status_code == 400
