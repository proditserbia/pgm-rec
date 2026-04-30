"""
Phase 2A unit tests — Recording Manifest & Export Index Layer.

Covers:
- parse_segment_start_time: valid/invalid filenames
- ffprobe_duration: mocking subprocess
- load_manifest / save_manifest: JSON round-trip
- _compute_gaps: gap detection logic
- register_segment: full flow (mocking ffprobe + real in-memory DB)
- resolve_export_range: full flow with real in-memory DB
- Schema models: SegmentEntry, GapEntry, DailyManifest, ResolveRangeRequest/Response
- Settings: new Phase 2A fields
- DB models: SegmentRecord, ManifestGap
- API endpoints: GET manifests/{date}, GET segments, POST exports/resolve-range
"""
from __future__ import annotations

import json
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
from app.db.models import Base, Channel, ManifestGap, SegmentRecord
from app.db.session import get_db
from app.models.schemas import (
    ChannelConfig,
    DailyManifest,
    GapEntry,
    ResolveRangeRequest,
    SegmentEntry,
    SegmentSlice,
    SegmentStatus,
)
from app.services.manifest_service import (
    _compute_gaps,
    _get_ffprobe_path,
    _segment_duration_target_seconds,
    ffprobe_duration,
    load_manifest,
    parse_segment_start_time,
    register_segment,
    resolve_export_range,
    save_manifest,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_manifests(tmp_path: Path) -> Path:
    d = tmp_path / "manifests"
    d.mkdir()
    return d


@pytest.fixture
def in_memory_engine():
    """
    In-memory SQLite with StaticPool so all sessions share the same
    connection and see the tables created by create_all().
    """
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


def _utcdt(year=2026, month=4, day=1, hour=0, minute=0, second=0) -> datetime:
    """Naive UTC datetime — matches what SQLite returns after a round-trip."""
    return datetime(year, month, day, hour, minute, second)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_settings_phase2a_defaults():
    s = get_settings()
    assert s.manifests_dir.name == "manifests"
    assert s.manifest_timezone == "Europe/Belgrade"
    assert s.manifest_gap_tolerance_seconds == 10.0


# ---------------------------------------------------------------------------
# DB models
# ---------------------------------------------------------------------------

def test_segment_record_model(db_session):
    _seed_channel(db_session)
    now = datetime.now(timezone.utc)
    rec = SegmentRecord(
        channel_id="rts1",
        filename="010426-000000.mp4",
        path="/tmp/chunks/010426-000000.mp4",
        start_time=now,
        end_time=now + timedelta(seconds=300),
        duration_seconds=300.0,
        size_bytes=12345678,
        status="complete",
        ffprobe_verified=True,
        manifest_date="2026-04-01",
    )
    db_session.add(rec)
    db_session.commit()
    fetched = db_session.query(SegmentRecord).filter_by(filename="010426-000000.mp4").first()
    assert fetched is not None
    assert fetched.channel_id == "rts1"
    assert fetched.duration_seconds == 300.0
    assert fetched.ffprobe_verified is True


def test_manifest_gap_model(db_session):
    _seed_channel(db_session)
    now = datetime.now(timezone.utc)
    gap = ManifestGap(
        channel_id="rts1",
        manifest_date="2026-04-01",
        gap_start=now,
        gap_end=now + timedelta(seconds=30),
        gap_seconds=30.0,
    )
    db_session.add(gap)
    db_session.commit()
    fetched = db_session.query(ManifestGap).filter_by(manifest_date="2026-04-01").first()
    assert fetched is not None
    assert fetched.gap_seconds == 30.0


def test_segment_record_unique_constraint(db_session):
    from sqlalchemy.exc import IntegrityError
    _seed_channel(db_session)
    now = datetime.now(timezone.utc)
    def _mk():
        return SegmentRecord(
            channel_id="rts1",
            filename="010426-000000.mp4",
            path="/tmp/chunks/010426-000000.mp4",
            start_time=now,
            end_time=now + timedelta(seconds=300),
            duration_seconds=300.0,
            size_bytes=1000,
            status="complete",
            ffprobe_verified=False,
            manifest_date="2026-04-01",
        )
    db_session.add(_mk())
    db_session.commit()
    db_session.add(_mk())
    with pytest.raises(IntegrityError):
        db_session.commit()


# ---------------------------------------------------------------------------
# parse_segment_start_time
# ---------------------------------------------------------------------------

def test_parse_segment_start_time_valid():
    # April 1, 2026 14:05:30 Belgrade (UTC+2 summer) → 12:05:30 UTC
    result = parse_segment_start_time("010426-140530.mp4", "%d%m%y-%H%M%S", "Europe/Belgrade")
    assert result is not None
    assert result.tzinfo is not None
    assert result.hour == 12
    assert result.minute == 5
    assert result.second == 30


def test_parse_segment_start_time_midnight():
    result = parse_segment_start_time("010426-000000.mp4", "%d%m%y-%H%M%S", "Europe/Belgrade")
    assert result is not None
    assert result.tzinfo == timezone.utc
    # midnight Belgrade (UTC+2) = 22:00 UTC on March 31
    assert result.day == 31
    assert result.month == 3
    assert result.hour == 22


def test_parse_segment_start_time_invalid_filename():
    result = parse_segment_start_time("not_a_valid_name.mp4", "%d%m%y-%H%M%S", "Europe/Belgrade")
    assert result is None


def test_parse_segment_start_time_utc_fallback():
    result = parse_segment_start_time("010426-120000.mp4", "%d%m%y-%H%M%S", "Invalid/Timezone")
    assert result is not None
    assert result.tzinfo == timezone.utc
    assert result.hour == 12


def test_parse_segment_start_time_different_pattern():
    result = parse_segment_start_time("2026-04-01_14-05-30.mp4", "%Y-%m-%d_%H-%M-%S", "UTC")
    assert result is not None
    assert result.year == 2026
    assert result.month == 4
    assert result.day == 1


# ---------------------------------------------------------------------------
# _segment_duration_target_seconds
# ---------------------------------------------------------------------------

def test_duration_target_300():
    assert _segment_duration_target_seconds("00:05:00") == 300


def test_duration_target_3600():
    assert _segment_duration_target_seconds("01:00:00") == 3600


def test_duration_target_invalid_falls_back():
    assert _segment_duration_target_seconds("bad") == 300


# ---------------------------------------------------------------------------
# _get_ffprobe_path
# ---------------------------------------------------------------------------

def test_get_ffprobe_path_windows():
    p = _get_ffprobe_path("C:\\ffmpeg\\bin\\ffmpeg.exe")
    assert "ffprobe" in p.lower()


def test_get_ffprobe_path_unix():
    p = _get_ffprobe_path("/usr/local/bin/ffmpeg")
    assert "ffprobe" in p


def test_get_ffprobe_path_plain():
    p = _get_ffprobe_path("ffmpeg")
    assert "ffprobe" in p


def test_get_ffprobe_path_custom():
    p = _get_ffprobe_path("custom_ffmpeg_wrapper")
    assert p == "ffprobe"


# ---------------------------------------------------------------------------
# ffprobe_duration
# ---------------------------------------------------------------------------

def test_ffprobe_duration_success(tmp_path):
    fake_file = tmp_path / "test.mp4"
    fake_file.write_bytes(b"fake")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="299.98\n")
        result = ffprobe_duration(fake_file, "ffprobe")
    assert result == pytest.approx(299.98)


