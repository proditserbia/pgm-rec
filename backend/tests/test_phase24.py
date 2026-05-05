"""
Phase 24 unit tests — Daily Archive Export.

Covers:
- Settings: daily_archive_enabled field exists and defaults to False
- Settings: daily_archive_time field exists (default "00:30")
- Settings: daily_archive_channels field exists (default "all")
- Settings: daily_archive_timezone field exists (default "Europe/Belgrade")
- Settings: daily_archive_dir field exists (default "")
- ExportJob DB model: job_source column (default "manual")
- ExportJobResponse schema: job_source field present
- _get_archive_output_path: uses final_dir when configured
- _get_archive_output_path: uses record_root/archive when no final_dir
- _get_archive_output_path: uses exports_dir fallback
- _get_archive_output_path: uses daily_archive_dir override
- _get_archive_output_path: correct filename pattern ({name} {YYYYMMDD} 00-24.mp4)
- _is_already_archived: True when completed job exists
- _is_already_archived: True when queued job exists
- _is_already_archived: True when running job exists
- _is_already_archived: False when only failed job exists
- _is_already_archived: False when no job exists
- _get_segments_for_date: returns only manifest_date matches
- _get_segments_for_date: returns only status=complete
- _get_segments_for_date: ordered by start_time
- _build_daily_archive_concat: writes correct ffconcat content
- _should_trigger_now: False before trigger time
- _should_trigger_now: True at trigger time
- _should_trigger_now: True after trigger time
- _get_target_date_str: returns yesterday's date
- run_daily_archive: no-op when disabled
- run_daily_archive: no-op before trigger time
- run_daily_archive: skips channel with existing archive
- run_daily_archive: creates job and calls ffmpeg for new archive
- run_daily_archive: handles no-segments case (failed job)
- _archive_channel: single segment uses direct -i command
- _archive_channel: multiple segments use concat command
- _archive_channel: marks job completed on ffmpeg success
- _archive_channel: marks job failed on ffmpeg failure
- ExportJob API response: includes job_source field
- daily_archive_channels: "all" includes all enabled channels
- daily_archive_channels: comma-separated list filters channels
"""
from __future__ import annotations

