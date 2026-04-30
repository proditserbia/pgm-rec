"""
Phase 2B unit tests — Export Engine.

Covers:
- Settings: new Phase 2B fields (exports_dir, max_concurrent_exports, etc.)
- DB model: ExportJob (create, update, query)
- Schemas: ExportJobStatus, ExportJobRequest, ExportJobResponse
- export_service: build_output_path, build_log_path, _sanitize,
                  write_concat_file_with_outpoint, build_export_command (single/multi),
                  build_export_command_reencode, _parse_progress
- export_worker: ExportWorker (enqueue, cancel, register/unregister process)
- API endpoints: POST /exports, GET /exports/{id}, GET /exports, POST /exports/{id}/cancel
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import get_settings
from app.db.models import Base, Channel, ExportJob, SegmentRecord
from app.db.session import get_db
from app.models.schemas import (
    ChannelConfig,
    ExportJobRequest,
    ExportJobResponse,
    ExportJobStatus,
    ResolveRangeRequest,
    ResolveRangeResponse,
    SegmentSlice,
    GapEntry,
)
from app.services.export_service import (
    _parse_progress,
    _sanitize,
    build_export_command,
    build_export_command_reencode,
    build_log_path,
    build_output_path,
    write_concat_file_with_outpoint,
)
from app.services.export_worker import ExportWorker


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


def _seed_segment(
    db: Session,
    channel_id: str = "rts1",
    start: datetime | None = None,
    duration: float = 300.0,
    filename: str | None = None,
    path: str = "/tmp/chunks/seg.mp4",
) -> SegmentRecord:
    if start is None:
        start = datetime(2026, 4, 1, 14, 0, 0)
    end = start + timedelta(seconds=duration)
    if filename is None:
        filename = f"{channel_id}_{start.strftime('%Y_%m_%d_%H_%M_%S')}.mp4"
    seg = SegmentRecord(
        channel_id=channel_id,
        filename=filename,
        path=path,
        start_time=start,
        end_time=end,
        duration_seconds=duration,
        size_bytes=1024 * 1024,
        status="complete",
        ffprobe_verified=True,
        manifest_date=start.strftime("%Y-%m-%d"),
    )
    db.add(seg)
    db.commit()
    return seg


# ---------------------------------------------------------------------------
# Settings — Phase 2B fields
# ---------------------------------------------------------------------------

def test_settings_phase2b_defaults():
    s = get_settings()
    assert s.exports_dir.name == "exports"
    assert s.export_logs_dir.name == "exports"
    assert s.max_concurrent_exports == 2
    assert s.export_ffmpeg_threads == 0


# ---------------------------------------------------------------------------
# DB model — ExportJob
# ---------------------------------------------------------------------------

def test_export_job_create(db_session):
    job = ExportJob(
        channel_id="rts1",
        date="2026-04-01",
        in_time="14:05:30",
        out_time="14:22:10",
        status="queued",
        progress_percent=0.0,
        has_gaps=False,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    assert job.id is not None
    assert job.status == "queued"
    assert job.progress_percent == 0.0
    assert job.has_gaps is False
    assert job.output_path is None
    assert job.error_message is None


def test_export_job_update_status(db_session):
    job = ExportJob(
        channel_id="rts1",
        date="2026-04-01",
        in_time="14:05:30",
        out_time="14:22:10",
        status="queued",
        progress_percent=0.0,
        has_gaps=False,
    )
    db_session.add(job)
    db_session.commit()

    job.status = "running"
    job.progress_percent = 50.0
    job.started_at = datetime.now(timezone.utc)
    db_session.commit()
    db_session.refresh(job)

    assert job.status == "running"
    assert job.progress_percent == 50.0
    assert job.started_at is not None


def test_export_job_completed(db_session):
    job = ExportJob(
        channel_id="rts1",
        date="2026-04-01",
        in_time="14:00:00",
        out_time="14:05:00",
        status="queued",
        progress_percent=0.0,
        has_gaps=False,
    )
    db_session.add(job)
    db_session.commit()

    job.status = "completed"
    job.progress_percent = 100.0
    job.output_path = "/data/exports/rts1/2026-04-01/rts1_2026-04-01_14-00-00_to_14-05-00.mp4"
    job.completed_at = datetime.now(timezone.utc)
    db_session.commit()
    db_session.refresh(job)

    assert job.status == "completed"
    assert job.output_path is not None
    assert job.completed_at is not None


def test_export_job_failed(db_session):
    job = ExportJob(
        channel_id="rts1",
        date="2026-04-01",
        in_time="14:00:00",
        out_time="14:05:00",
        status="queued",
        progress_percent=0.0,
        has_gaps=False,
    )
    db_session.add(job)
    db_session.commit()

    job.status = "failed"
    job.error_message = "FFmpeg failed with exit code 1"
    job.completed_at = datetime.now(timezone.utc)
    db_session.commit()
    db_session.refresh(job)

    assert job.status == "failed"
    assert "FFmpeg" in job.error_message


def test_export_job_multiple(db_session):
    for i in range(3):
        job = ExportJob(
            channel_id="rts1",
            date=f"2026-04-0{i+1}",
            in_time="12:00:00",
            out_time="12:05:00",
            status="queued",
            progress_percent=0.0,
            has_gaps=False,
        )
        db_session.add(job)
    db_session.commit()
    count = db_session.query(ExportJob).filter(ExportJob.channel_id == "rts1").count()
    assert count == 3


# ---------------------------------------------------------------------------
# Schemas — ExportJob*
# ---------------------------------------------------------------------------

def test_export_job_status_values():
    assert ExportJobStatus.QUEUED == "queued"
    assert ExportJobStatus.RUNNING == "running"
    assert ExportJobStatus.COMPLETED == "completed"
    assert ExportJobStatus.FAILED == "failed"
    assert ExportJobStatus.CANCELLED == "cancelled"


def test_export_job_request_schema():
    req = ExportJobRequest(date="2026-04-01", in_time="14:05:30", out_time="14:22:10")
    assert req.date == "2026-04-01"
    assert req.in_time == "14:05:30"
    assert req.out_time == "14:22:10"
    assert req.allow_gaps is True  # default


def test_export_job_request_allow_gaps_false():
    req = ExportJobRequest(date="2026-04-01", in_time="14:00:00", out_time="15:00:00", allow_gaps=False)
    assert req.allow_gaps is False


def test_export_job_response_schema():
    resp = ExportJobResponse(
        id=1,
        channel_id="rts1",
        date="2026-04-01",
        in_time="14:00:00",
        out_time="14:05:00",
        status=ExportJobStatus.COMPLETED,
        progress_percent=100.0,
        has_gaps=False,
        created_at=datetime.now(timezone.utc),
    )
    assert resp.status == ExportJobStatus.COMPLETED
    assert resp.output_path is None  # optional


# ---------------------------------------------------------------------------
# export_service — helpers
# ---------------------------------------------------------------------------

def test_sanitize_normal():
    assert _sanitize("14:05:30") == "14_05_30"


def test_sanitize_safe_chars():
    assert _sanitize("rts1") == "rts1"
    assert _sanitize("2026-04-01") == "2026-04-01"


def test_sanitize_special_chars():
    result = _sanitize("a/b\\c d")
    assert "/" not in result
    assert "\\" not in result
    assert " " not in result


def test_build_output_path(tmp_path):
    out = build_output_path(tmp_path, "rts1", "2026-04-01", "14:05:30", "14:22:10")
    assert out.parent.exists()
    assert out.name == "rts1_2026-04-01_14_05_30_to_14_22_10.mp4"
    assert "rts1" in str(out)
    assert "2026-04-01" in str(out)


def test_build_output_path_creates_dirs(tmp_path):
    out = build_output_path(tmp_path, "rts2", "2026-04-02", "00:00:00", "23:59:59")
    assert out.parent.is_dir()


def test_build_log_path(tmp_path):
    log = build_log_path(tmp_path, "rts1", "2026-04-01", 42)
    assert log.parent.is_dir()
    assert log.name == "export_42.log"


# ---------------------------------------------------------------------------
# export_service — concat file writer
# ---------------------------------------------------------------------------

def test_write_concat_file_single_segment(tmp_path):
    concat = tmp_path / "concat.txt"
    segs = [
        SegmentSlice(
            filename="seg1.mp4",
            path="/tmp/seg1.mp4",
            start_time=datetime(2026, 4, 1, 14, 0, 0),
            end_time=datetime(2026, 4, 1, 14, 5, 0),
            duration_seconds=300.0,
        )
    ]
    write_concat_file_with_outpoint(concat, segs, first_offset=30.0, last_outpoint=270.0)
    content = concat.read_text()
    assert "ffconcat version 1.0" in content
    assert "file '/tmp/seg1.mp4'" in content
    assert "inpoint 30.0" in content
    assert "outpoint 270.0" in content


def test_write_concat_file_no_trim_needed(tmp_path):
    concat = tmp_path / "concat.txt"
    segs = [
        SegmentSlice(
            filename="seg1.mp4",
            path="/tmp/seg1.mp4",
            start_time=datetime(2026, 4, 1, 14, 0, 0),
            end_time=datetime(2026, 4, 1, 14, 5, 0),
            duration_seconds=300.0,
        )
    ]
    # no offset, outpoint == duration → no trim directives
    write_concat_file_with_outpoint(concat, segs, first_offset=0.0, last_outpoint=300.0)
    content = concat.read_text()
    assert "inpoint" not in content
    assert "outpoint" not in content


def test_write_concat_file_multiple_segments(tmp_path):
    concat = tmp_path / "concat.txt"
    segs = [
        SegmentSlice(
            filename=f"seg{i}.mp4",
            path=f"/tmp/seg{i}.mp4",
            start_time=datetime(2026, 4, 1, 14, i * 5, 0),
            end_time=datetime(2026, 4, 1, 14, (i + 1) * 5, 0),
            duration_seconds=300.0,
        )
        for i in range(3)
    ]
    write_concat_file_with_outpoint(concat, segs, first_offset=30.0, last_outpoint=120.0)
    content = concat.read_text()
    assert content.count("file '") == 3
    assert "inpoint 30.0" in content
    assert "outpoint 120.0" in content


# ---------------------------------------------------------------------------
# export_service — command builders
# ---------------------------------------------------------------------------

def _make_resolve(
    segments: list[SegmentSlice],
    first_offset: float = 0.0,
    duration: float = 300.0,
    has_gaps: bool = False,
) -> ResolveRangeResponse:
    return ResolveRangeResponse(
        channel_id="rts1",
        date="2026-04-01",
        in_time="14:00:00",
        out_time="14:05:00",
        segments=segments,
        first_segment_offset_seconds=first_offset,
        export_duration_seconds=duration,
        has_gaps=has_gaps,
        gaps=[],
    )


def _make_slice(idx: int = 0) -> SegmentSlice:
    return SegmentSlice(
        filename=f"seg{idx}.mp4",
        path=f"/tmp/seg{idx}.mp4",
        start_time=datetime(2026, 4, 1, 14, idx * 5, 0),
        end_time=datetime(2026, 4, 1, 14, (idx + 1) * 5, 0),
        duration_seconds=300.0,
    )


def test_build_export_command_single_segment(tmp_path):
    out = tmp_path / "out.mp4"
    resolve = _make_resolve([_make_slice(0)], first_offset=30.0, duration=270.0)
    cmd = build_export_command(resolve, out, "ffmpeg", threads=0, concat_file=None)
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd
    assert "30.000000" in cmd
    assert "-t" in cmd
    assert "270.000000" in cmd
    assert "-c" in cmd
    assert "copy" in cmd
    assert str(out) in cmd


def test_build_export_command_single_segment_with_threads(tmp_path):
    out = tmp_path / "out.mp4"
    resolve = _make_resolve([_make_slice(0)], duration=300.0)
    cmd = build_export_command(resolve, out, "ffmpeg", threads=4, concat_file=None)
    assert "-threads" in cmd
    assert "4" in cmd


def test_build_export_command_no_threads(tmp_path):
    out = tmp_path / "out.mp4"
    resolve = _make_resolve([_make_slice(0)], duration=300.0)
    cmd = build_export_command(resolve, out, "ffmpeg", threads=0, concat_file=None)
    assert "-threads" not in cmd


def test_build_export_command_multi_segment(tmp_path):
    out = tmp_path / "out.mp4"
    concat = tmp_path / "concat.txt"
    concat.write_text("ffconcat version 1.0\n", encoding="utf-8")
    segs = [_make_slice(i) for i in range(3)]
    resolve = _make_resolve(segs, first_offset=30.0, duration=870.0)
    cmd = build_export_command(resolve, out, "ffmpeg", threads=0, concat_file=concat)
    assert "-f" in cmd
    assert "concat" in cmd
    assert "-safe" in cmd
    assert str(concat) in cmd
    assert str(out) in cmd
    # No -ss/-t in concat mode
    assert "-ss" not in cmd
    assert "-t" not in cmd


def test_build_export_command_reencode_single(tmp_path):
    out = tmp_path / "out.mp4"
    resolve = _make_resolve([_make_slice(0)], first_offset=0.0, duration=300.0)
    cmd = build_export_command_reencode(resolve, out, "ffmpeg", threads=0, concat_file=None)
    assert "-c:v" in cmd
    assert "libx264" in cmd
    assert "-c:a" in cmd
    assert "aac" in cmd
    assert "copy" not in cmd


def test_build_export_command_reencode_with_threads(tmp_path):
    out = tmp_path / "out.mp4"
    resolve = _make_resolve([_make_slice(0)], duration=300.0)
    cmd = build_export_command_reencode(resolve, out, "ffmpeg", threads=2, concat_file=None)
    assert "-threads" in cmd
    assert "2" in cmd


def test_build_export_command_reencode_multi(tmp_path):
    out = tmp_path / "out.mp4"
    concat = tmp_path / "concat.txt"
    concat.write_text("ffconcat version 1.0\n", encoding="utf-8")
    segs = [_make_slice(i) for i in range(2)]
    resolve = _make_resolve(segs, duration=600.0)
    cmd = build_export_command_reencode(resolve, out, "ffmpeg", threads=0, concat_file=concat)
    assert "-f" in cmd
    assert "concat" in cmd
    assert "libx264" in cmd


# ---------------------------------------------------------------------------
# export_service — progress parser
# ---------------------------------------------------------------------------

def test_parse_progress_normal():
    line = "frame=  300 fps= 25 size=   1024kB time=00:00:30.00 bitrate=..."
    pct = _parse_progress(line, total_seconds=300.0)
    assert pct == pytest.approx(10.0, abs=0.1)


def test_parse_progress_complete():
    line = "time=00:05:00.00 bitrate=1234.5kbits/s"
    pct = _parse_progress(line, total_seconds=300.0)
    assert pct == pytest.approx(100.0, abs=0.1)


def test_parse_progress_overshoot():
    # Should clamp to 100.0
    line = "time=00:06:00.00"
    pct = _parse_progress(line, total_seconds=300.0)
    assert pct == 100.0


def test_parse_progress_no_match():
    pct = _parse_progress("ffmpeg version 6.0", total_seconds=300.0)
    assert pct is None


def test_parse_progress_zero_duration():
    line = "time=00:00:30.00"
    pct = _parse_progress(line, total_seconds=0)
    assert pct is None


# ---------------------------------------------------------------------------
# ExportWorker — unit tests (no real asyncio needed for most)
# ---------------------------------------------------------------------------

def test_export_worker_register_unregister():
    worker = ExportWorker(max_concurrent=2)
    mock_proc = MagicMock()
    worker.register_process(1, mock_proc)
    assert worker._processes[1] is mock_proc
    worker.unregister_process(1)
    assert 1 not in worker._processes


def test_export_worker_cancel_job_no_process():
    worker = ExportWorker(max_concurrent=2)
    # Nothing registered → cancel_job returns False (no signal sent)
    result = worker.cancel_job(999)
    assert result is False


def test_export_worker_cancel_running_process():
    worker = ExportWorker(max_concurrent=2)
    mock_proc = MagicMock()
    worker.register_process(1, mock_proc)
    result = worker.cancel_job(1)
    assert result is True
    mock_proc.terminate.assert_called_once()


def test_export_worker_cancel_running_task():
    worker = ExportWorker(max_concurrent=2)
    mock_task = MagicMock()
    mock_task.done.return_value = False
    worker._job_tasks[1] = mock_task
    result = worker.cancel_job(1)
    assert result is True
    mock_task.cancel.assert_called_once()


def test_export_worker_cancel_done_task():
    worker = ExportWorker(max_concurrent=2)
    mock_task = MagicMock()
    mock_task.done.return_value = True
    worker._job_tasks[1] = mock_task
    result = worker.cancel_job(1)
    # Task is done, process not present → False
    assert result is False
    mock_task.cancel.assert_not_called()


def test_export_worker_enqueue_no_wake():
    # enqueue without start() should not crash
    worker = ExportWorker(max_concurrent=2)
    worker.enqueue(42)  # _wake is None — should be no-op


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------

@pytest.fixture
def app_client(in_memory_engine, tmp_path):
    """Test client with overridden DB and settings."""
    from fastapi import FastAPI
    from app.api.v1 import exports as exports_router
    from app.api.v1.deps import get_current_user
    from app.db.models import User

    test_app = FastAPI()
    test_app.include_router(exports_router.router, prefix="/api/v1")

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=in_memory_engine)

    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_auth():
        return User(id=1, username="testadmin", password_hash="x", role="admin", is_active=True)

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_auth

    with TestClient(test_app) as client:
        # Seed a channel and segments in the DB
        with SessionLocal() as db:
            _seed_channel(db)
            for i in range(3):
                _seed_segment(
                    db,
                    start=datetime(2026, 4, 1, 14, i * 5, 0),
                    duration=300.0,
                    filename=f"rts1_seg{i}.mp4",
                    path=f"/tmp/seg{i}.mp4",
                )
        yield client, SessionLocal


def test_create_export_job_success(app_client, tmp_path):
    client, SessionLocal = app_client
    settings = get_settings()

    worker_mock = MagicMock()
    with (
        patch("app.api.v1.exports.get_export_worker", return_value=worker_mock),
        patch("app.api.v1.exports.resolve_export_range") as mock_resolve,
    ):
        mock_resolve.return_value = ResolveRangeResponse(
            channel_id="rts1",
            date="2026-04-01",
            in_time="14:00:00",
            out_time="14:15:00",
            segments=[_make_slice(i) for i in range(3)],
            first_segment_offset_seconds=0.0,
            export_duration_seconds=900.0,
            has_gaps=False,
            gaps=[],
        )
        resp = client.post(
            "/api/v1/channels/rts1/exports",
            json={"date": "2026-04-01", "in_time": "14:00:00", "out_time": "14:15:00"},
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["channel_id"] == "rts1"
    assert data["status"] == "queued"
    assert data["has_gaps"] is False
    assert "id" in data
    worker_mock.enqueue.assert_called_once()


def test_create_export_job_channel_not_found(app_client):
    client, _ = app_client
    with patch("app.api.v1.exports.resolve_export_range") as mock_resolve:
        mock_resolve.return_value = MagicMock(has_gaps=False, segments=[_make_slice()])
        resp = client.post(
            "/api/v1/channels/nonexistent/exports",
            json={"date": "2026-04-01", "in_time": "14:00:00", "out_time": "14:05:00"},
        )
    assert resp.status_code == 404


def test_create_export_job_no_segments(app_client):
    client, _ = app_client
    worker_mock = MagicMock()
    with (
        patch("app.api.v1.exports.get_export_worker", return_value=worker_mock),
        patch("app.api.v1.exports.resolve_export_range") as mock_resolve,
    ):
        mock_resolve.return_value = ResolveRangeResponse(
            channel_id="rts1",
            date="2026-04-01",
            in_time="02:00:00",
            out_time="02:05:00",
            segments=[],
            first_segment_offset_seconds=0.0,
            export_duration_seconds=300.0,
            has_gaps=False,
            gaps=[],
        )
        resp = client.post(
            "/api/v1/channels/rts1/exports",
            json={"date": "2026-04-01", "in_time": "02:00:00", "out_time": "02:05:00"},
        )
    assert resp.status_code == 422


def test_create_export_job_with_gaps_allowed(app_client):
    client, _ = app_client
    worker_mock = MagicMock()
    with (
        patch("app.api.v1.exports.get_export_worker", return_value=worker_mock),
        patch("app.api.v1.exports.resolve_export_range") as mock_resolve,
    ):
        mock_resolve.return_value = ResolveRangeResponse(
            channel_id="rts1",
            date="2026-04-01",
            in_time="14:00:00",
            out_time="14:15:00",
            segments=[_make_slice(0), _make_slice(2)],  # seg1 missing → gap
            first_segment_offset_seconds=0.0,
            export_duration_seconds=900.0,
            has_gaps=True,
            gaps=[GapEntry(
                gap_start=datetime(2026, 4, 1, 14, 5, 0),
                gap_end=datetime(2026, 4, 1, 14, 10, 0),
                gap_seconds=300.0,
            )],
        )
        resp = client.post(
            "/api/v1/channels/rts1/exports",
            json={"date": "2026-04-01", "in_time": "14:00:00", "out_time": "14:15:00",
                  "allow_gaps": True},
        )
    assert resp.status_code == 201
    assert resp.json()["has_gaps"] is True


def test_create_export_job_with_gaps_rejected(app_client):
    client, _ = app_client
    worker_mock = MagicMock()
    with (
        patch("app.api.v1.exports.get_export_worker", return_value=worker_mock),
        patch("app.api.v1.exports.resolve_export_range") as mock_resolve,
    ):
        mock_resolve.return_value = ResolveRangeResponse(
            channel_id="rts1",
            date="2026-04-01",
            in_time="14:00:00",
            out_time="14:10:00",
            segments=[_make_slice(0)],
            first_segment_offset_seconds=0.0,
            export_duration_seconds=600.0,
            has_gaps=True,
            gaps=[GapEntry(
                gap_start=datetime(2026, 4, 1, 14, 5, 0),
                gap_end=datetime(2026, 4, 1, 14, 10, 0),
                gap_seconds=300.0,
            )],
        )
        resp = client.post(
            "/api/v1/channels/rts1/exports",
            json={"date": "2026-04-01", "in_time": "14:00:00", "out_time": "14:10:00",
                  "allow_gaps": False},
        )
    assert resp.status_code == 409


def test_get_export_job(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="14:00:00", out_time="14:05:00",
            status="completed", progress_percent=100.0,
            has_gaps=False,
            output_path="/data/exports/rts1/2026-04-01/out.mp4",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.get(f"/api/v1/exports/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == job_id
    assert data["status"] == "completed"
    assert data["output_path"] is not None


def test_get_export_job_not_found(app_client):
    client, _ = app_client
    resp = client.get("/api/v1/exports/99999")
    assert resp.status_code == 404


def test_list_export_jobs(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        for i in range(4):
            db.add(ExportJob(
                channel_id="rts1", date=f"2026-04-0{i+1}",
                in_time="12:00:00", out_time="12:05:00",
                status="completed", progress_percent=100.0, has_gaps=False,
            ))
        db.commit()

    resp = client.get("/api/v1/exports")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 4


def test_list_export_jobs_filter_channel(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        db.add(ExportJob(
            channel_id="rts2", date="2026-04-01",
            in_time="12:00:00", out_time="12:05:00",
            status="queued", progress_percent=0.0, has_gaps=False,
        ))
        db.commit()

    resp = client.get("/api/v1/exports?channel_id=rts2")
    assert resp.status_code == 200
    data = resp.json()
    assert all(j["channel_id"] == "rts2" for j in data)


def test_list_export_jobs_filter_status(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        db.add(ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="10:00:00", out_time="10:05:00",
            status="running", progress_percent=50.0, has_gaps=False,
        ))
        db.commit()

    resp = client.get("/api/v1/exports?status=running")
    assert resp.status_code == 200
    data = resp.json()
    assert all(j["status"] == "running" for j in data)


def test_list_export_jobs_invalid_status(app_client):
    client, _ = app_client
    resp = client.get("/api/v1/exports?status=unknown_status")
    assert resp.status_code == 400


def test_cancel_queued_job(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="08:00:00", out_time="08:05:00",
            status="queued", progress_percent=0.0, has_gaps=False,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    worker_mock = MagicMock()
    worker_mock.cancel_job.return_value = False  # not running yet

    with patch("app.api.v1.exports.get_export_worker", return_value=worker_mock):
        resp = client.post(f"/api/v1/exports/{job_id}/cancel")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"


def test_cancel_running_job(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="09:00:00", out_time="09:15:00",
            status="running", progress_percent=25.0, has_gaps=False,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    worker_mock = MagicMock()
    worker_mock.cancel_job.return_value = True  # process was running

    with patch("app.api.v1.exports.get_export_worker", return_value=worker_mock):
        resp = client.post(f"/api/v1/exports/{job_id}/cancel")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"
    worker_mock.cancel_job.assert_called_once_with(job_id)


def test_cancel_completed_job_conflict(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="09:00:00", out_time="09:05:00",
            status="completed", progress_percent=100.0, has_gaps=False,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.post(f"/api/v1/exports/{job_id}/cancel")
    assert resp.status_code == 409


def test_cancel_failed_job_conflict(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="09:00:00", out_time="09:05:00",
            status="failed", progress_percent=0.0, has_gaps=False,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.post(f"/api/v1/exports/{job_id}/cancel")
    assert resp.status_code == 409


def test_cancel_already_cancelled_conflict(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        job = ExportJob(
            channel_id="rts1", date="2026-04-01",
            in_time="09:00:00", out_time="09:05:00",
            status="cancelled", progress_percent=0.0, has_gaps=False,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    resp = client.post(f"/api/v1/exports/{job_id}/cancel")
    assert resp.status_code == 409


def test_cancel_nonexistent_job(app_client):
    client, _ = app_client
    resp = client.post("/api/v1/exports/99999/cancel")
    assert resp.status_code == 404


def test_list_export_jobs_limit(app_client):
    client, SessionLocal = app_client
    with SessionLocal() as db:
        for i in range(10):
            db.add(ExportJob(
                channel_id="rts1", date="2026-04-01",
                in_time=f"{i:02d}:00:00", out_time=f"{i:02d}:05:00",
                status="completed", progress_percent=100.0, has_gaps=False,
            ))
        db.commit()

    resp = client.get("/api/v1/exports?limit=3")
    assert resp.status_code == 200
    assert len(resp.json()) <= 3