def test_ffprobe_duration_not_found(tmp_path):
    fake_file = tmp_path / "test.mp4"
    fake_file.write_bytes(b"fake")
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = ffprobe_duration(fake_file, "ffprobe")
    assert result is None


def test_ffprobe_duration_nonzero_returncode(tmp_path):
    fake_file = tmp_path / "test.mp4"
    fake_file.write_bytes(b"fake")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = ffprobe_duration(fake_file, "ffprobe")
    assert result is None


def test_ffprobe_duration_timeout(tmp_path):
    import subprocess
    fake_file = tmp_path / "test.mp4"
    fake_file.write_bytes(b"fake")
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 15)):
        result = ffprobe_duration(fake_file, "ffprobe")
    assert result is None


# ---------------------------------------------------------------------------
# load_manifest / save_manifest
# ---------------------------------------------------------------------------

def _make_segment_entry(filename: str, start: datetime, duration: float = 300.0) -> SegmentEntry:
    now = datetime.now(timezone.utc)
    return SegmentEntry(
        filename=filename,
        path=f"/tmp/{filename}",
        start_time=start,
        end_time=start + timedelta(seconds=duration),
        duration_seconds=duration,
        size_bytes=10_000_000,
        status=SegmentStatus.COMPLETE,
        created_at=now,
        ffprobe_verified=True,
    )