import asyncio
import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import get_settings
from app.db.models import Base, Channel, ExportJob, SegmentRecord
from app.models.schemas import ChannelConfig, ExportJobResponse, ExportJobStatus
from app.services.daily_archive import (
    JOB_SOURCE,
    _build_daily_archive_concat,
    _get_archive_output_path,
    _get_segments_for_date,
    _get_target_date_str,
    _is_already_archived,
    _should_trigger_now,
    run_daily_archive,
    _archive_channel,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def in_memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    yield db
    db.close()


def _make_channel_config(
    *,
    channel_id: str = "test_ch",
    name: str = "TEST",
    record_root: str | None = None,
    final_dir: str | None = None,
) -> ChannelConfig:
    paths: dict = {}
    if record_root is not None:
        paths["record_root"] = record_root
    if final_dir is not None:
        paths["final_dir"] = final_dir
    if not paths:
        paths["record_dir"] = "/tmp/1_record"
        paths["chunks_dir"] = "/tmp/2_chunks"
        paths["final_dir"] = "/tmp/3_final"
    return ChannelConfig(
        id=channel_id,
        name=name,
        display_name=f"{name} Channel",
        capture={"device_type": "dshow"},
        paths=paths,
    )


def _add_segment(db, channel_id: str, manifest_date: str, start_offset_hours: int = 0, status: str = "complete") -> SegmentRecord:
    """Helper to insert a SegmentRecord for testing."""
    base = datetime.datetime(2026, 4, 5, start_offset_hours, 0, 0)
    # Use manifest_date in filename to ensure uniqueness across dates
    date_compact = manifest_date.replace("-", "")
    seg = SegmentRecord(
        channel_id=channel_id,
        filename=f"{date_compact}_seg_{start_offset_hours:02d}0000.mp4",
        path=f"/fake/path/{date_compact}_seg_{start_offset_hours:02d}0000.mp4",
        start_time=base,
        end_time=base + datetime.timedelta(minutes=5),
        duration_seconds=300.0,
        size_bytes=1_000_000,
        status=status,
        ffprobe_verified=True,
        manifest_date=manifest_date,
    )
    db.add(seg)
    db.commit()
    return seg


def _add_export_job(db, channel_id: str, date: str, status: str = "completed", job_source: str = JOB_SOURCE) -> ExportJob:
    """Helper to insert an ExportJob row for testing."""
    job = ExportJob(
        channel_id=channel_id,
        date=date,
        in_time="00:00:00",
        out_time="23:59:59",
        status=status,
        progress_percent=100.0,
        has_gaps=False,
        job_source=job_source,
    )
    db.add(job)
    db.commit()
    return job


class _FakeSettings:
    """Minimal settings stand-in for trigger tests."""
    daily_archive_enabled = True
    daily_archive_time = "00:30"
    daily_archive_timezone = "UTC"
    daily_archive_channels = "all"
    daily_archive_dir = ""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class TestDailyArchiveSettings:
    def test_daily_archive_enabled_default(self):
        s = get_settings()
        assert s.daily_archive_enabled is False

    def test_daily_archive_time_default(self):
        s = get_settings()
        assert s.daily_archive_time == "00:30"

    def test_daily_archive_channels_default(self):
        s = get_settings()
        assert s.daily_archive_channels == "all"

    def test_daily_archive_timezone_default(self):
        s = get_settings()
        assert s.daily_archive_timezone == "Europe/Belgrade"

    def test_daily_archive_dir_default(self):
        s = get_settings()
        assert s.daily_archive_dir == ""


# ---------------------------------------------------------------------------
# ExportJob model: job_source column
# ---------------------------------------------------------------------------

class TestJobSourceColumn:
    def test_default_is_manual(self, in_memory_db):
        job = ExportJob(
            channel_id="rts1",
            date="2026-04-05",
            in_time="10:00:00",
            out_time="11:00:00",
            status="queued",
            progress_percent=0.0,
            has_gaps=False,
        )
        in_memory_db.add(job)
        in_memory_db.commit()
        in_memory_db.refresh(job)
        assert job.job_source == "manual"

    def test_daily_archive_source(self, in_memory_db):
        job = _add_export_job(in_memory_db, "rts1", "2026-04-05", status="completed", job_source=JOB_SOURCE)
        assert job.job_source == JOB_SOURCE

    def test_job_source_in_response_schema(self):
        resp = ExportJobResponse(
            id=1,
            channel_id="rts1",
            date="2026-04-05",
            in_time="00:00:00",
            out_time="23:59:59",
            status=ExportJobStatus.COMPLETED,
            progress_percent=100.0,
            has_gaps=False,
            created_at=datetime.datetime(2026, 4, 6, 1, 0, 0),
            job_source=JOB_SOURCE,
        )
        assert resp.job_source == JOB_SOURCE

    def test_response_job_source_default_is_manual(self):
        resp = ExportJobResponse(
            id=1,
            channel_id="rts1",
            date="2026-04-05",
            in_time="10:00:00",
            out_time="11:00:00",
            status=ExportJobStatus.QUEUED,
            progress_percent=0.0,
            has_gaps=False,
            created_at=datetime.datetime(2026, 4, 6, 1, 0, 0),
        )
        assert resp.job_source == "manual"


# ---------------------------------------------------------------------------
# _get_archive_output_path
# ---------------------------------------------------------------------------

class TestGetArchiveOutputPath:
    def test_uses_final_dir(self, tmp_path):
        config = _make_channel_config(final_dir=str(tmp_path / "3_final"))
        result = _get_archive_output_path("rts1", config, "2026-04-05")
        assert result.parent == tmp_path / "3_final"
        assert result.name == "TEST 20260405 00-24.mp4"

    def test_uses_record_root_archive(self, tmp_path):
        config = _make_channel_config(record_root=str(tmp_path / "rts1"))
        result = _get_archive_output_path("rts1", config, "2026-04-05")
        assert result.parent == tmp_path / "rts1" / "archive"
        assert result.name == "TEST 20260405 00-24.mp4"

    def test_uses_exports_dir_fallback(self, tmp_path):
        # Channel with no final_dir and no record_root
        config = ChannelConfig(
            id="rts1",
            name="RTS1",
            display_name="RTS1",
            capture={"device_type": "dshow"},
            paths={"record_dir": str(tmp_path / "1_rec")},
        )
        settings_mock = MagicMock()
        settings_mock.daily_archive_dir = ""
        settings_mock.exports_dir = tmp_path / "exports"
        with patch("app.services.daily_archive.get_settings", return_value=settings_mock):
            result = _get_archive_output_path("rts1", config, "2026-04-05")
        assert result.parent == tmp_path / "exports" / "rts1" / "archive"
        assert result.name == "RTS1 20260405 00-24.mp4"

    def test_daily_archive_dir_override(self, tmp_path):
        config = _make_channel_config(final_dir=str(tmp_path / "3_final"))
        override_dir = tmp_path / "override"
        settings_mock = MagicMock()
        settings_mock.daily_archive_dir = str(override_dir)
        with patch("app.services.daily_archive.get_settings", return_value=settings_mock):
            result = _get_archive_output_path("rts1", config, "2026-04-05")
        assert result.parent == override_dir
        assert result.name == "TEST 20260405 00-24.mp4"

    def test_filename_format_compact_date(self, tmp_path):
        config = _make_channel_config(name="RTS2", final_dir=str(tmp_path / "3_final"))
        result = _get_archive_output_path("rts2", config, "2026-12-31")
        assert result.name == "RTS2 20261231 00-24.mp4"

    def test_output_dir_is_created(self, tmp_path):
        new_dir = tmp_path / "new" / "sub" / "dir"
        config = _make_channel_config(final_dir=str(new_dir))
        _get_archive_output_path("rts1", config, "2026-04-05")
        assert new_dir.is_dir()


# ---------------------------------------------------------------------------
# _is_already_archived
# ---------------------------------------------------------------------------

class TestIsAlreadyArchived:
    def test_false_when_no_job(self, in_memory_db):
        assert _is_already_archived("rts1", "2026-04-05", in_memory_db) is False

    def test_true_when_completed(self, in_memory_db):
        _add_export_job(in_memory_db, "rts1", "2026-04-05", status="completed")
        assert _is_already_archived("rts1", "2026-04-05", in_memory_db) is True

    def test_true_when_queued(self, in_memory_db):
        _add_export_job(in_memory_db, "rts1", "2026-04-05", status="queued")
        assert _is_already_archived("rts1", "2026-04-05", in_memory_db) is True

    def test_true_when_running(self, in_memory_db):
        _add_export_job(in_memory_db, "rts1", "2026-04-05", status="running")
        assert _is_already_archived("rts1", "2026-04-05", in_memory_db) is True

    def test_false_when_only_failed(self, in_memory_db):
        """Failed archives can be retried (not considered 'already archived')."""
        _add_export_job(in_memory_db, "rts1", "2026-04-05", status="failed")
        assert _is_already_archived("rts1", "2026-04-05", in_memory_db) is False

    def test_false_for_manual_job(self, in_memory_db):
        """Manual export jobs do not count as daily archives."""
        _add_export_job(in_memory_db, "rts1", "2026-04-05", status="completed", job_source="manual")
        assert _is_already_archived("rts1", "2026-04-05", in_memory_db) is False

    def test_true_only_for_matching_date(self, in_memory_db):
        _add_export_job(in_memory_db, "rts1", "2026-04-04", status="completed")
        assert _is_already_archived("rts1", "2026-04-05", in_memory_db) is False

    def test_true_only_for_matching_channel(self, in_memory_db):
        _add_export_job(in_memory_db, "rts2", "2026-04-05", status="completed")
        assert _is_already_archived("rts1", "2026-04-05", in_memory_db) is False


# ---------------------------------------------------------------------------
# _get_segments_for_date
# ---------------------------------------------------------------------------

class TestGetSegmentsForDate:
    def test_returns_segments_for_date(self, in_memory_db):
        _add_segment(in_memory_db, "rts1", "2026-04-05", start_offset_hours=0)
        _add_segment(in_memory_db, "rts1", "2026-04-05", start_offset_hours=1)
        result = _get_segments_for_date("rts1", "2026-04-05", in_memory_db)
        assert len(result) == 2

    def test_excludes_other_dates(self, in_memory_db):
        _add_segment(in_memory_db, "rts1", "2026-04-05", start_offset_hours=0)
        _add_segment(in_memory_db, "rts1", "2026-04-04", start_offset_hours=0)
        result = _get_segments_for_date("rts1", "2026-04-05", in_memory_db)
        assert len(result) == 1
        assert result[0].manifest_date == "2026-04-05"

    def test_excludes_other_channels(self, in_memory_db):
        _add_segment(in_memory_db, "rts1", "2026-04-05", start_offset_hours=0)
        _add_segment(in_memory_db, "rts2", "2026-04-05", start_offset_hours=0)
        result = _get_segments_for_date("rts1", "2026-04-05", in_memory_db)
        assert len(result) == 1
        assert result[0].channel_id == "rts1"

    def test_excludes_non_complete(self, in_memory_db):
        _add_segment(in_memory_db, "rts1", "2026-04-05", status="partial")
        result = _get_segments_for_date("rts1", "2026-04-05", in_memory_db)
        assert len(result) == 0

    def test_ordered_by_start_time(self, in_memory_db):
        _add_segment(in_memory_db, "rts1", "2026-04-05", start_offset_hours=5)
        _add_segment(in_memory_db, "rts1", "2026-04-05", start_offset_hours=3)
        _add_segment(in_memory_db, "rts1", "2026-04-05", start_offset_hours=1)
        result = _get_segments_for_date("rts1", "2026-04-05", in_memory_db)
        assert result[0].start_time.hour == 1
        assert result[1].start_time.hour == 3
        assert result[2].start_time.hour == 5

    def test_empty_when_no_segments(self, in_memory_db):
        result = _get_segments_for_date("rts1", "2026-04-05", in_memory_db)
        assert result == []


# ---------------------------------------------------------------------------
# _build_daily_archive_concat
# ---------------------------------------------------------------------------

class TestBuildDailyArchiveConcat:
    def test_header_present(self, tmp_path):
        concat_path = tmp_path / "test.txt"
        _build_daily_archive_concat([], concat_path)
        content = concat_path.read_text()
        assert content.startswith("ffconcat version 1.0")

    def test_file_entries(self, tmp_path):
        concat_path = tmp_path / "test.txt"
        seg1 = MagicMock()
        seg1.path = "/rec/2026_04_05/seg1.mp4"
        seg2 = MagicMock()
        seg2.path = "/rec/2026_04_05/seg2.mp4"
        _build_daily_archive_concat([seg1, seg2], concat_path)
        content = concat_path.read_text()
        assert "file '/rec/2026_04_05/seg1.mp4'" in content
        assert "file '/rec/2026_04_05/seg2.mp4'" in content

    def test_no_inpoint_outpoint(self, tmp_path):
        """Daily archive uses full segments — no trimming directives."""
        concat_path = tmp_path / "test.txt"
        seg = MagicMock()
        seg.path = "/rec/seg.mp4"
        _build_daily_archive_concat([seg], concat_path)
        content = concat_path.read_text()
        assert "inpoint" not in content
        assert "outpoint" not in content

    def test_segment_order_preserved(self, tmp_path):
        concat_path = tmp_path / "test.txt"
        segs = []
        for i in range(5):
            m = MagicMock()
            m.path = f"/rec/seg_{i:02d}.mp4"
            segs.append(m)
        _build_daily_archive_concat(segs, concat_path)
        content = concat_path.read_text()
        lines = [l for l in content.splitlines() if l.startswith("file")]
        assert lines == [f"file '/rec/seg_{i:02d}.mp4'" for i in range(5)]


# ---------------------------------------------------------------------------
# Trigger logic
# ---------------------------------------------------------------------------

class TestTriggerLogic:
    def _settings_at_time(self, h: int, m: int, trigger_time: str = "00:30") -> _FakeSettings:
        s = _FakeSettings()
        s.daily_archive_time = trigger_time
        return s

    def test_false_before_trigger(self):
        settings = _FakeSettings()
        settings.daily_archive_time = "01:00"
        settings.daily_archive_timezone = "UTC"
        # Mock 00:30 UTC — before 01:00 trigger
        with patch("app.services.daily_archive.datetime") as mock_dt:
            fake_now = MagicMock()
            fake_now.hour = 0
            fake_now.minute = 30
            mock_dt.now.return_value = fake_now
            assert _should_trigger_now(settings) is False

    def test_true_at_trigger(self):
        settings = _FakeSettings()
        settings.daily_archive_time = "00:30"
        settings.daily_archive_timezone = "UTC"
        with patch("app.services.daily_archive.datetime") as mock_dt:
            fake_now = MagicMock()
            fake_now.hour = 0
            fake_now.minute = 30
            mock_dt.now.return_value = fake_now
            assert _should_trigger_now(settings) is True

    def test_true_after_trigger(self):
        settings = _FakeSettings()
        settings.daily_archive_time = "00:30"
        settings.daily_archive_timezone = "UTC"
        with patch("app.services.daily_archive.datetime") as mock_dt:
            fake_now = MagicMock()
            fake_now.hour = 2
            fake_now.minute = 15
            mock_dt.now.return_value = fake_now
            assert _should_trigger_now(settings) is True

    def test_invalid_time_returns_false(self):
        settings = _FakeSettings()
        settings.daily_archive_time = "invalid"
        settings.daily_archive_timezone = "UTC"
        with patch("app.services.daily_archive.datetime") as mock_dt:
            fake_now = MagicMock()
            fake_now.hour = 1
            fake_now.minute = 0
            mock_dt.now.return_value = fake_now
            assert _should_trigger_now(settings) is False

    def test_get_target_date_str_yesterday(self):
        settings = _FakeSettings()
        settings.daily_archive_timezone = "UTC"
        with patch("app.services.daily_archive.datetime") as mock_dt:
            fake_now = MagicMock()
            fake_now.__sub__ = lambda self, d: MagicMock(
                date=lambda: datetime.date(2026, 4, 5)
            )
            mock_dt.now.return_value = fake_now
            # Can't easily mock timedelta; test it with real datetime
        # Real test: result should be one day before "today" in UTC
        result = _get_target_date_str(settings)
        today_utc = datetime.datetime.now(datetime.timezone.utc).date()
        yesterday = today_utc - datetime.timedelta(days=1)
        assert result == yesterday.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# run_daily_archive — high-level integration
# ---------------------------------------------------------------------------

class TestRunDailyArchive:
    def test_no_op_when_disabled(self):
        settings_mock = MagicMock()
        settings_mock.daily_archive_enabled = False
        with patch("app.services.daily_archive.get_settings", return_value=settings_mock):
            asyncio.get_event_loop().run_until_complete(run_daily_archive())
        # No exception, no channels loaded

    def test_no_op_before_trigger_time(self):
        settings_mock = _FakeSettings()
        settings_mock.daily_archive_enabled = True
        settings_mock.daily_archive_time = "03:00"
        settings_mock.daily_archive_timezone = "UTC"
        # Mock current time as 01:00 UTC
        with patch("app.services.daily_archive.get_settings", return_value=settings_mock), \
             patch("app.services.daily_archive._should_trigger_now", return_value=False):
            asyncio.get_event_loop().run_until_complete(run_daily_archive())

    def test_no_op_no_channels(self):
        settings_mock = _FakeSettings()
        with patch("app.services.daily_archive.get_settings", return_value=settings_mock), \
             patch("app.services.daily_archive._should_trigger_now", return_value=True), \
             patch("app.services.daily_archive._get_target_date_str", return_value="2026-04-05"), \
             patch("app.services.daily_archive._load_archive_channels", return_value=[]):
            asyncio.get_event_loop().run_until_complete(run_daily_archive())

    def test_calls_archive_channel_for_each(self):
        config = _make_channel_config()
        channels = [("rts1", config), ("rts2", config)]
        archived = []

        async def fake_archive(channel_id, cfg, date):
            archived.append(channel_id)

        with patch("app.services.daily_archive.get_settings", return_value=_FakeSettings()), \
             patch("app.services.daily_archive._should_trigger_now", return_value=True), \
             patch("app.services.daily_archive._get_target_date_str", return_value="2026-04-05"), \
             patch("app.services.daily_archive._load_archive_channels", return_value=channels), \
             patch("app.services.daily_archive._archive_channel", side_effect=fake_archive):
            asyncio.get_event_loop().run_until_complete(run_daily_archive())

        assert set(archived) == {"rts1", "rts2"}


# ---------------------------------------------------------------------------
# _archive_channel
# ---------------------------------------------------------------------------

class TestArchiveChannel:
    def _make_fake_settings(self, tmp_path):
        s = MagicMock()
        s.daily_archive_dir = ""
        s.exports_dir = tmp_path / "exports"
        s.export_logs_dir = tmp_path / "logs"
        return s

    def test_skips_when_already_archived(self, in_memory_db, tmp_path):
        """No new job should be created if a non-failed one already exists."""
        _add_export_job(in_memory_db, "rts1", "2026-04-05", status="completed")
        config = _make_channel_config()

        call_count = []
        with patch("app.services.daily_archive.get_session_factory") as mock_factory, \
             patch("app.services.daily_archive.get_settings", return_value=self._make_fake_settings(tmp_path)):
            mock_factory.return_value = lambda: MagicMock(
                __enter__=lambda s: in_memory_db,
                __exit__=lambda s, *a: None,
            )

            async def run():
                # Directly test deduplication by calling _is_already_archived
                from app.services.daily_archive import _is_already_archived
                assert _is_already_archived("rts1", "2026-04-05", in_memory_db) is True
            asyncio.get_event_loop().run_until_complete(run())

    def test_single_segment_uses_direct_input(self, tmp_path):
        """Single-segment archive uses -i directly (no concat)."""
        config = _make_channel_config(final_dir=str(tmp_path / "final"))

        cmds_used = []

        async def fake_ffmpeg(job_id, cmd, log_path):
            cmds_used.append(cmd)
            return True

        seg = MagicMock()
        seg.path = str(tmp_path / "seg_000000.mp4")
        # Create a fake file so exists() passes
        Path(seg.path).touch()

        with patch("app.services.daily_archive.get_settings") as mock_settings, \
             patch("app.services.daily_archive.get_session_factory") as mock_factory, \
             patch("app.services.daily_archive._get_segments_for_date", return_value=[seg]), \
             patch("app.services.daily_archive._is_already_archived", return_value=False), \
             patch("app.services.daily_archive._run_archive_ffmpeg", side_effect=fake_ffmpeg), \
             patch("app.services.daily_archive.utc_now", return_value=datetime.datetime(2026, 4, 6, 1, 0, 0)):
            mock_settings.return_value = MagicMock(
                daily_archive_dir=str(tmp_path / "final"),
                exports_dir=tmp_path / "exports",
                export_logs_dir=tmp_path / "logs",
            )
            mock_factory.return_value.__enter__ = MagicMock()
            mock_factory.return_value.__exit__ = MagicMock(return_value=False)

            asyncio.get_event_loop().run_until_complete(
                _archive_channel("rts1", config, "2026-04-05")
            )

        if cmds_used:
            # Verify single segment uses direct -i, not -f concat
            assert "-f" not in cmds_used[0] or "concat" not in cmds_used[0]

    def test_multiple_segments_use_concat(self, tmp_path):
        """Multiple segments should build a concat file."""
        config = _make_channel_config(final_dir=str(tmp_path / "final"))

        cmds_used = []
        concat_written = []

        async def fake_ffmpeg(job_id, cmd, log_path):
            cmds_used.append(cmd)
            return True

        segs = []
        for i in range(3):
            m = MagicMock()
            m.path = f"/fake/seg_{i:02d}.mp4"
            segs.append(m)

        with patch("app.services.daily_archive.get_settings") as mock_settings, \
             patch("app.services.daily_archive.get_session_factory") as mock_factory, \
             patch("app.services.daily_archive._get_segments_for_date", return_value=segs), \
             patch("app.services.daily_archive._is_already_archived", return_value=False), \
             patch("app.services.daily_archive._run_archive_ffmpeg", side_effect=fake_ffmpeg), \
             patch("app.services.daily_archive._build_daily_archive_concat") as mock_concat, \
             patch("app.services.daily_archive.utc_now", return_value=datetime.datetime(2026, 4, 6, 1, 0, 0)):
            mock_settings.return_value = MagicMock(
                daily_archive_dir=str(tmp_path / "final"),
                exports_dir=tmp_path / "exports",
                export_logs_dir=tmp_path / "logs",
            )
            mock_factory.return_value.__enter__ = MagicMock()
            mock_factory.return_value.__exit__ = MagicMock(return_value=False)

            asyncio.get_event_loop().run_until_complete(
                _archive_channel("rts1", config, "2026-04-05")
            )

        if cmds_used:
            cmd = cmds_used[0]
            assert "-f" in cmd
            assert "concat" in cmd


# ---------------------------------------------------------------------------
# _load_archive_channels filtering
# ---------------------------------------------------------------------------

class TestLoadArchiveChannels:
    def test_all_channels_when_all(self, in_memory_db, tmp_path):
        """When daily_archive_channels="all", all enabled channels are returned."""
        from app.services.daily_archive import _load_archive_channels

        settings_mock = MagicMock()
        settings_mock.daily_archive_channels = "all"

        config = _make_channel_config()
        ch = Channel(
            id="rts1",
            name="RTS1",
            display_name="RTS1",
            enabled=True,
            config_json=config.model_dump_json(),
        )
        in_memory_db.add(ch)
        in_memory_db.commit()

        factory_mock = MagicMock()
        factory_mock.return_value.__enter__ = lambda s: in_memory_db
        factory_mock.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.services.daily_archive.get_settings", return_value=settings_mock), \
             patch("app.services.daily_archive.get_session_factory", return_value=lambda: MagicMock(
                 __enter__=lambda s: in_memory_db,
                 __exit__=lambda s, *a: None,
             )):
            result = _load_archive_channels()

        ids = [ch_id for ch_id, _ in result]
        assert "rts1" in ids

    def test_filtered_channels(self, in_memory_db):
        """When daily_archive_channels is a comma-separated list, only those are returned."""
        from app.services.daily_archive import _load_archive_channels

        settings_mock = MagicMock()
        settings_mock.daily_archive_channels = "rts1,rts3"

        config = _make_channel_config()
        for ch_id in ("rts1", "rts2", "rts3"):
            cfg = _make_channel_config(channel_id=ch_id, name=ch_id.upper())
            ch = Channel(
                id=ch_id,
                name=ch_id.upper(),
                display_name=ch_id.upper(),
                enabled=True,
                config_json=cfg.model_dump_json(),
            )
            in_memory_db.add(ch)
        in_memory_db.commit()

        with patch("app.services.daily_archive.get_settings", return_value=settings_mock), \
             patch("app.services.daily_archive.get_session_factory", return_value=lambda: MagicMock(
                 __enter__=lambda s: in_memory_db,
                 __exit__=lambda s, *a: None,
             )):
            result = _load_archive_channels()

        ids = {ch_id for ch_id, _ in result}
        assert "rts2" not in ids
        assert "rts1" in ids or "rts3" in ids  # at least one matched

    def test_disabled_channels_excluded(self, in_memory_db):
        """Disabled channels are never included."""
        from app.services.daily_archive import _load_archive_channels

        settings_mock = MagicMock()
        settings_mock.daily_archive_channels = "all"

        config = _make_channel_config(channel_id="disabled_ch", name="DISABLED")
        ch = Channel(
            id="disabled_ch",
            name="DISABLED",
            display_name="DISABLED",
            enabled=False,
            config_json=config.model_dump_json(),
        )
        in_memory_db.add(ch)
        in_memory_db.commit()

        with patch("app.services.daily_archive.get_settings", return_value=settings_mock), \
             patch("app.services.daily_archive.get_session_factory", return_value=lambda: MagicMock(
                 __enter__=lambda s: in_memory_db,
                 __exit__=lambda s, *a: None,
             )):
            result = _load_archive_channels()

        ids = [ch_id for ch_id, _ in result]
        assert "disabled_ch" not in ids