def _make_manifest(channel_id: str = "rts1", date_str: str = "2026-04-01") -> DailyManifest:
    now = datetime.now(timezone.utc)
    seg1 = _make_segment_entry("010426-000000.mp4", _utcdt(2026, 3, 31, 22, 0, 0))
    seg2 = _make_segment_entry("010426-000500.mp4", _utcdt(2026, 3, 31, 22, 5, 0))
    return DailyManifest(
        channel_id=channel_id,
        date=date_str,
        timezone="Europe/Belgrade",
        segment_duration_target=300,
        segments=[seg1, seg2],
        gaps=[],
        updated_at=now,
    )


def test_save_and_load_manifest(tmp_manifests):
    manifest = _make_manifest()
    save_manifest(manifest, tmp_manifests)
    loaded = load_manifest("rts1", "2026-04-01", tmp_manifests)
    assert loaded is not None
    assert loaded.channel_id == "rts1"
    assert loaded.date == "2026-04-01"
    assert len(loaded.segments) == 2
    assert loaded.segments[0].filename == "010426-000000.mp4"


def test_load_manifest_missing_returns_none(tmp_manifests):
    result = load_manifest("rts1", "2099-01-01", tmp_manifests)
    assert result is None


def test_save_manifest_atomic(tmp_manifests):
    manifest = _make_manifest()
    save_manifest(manifest, tmp_manifests)
    path = tmp_manifests / "rts1" / "2026-04-01.json"
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()


def test_manifest_json_is_human_readable(tmp_manifests):
    manifest = _make_manifest()
    save_manifest(manifest, tmp_manifests)
    path = tmp_manifests / "rts1" / "2026-04-01.json"
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed["channel_id"] == "rts1"
    assert isinstance(parsed["segments"], list)
    assert "\n" in raw


def test_manifest_overwrites_without_corruption(tmp_manifests):
    m1 = _make_manifest()
    save_manifest(m1, tmp_manifests)
    m2 = _make_manifest()
    m2.segments = m2.segments[:1]
    save_manifest(m2, tmp_manifests)
    loaded = load_manifest("rts1", "2026-04-01", tmp_manifests)
    assert len(loaded.segments) == 1


# ---------------------------------------------------------------------------
# _compute_gaps
# ---------------------------------------------------------------------------

def test_compute_gaps_no_gap():
    s1 = _make_segment_entry("a.mp4", _utcdt(2026, 4, 1, 0, 0, 0), 300)
    s2 = _make_segment_entry("b.mp4", _utcdt(2026, 4, 1, 0, 5, 0), 300)
    assert _compute_gaps([s1, s2], tolerance_seconds=10.0) == []


def test_compute_gaps_with_gap():
    s1 = _make_segment_entry("a.mp4", _utcdt(2026, 4, 1, 0, 0, 0), 300)
    s2 = _make_segment_entry("b.mp4", _utcdt(2026, 4, 1, 0, 6, 0), 300)  # 60s gap
    gaps = _compute_gaps([s1, s2], tolerance_seconds=10.0)
    assert len(gaps) == 1
    assert gaps[0].gap_seconds == pytest.approx(60.0)


def test_compute_gaps_below_tolerance():
    s1 = _make_segment_entry("a.mp4", _utcdt(2026, 4, 1, 0, 0, 0), 300)
    s2 = _make_segment_entry("b.mp4", _utcdt(2026, 4, 1, 0, 5, 5), 300)  # 5s gap
    assert _compute_gaps([s1, s2], tolerance_seconds=10.0) == []


def test_compute_gaps_multiple_gaps():
    s1 = _make_segment_entry("a.mp4", _utcdt(2026, 4, 1, 0, 0, 0), 300)
    s2 = _make_segment_entry("b.mp4", _utcdt(2026, 4, 1, 0, 6, 0), 300)
    s3 = _make_segment_entry("c.mp4", _utcdt(2026, 4, 1, 0, 13, 0), 300)
    assert len(_compute_gaps([s1, s2, s3], tolerance_seconds=10.0)) == 2


def test_compute_gaps_single_segment():
    s1 = _make_segment_entry("a.mp4", _utcdt(2026, 4, 1, 0, 0, 0), 300)
    assert _compute_gaps([s1], tolerance_seconds=10.0) == []


def test_compute_gaps_unordered_input():
    s1 = _make_segment_entry("a.mp4", _utcdt(2026, 4, 1, 0, 0, 0), 300)
    s2 = _make_segment_entry("b.mp4", _utcdt(2026, 4, 1, 0, 6, 0), 300)
    gaps = _compute_gaps([s2, s1], tolerance_seconds=10.0)  # reversed
    assert len(gaps) == 1
    assert gaps[0].gap_seconds == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# register_segment
# ---------------------------------------------------------------------------

def test_register_segment_creates_db_record(tmp_manifests, db_session, tmp_path):
    _seed_channel(db_session)
    config = _make_channel_config()
    seg_file = tmp_path / "010426-000000.mp4"
    seg_file.write_bytes(b"\x00" * 1000)

    with patch("app.services.manifest_service.ffprobe_duration", return_value=300.0), \
         patch("app.services.manifest_service.get_settings") as mock_settings:
        mock_settings.return_value.manifests_dir = tmp_manifests
        mock_settings.return_value.manifest_gap_tolerance_seconds = 10.0
        record = register_segment("rts1", seg_file, config, db_session)

    assert record is not None
    assert record.filename == "010426-000000.mp4"
    assert record.duration_seconds == 300.0
    assert record.ffprobe_verified is True
    assert record.manifest_date is not None


def test_register_segment_creates_manifest_file(tmp_manifests, db_session, tmp_path):
    _seed_channel(db_session)
    config = _make_channel_config()
    seg_file = tmp_path / "010426-000000.mp4"
    seg_file.write_bytes(b"\x00" * 1000)

    with patch("app.services.manifest_service.ffprobe_duration", return_value=300.0), \
         patch("app.services.manifest_service.get_settings") as mock_settings:
        mock_settings.return_value.manifests_dir = tmp_manifests
        mock_settings.return_value.manifest_gap_tolerance_seconds = 10.0
        register_segment("rts1", seg_file, config, db_session)

    json_files = list(tmp_manifests.glob("rts1/*.json"))
    assert len(json_files) == 1


def test_register_segment_idempotent(tmp_manifests, db_session, tmp_path):
    _seed_channel(db_session)
    config = _make_channel_config()
    seg_file = tmp_path / "010426-000000.mp4"
    seg_file.write_bytes(b"\x00" * 1000)

    with patch("app.services.manifest_service.ffprobe_duration", return_value=300.0), \
         patch("app.services.manifest_service.get_settings") as mock_settings:
        mock_settings.return_value.manifests_dir = tmp_manifests
        mock_settings.return_value.manifest_gap_tolerance_seconds = 10.0
        r1 = register_segment("rts1", seg_file, config, db_session)
        r2 = register_segment("rts1", seg_file, config, db_session)

    assert r1 is not None
    assert r2 is not None
    count = db_session.query(SegmentRecord).filter_by(filename="010426-000000.mp4").count()
    assert count == 1


def test_register_segment_invalid_filename_returns_none(tmp_manifests, db_session, tmp_path):
    _seed_channel(db_session)
    config = _make_channel_config()
    seg_file = tmp_path / "invalid_name.mp4"
    seg_file.write_bytes(b"\x00" * 1000)

    with patch("app.services.manifest_service.get_settings") as mock_settings:
        mock_settings.return_value.manifests_dir = tmp_manifests
        mock_settings.return_value.manifest_gap_tolerance_seconds = 10.0
        result = register_segment("rts1", seg_file, config, db_session)

    assert result is None


def test_register_segment_ffprobe_unavailable_uses_config_duration(
    tmp_manifests, db_session, tmp_path
):
    _seed_channel(db_session)
    config = _make_channel_config()
    seg_file = tmp_path / "010426-000000.mp4"
    seg_file.write_bytes(b"\x00" * 1000)

    with patch("app.services.manifest_service.ffprobe_duration", return_value=None), \
         patch("app.services.manifest_service.get_settings") as mock_settings:
        mock_settings.return_value.manifests_dir = tmp_manifests
        mock_settings.return_value.manifest_gap_tolerance_seconds = 10.0
        record = register_segment("rts1", seg_file, config, db_session)

    assert record is not None
    assert record.ffprobe_verified is False
    assert record.duration_seconds == 300.0


def test_register_segment_detects_gap(tmp_manifests, db_session, tmp_path):
    """Two segments with a 2-minute gap should produce one ManifestGap row."""
    _seed_channel(db_session)
    config = _make_channel_config()

    seg1 = tmp_path / "010426-000000.mp4"  # April 1 00:00 Belgrade = March 31 22:00 UTC
    seg1.write_bytes(b"\x00" * 1000)
    seg2 = tmp_path / "010426-000700.mp4"  # April 1 00:07 Belgrade (2-min gap after 5-min seg)
    seg2.write_bytes(b"\x00" * 1000)

    with patch("app.services.manifest_service.ffprobe_duration", return_value=300.0), \
         patch("app.services.manifest_service.get_settings") as mock_settings:
        mock_settings.return_value.manifests_dir = tmp_manifests
        mock_settings.return_value.manifest_gap_tolerance_seconds = 10.0
        register_segment("rts1", seg1, config, db_session)
        register_segment("rts1", seg2, config, db_session)

    gaps = db_session.query(ManifestGap).filter_by(channel_id="rts1").all()
    assert len(gaps) == 1
    assert gaps[0].gap_seconds == pytest.approx(120.0)


# ---------------------------------------------------------------------------
# resolve_export_range
# ---------------------------------------------------------------------------

def _insert_segment(
    db: Session, channel_id: str, filename: str, start: datetime, duration: float = 300.0,
    manifest_date: str = "2026-04-01",
) -> SegmentRecord:
    """Insert with naive datetime (SQLite strips tz on round-trip)."""
    naive_start = start.replace(tzinfo=None) if start.tzinfo else start
    rec = SegmentRecord(
        channel_id=channel_id,
        filename=filename,
        path=f"/tmp/{filename}",
        start_time=naive_start,
        end_time=naive_start + timedelta(seconds=duration),
        duration_seconds=duration,
        size_bytes=10_000_000,
        status="complete",
        ffprobe_verified=True,
        manifest_date=manifest_date,
    )
    db.add(rec)
    db.commit()
    return rec


def test_resolve_range_returns_overlapping_segments(db_session):
    _seed_channel(db_session)
    _insert_segment(db_session, "rts1", "a.mp4", _utcdt(2026, 4, 1, 14, 0, 0))
    _insert_segment(db_session, "rts1", "b.mp4", _utcdt(2026, 4, 1, 14, 5, 0))
    _insert_segment(db_session, "rts1", "c.mp4", _utcdt(2026, 4, 1, 14, 10, 0))

    req = ResolveRangeRequest(date="2026-04-01", in_time="14:03:00", out_time="14:12:00")
    result = resolve_export_range("rts1", req, db_session)

    assert result.channel_id == "rts1"
    assert len(result.segments) == 3
    assert result.segments[0].filename == "a.mp4"


def test_resolve_range_first_segment_offset(db_session):
    _seed_channel(db_session)
    _insert_segment(db_session, "rts1", "a.mp4", _utcdt(2026, 4, 1, 14, 0, 0))

    req = ResolveRangeRequest(date="2026-04-01", in_time="14:02:30", out_time="14:04:00")
    result = resolve_export_range("rts1", req, db_session)

    assert result.first_segment_offset_seconds == pytest.approx(150.0)


def test_resolve_range_export_duration(db_session):
    _seed_channel(db_session)
    _insert_segment(db_session, "rts1", "a.mp4", _utcdt(2026, 4, 1, 14, 0, 0))
    _insert_segment(db_session, "rts1", "b.mp4", _utcdt(2026, 4, 1, 14, 5, 0))

    req = ResolveRangeRequest(date="2026-04-01", in_time="14:01:00", out_time="14:08:00")
    result = resolve_export_range("rts1", req, db_session)

    assert result.export_duration_seconds == pytest.approx(7 * 60.0)


def test_resolve_range_no_segments(db_session):
    _seed_channel(db_session)
    req = ResolveRangeRequest(date="2026-04-01", in_time="14:00:00", out_time="14:05:00")
    result = resolve_export_range("rts1", req, db_session)
    assert result.segments == []
    assert result.has_gaps is False


def test_resolve_range_detects_gap_in_range(db_session):
    _seed_channel(db_session)
    _insert_segment(db_session, "rts1", "a.mp4", _utcdt(2026, 4, 1, 14, 0, 0))
    _insert_segment(db_session, "rts1", "b.mp4", _utcdt(2026, 4, 1, 14, 15, 0))  # 10-min gap

    req = ResolveRangeRequest(date="2026-04-01", in_time="14:02:00", out_time="14:17:00")
    result = resolve_export_range("rts1", req, db_session)

    assert result.has_gaps is True
    assert len(result.gaps) == 1
    assert result.gaps[0].gap_seconds == pytest.approx(10 * 60.0)


def test_resolve_range_invalid_time_raises(db_session):
    _seed_channel(db_session)
    req = ResolveRangeRequest(date="2026-04-01", in_time="14:00:00", out_time="13:00:00")
    with pytest.raises(ValueError, match="out_time must be after in_time"):
        resolve_export_range("rts1", req, db_session)


def test_resolve_range_bad_date_format_raises(db_session):
    _seed_channel(db_session)
    req = ResolveRangeRequest(date="not-a-date", in_time="14:00:00", out_time="15:00:00")
    with pytest.raises(ValueError):
        resolve_export_range("rts1", req, db_session)


# ---------------------------------------------------------------------------
# Schema models
# ---------------------------------------------------------------------------

def test_segment_status_values():
    assert SegmentStatus.COMPLETE == "complete"
    assert SegmentStatus.PARTIAL == "partial"
    assert SegmentStatus.ERROR == "error"


def test_daily_manifest_schema():
    now = datetime.now(timezone.utc)
    seg = _make_segment_entry("a.mp4", _utcdt(2026, 4, 1, 14, 0, 0))
    m = DailyManifest(
        channel_id="rts1",
        date="2026-04-01",
        timezone="Europe/Belgrade",
        segment_duration_target=300,
        segments=[seg],
        gaps=[],
        updated_at=now,
    )
    assert m.channel_id == "rts1"
    assert len(m.segments) == 1
    assert m.segment_duration_target == 300


def test_gap_entry_schema():
    g = GapEntry(
        gap_start=_utcdt(2026, 4, 1, 14, 5, 0),
        gap_end=_utcdt(2026, 4, 1, 14, 7, 0),
        gap_seconds=120.0,
    )
    assert g.gap_seconds == 120.0


def test_resolve_range_request_schema():
    req = ResolveRangeRequest(date="2026-04-01", in_time="14:05:30", out_time="14:22:10")
    assert req.date == "2026-04-01"
    assert req.in_time == "14:05:30"


def test_channel_config_timezone_default():
    config = _make_channel_config()
    assert config.timezone == "Europe/Belgrade"


def test_channel_config_timezone_override():
    config = ChannelConfig(
        id="test",
        name="Test",
        display_name="Test",
        timezone="UTC",
        paths={"record_dir": "/a", "chunks_dir": "/b", "final_dir": "/c"},
    )
    assert config.timezone == "UTC"


# ---------------------------------------------------------------------------
# API endpoints (minimal test app — no full lifespan)
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client(tmp_manifests, in_memory_engine):
    """
    Minimal FastAPI test app with only the manifests router, using the
    in-memory engine via StaticPool.  Avoids full app lifespan.
    """
    from app.api.v1.manifests import router as manifests_router
    from app.api.v1.deps import get_current_user
    from app.db.models import User

    test_app = FastAPI()
    test_app.include_router(manifests_router, prefix="/api/v1")

    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=in_memory_engine)

    def _override_db():
        with TestSessionLocal() as session:
            yield session

    def _override_auth():
        return User(id=1, username="testadmin", password_hash="x", role="admin", is_active=True)

    test_app.dependency_overrides[get_db] = _override_db
    test_app.dependency_overrides[get_current_user] = _override_auth

    with TestSessionLocal() as db:
        config = _make_channel_config()
        ch = Channel(
            id="rts1", name="RTS1", display_name="RTS1 Test",
            enabled=True, config_json=config.model_dump_json(),
        )
        db.add(ch)
        db.commit()

    client = TestClient(test_app, raise_server_exceptions=True)
    yield client, TestSessionLocal, tmp_manifests
    test_app.dependency_overrides.clear()


def test_get_manifest_not_found(api_client):
    client, _, _ = api_client
    resp = client.get("/api/v1/channels/rts1/manifests/2099-01-01")
    assert resp.status_code == 404


def test_get_manifest_returns_manifest(api_client):
    client, _, tmp_manifests = api_client
    manifest = _make_manifest()
    save_manifest(manifest, tmp_manifests)
    with patch("app.api.v1.manifests.get_settings") as mock_settings:
        mock_settings.return_value.manifests_dir = tmp_manifests
        resp = client.get("/api/v1/channels/rts1/manifests/2026-04-01")
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel_id"] == "rts1"
    assert len(body["segments"]) == 2


def test_get_segments_empty(api_client):
    client, _, _ = api_client
    resp = client.get("/api/v1/channels/rts1/segments?date=2026-04-01")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_segments_with_data(api_client):
    client, SessionLocal, _ = api_client
    with SessionLocal() as db:
        _insert_segment(db, "rts1", "a.mp4", _utcdt(2026, 4, 1, 14, 0, 0))
    resp = client.get("/api/v1/channels/rts1/segments?date=2026-04-01")
    assert resp.status_code == 200
    segments = resp.json()
    assert len(segments) == 1
    assert segments[0]["filename"] == "a.mp4"


def test_resolve_range_api(api_client):
    client, SessionLocal, _ = api_client
    with SessionLocal() as db:
        _insert_segment(db, "rts1", "a.mp4", _utcdt(2026, 4, 1, 14, 0, 0))
        _insert_segment(db, "rts1", "b.mp4", _utcdt(2026, 4, 1, 14, 5, 0))
    resp = client.post(
        "/api/v1/channels/rts1/exports/resolve-range",
        json={"date": "2026-04-01", "in_time": "14:02:00", "out_time": "14:07:00"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel_id"] == "rts1"
    assert len(body["segments"]) == 2
    assert body["has_gaps"] is False


def test_resolve_range_api_channel_not_found(api_client):
    client, _, _ = api_client
    resp = client.post(
        "/api/v1/channels/nonexistent/exports/resolve-range",
        json={"date": "2026-04-01", "in_time": "14:00:00", "out_time": "15:00:00"},
    )
    assert resp.status_code == 404


def test_resolve_range_api_invalid_times(api_client):
    client, _, _ = api_client
    resp = client.post(
        "/api/v1/channels/rts1/exports/resolve-range",
        json={"date": "2026-04-01", "in_time": "15:00:00", "out_time": "14:00:00"},
    )
    assert resp.status_code == 400


def test_get_manifest_channel_not_found(api_client):
    client, _, _ = api_client
    resp = client.get("/api/v1/channels/nosuchchannel/manifests/2026-04-01")
    assert resp.status_code == 404


def test_get_segments_channel_not_found(api_client):
    client, _, _ = api_client
    resp = client.get("/api/v1/channels/nosuchchannel/segments?date=2026-04-01")
    assert resp.status_code == 404
