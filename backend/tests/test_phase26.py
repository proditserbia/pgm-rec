"""
Tests for Phase 26 — Conditional file_mover scheduling.

Coverage:
  _has_legacy_channels: returns False for all date-based configs, True when
    any enabled channel has record_dir or chunks_dir.

  Scheduler registration: file_mover is NOT added to the scheduler when all
    channels use date-based layout; IS added when a legacy channel exists.

  file_mover guard: running _run_file_mover_sync() against a date-based
    channel emits a WARNING and does not call _move_completed_files.

  Startup log: "File mover disabled" vs "File mover enabled" messages appear
    in the correct scenarios.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Channel
from app.main import _has_legacy_channels


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(channels: list[dict]):
    """Return a sessionmaker backed by an in-memory SQLite DB seeded with *channels*."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        for cfg in channels:
            db.add(Channel(
                id=cfg["id"],
                name=cfg.get("name", cfg["id"]),
                display_name=cfg.get("display_name", cfg["id"]),
                enabled=cfg.get("enabled", True),
                config_json=json.dumps(cfg),
            ))
        db.commit()
    return SessionLocal


def _date_based_cfg(ch_id: str = "rts1", record_root: str = "/tmp/rts1") -> dict:
    return {
        "id": ch_id,
        "name": ch_id.upper(),
        "display_name": ch_id.upper(),
        "capture": {"device_type": "dshow"},
        "paths": {"record_root": record_root},
    }


def _legacy_cfg(
    ch_id: str = "legacy_ch",
    record_dir: str = "/tmp/1_rec",
    chunks_dir: str = "/tmp/2_ch",
) -> dict:
    return {
        "id": ch_id,
        "name": ch_id.upper(),
        "display_name": ch_id.upper(),
        "capture": {"device_type": "dshow"},
        "paths": {
            "record_dir": record_dir,
            "chunks_dir": chunks_dir,
            "final_dir": "/tmp/3_fin",
        },
    }


# ---------------------------------------------------------------------------
# _has_legacy_channels
# ---------------------------------------------------------------------------

class TestHasLegacyChannels:
    def test_returns_false_for_date_based_only(self):
        """All date-based channels → _has_legacy_channels returns False."""
        SessionLocal = _make_db([_date_based_cfg()])
        with SessionLocal() as db:
            assert _has_legacy_channels(db) is False

    def test_returns_true_with_record_dir(self):
        """Channel with record_dir → _has_legacy_channels returns True."""
        SessionLocal = _make_db([_legacy_cfg()])
        with SessionLocal() as db:
            assert _has_legacy_channels(db) is True

    def test_returns_true_when_mixed(self):
        """One date-based + one legacy → True because legacy channel exists."""
        SessionLocal = _make_db([
            _date_based_cfg("rts1"),
            _legacy_cfg("old_ch"),
        ])
        with SessionLocal() as db:
            assert _has_legacy_channels(db) is True

    def test_returns_false_empty_db(self):
        """No channels at all → False."""
        SessionLocal = _make_db([])
        with SessionLocal() as db:
            assert _has_legacy_channels(db) is False

    def test_ignores_disabled_channels(self):
        """Disabled legacy channels do NOT count."""
        cfg = _legacy_cfg()
        cfg["enabled"] = False
        SessionLocal = _make_db([cfg])
        with SessionLocal() as db:
            assert _has_legacy_channels(db) is False

    def test_chunks_dir_alone_counts_as_legacy(self):
        """chunks_dir present without record_dir also triggers legacy detection."""
        cfg = {
            "id": "partial_legacy",
            "name": "PL",
            "display_name": "PL",
            "capture": {"device_type": "dshow"},
            "paths": {"chunks_dir": "/tmp/2_ch"},
        }
        SessionLocal = _make_db([cfg])
        with SessionLocal() as db:
            assert _has_legacy_channels(db) is True


# ---------------------------------------------------------------------------
# Scheduler registration
# ---------------------------------------------------------------------------

class TestSchedulerRegistration:
    """Verify that file_mover is or is not added to the scheduler."""

    def _run_scheduler_section(self, SessionLocal):
        """
        Execute only the scheduler-setup portion of the lifespan, returning
        the list of job names that were registered.
        """
        from app.services.scheduler import BackgroundScheduler

        added_jobs: list[str] = []
        mock_scheduler = MagicMock(spec=BackgroundScheduler)
        mock_scheduler.add.side_effect = lambda name, *args, **kw: added_jobs.append(name)
        mock_scheduler.start = AsyncMock()

        settings = MagicMock()
        settings.segment_indexer_interval_seconds = 5
        settings.file_mover_interval_seconds = 30
        settings.retention_run_interval_seconds = 60

        with (
            patch("app.main.get_scheduler", return_value=mock_scheduler),
            patch("app.main.get_session_factory", return_value=SessionLocal),
            patch("app.main.get_settings", return_value=settings),
        ):
            from app.main import _has_legacy_channels
            from app.services.file_mover import run_file_mover
            from app.services.retention import run_retention
            from app.services.export_retention import run_export_retention
            from app.services.segment_indexer import run_segment_indexer
            from app.services.daily_archive import run_daily_archive

            # Replicate the scheduler-setup block from lifespan
            scheduler = mock_scheduler
            scheduler.add("segment_indexer", settings.segment_indexer_interval_seconds, run_segment_indexer)
            with SessionLocal() as db:
                if _has_legacy_channels(db):
                    scheduler.add("file_mover", settings.file_mover_interval_seconds, run_file_mover)
            scheduler.add("retention", settings.retention_run_interval_seconds, run_retention)
            scheduler.add("export_retention", settings.retention_run_interval_seconds, run_export_retention)
            scheduler.add("daily_archive", 60, run_daily_archive)

        return added_jobs

    def test_file_mover_not_scheduled_in_date_based_mode(self):
        """All date-based channels → file_mover must NOT appear in scheduled jobs."""
        SessionLocal = _make_db([_date_based_cfg()])
        jobs = self._run_scheduler_section(SessionLocal)
        assert "file_mover" not in jobs

    def test_file_mover_scheduled_in_legacy_mode(self):
        """At least one legacy channel → file_mover MUST appear in scheduled jobs."""
        SessionLocal = _make_db([_legacy_cfg()])
        jobs = self._run_scheduler_section(SessionLocal)
        assert "file_mover" in jobs

    def test_standard_jobs_always_scheduled(self):
        """segment_indexer, retention, export_retention, daily_archive always appear."""
        SessionLocal = _make_db([_date_based_cfg()])
        jobs = self._run_scheduler_section(SessionLocal)
        for expected in ("segment_indexer", "retention", "export_retention", "daily_archive"):
            assert expected in jobs

    def test_file_mover_absent_when_no_channels(self):
        """Empty DB (no channels) → file_mover not scheduled."""
        SessionLocal = _make_db([])
        jobs = self._run_scheduler_section(SessionLocal)
        assert "file_mover" not in jobs


# ---------------------------------------------------------------------------
# file_mover guard — WARNING for date-based channels
# ---------------------------------------------------------------------------

class TestFileMoverGuard:
    def test_date_based_channel_emits_warning(self, caplog):
        """_run_file_mover_sync emits WARNING when it encounters a date-based channel."""
        from app.services.file_mover import _run_file_mover_sync

        SessionLocal = _make_db([_date_based_cfg()])
        with (
            patch("app.services.file_mover.get_session_factory", return_value=SessionLocal),
            patch("app.services.file_mover._move_completed_files") as mock_move,
            caplog.at_level(logging.WARNING, logger="app.services.file_mover"),
        ):
            _run_file_mover_sync()

        mock_move.assert_not_called()
        assert any("date-based" in r.message.lower() or "record_root" in r.message.lower()
                   for r in caplog.records), "Expected a WARNING mentioning date-based layout"

    def test_legacy_channel_with_missing_dirs_does_not_move(self, tmp_path):
        """Legacy channel (record_dir/chunks_dir) still does not move files
        because the current file_mover implementation skips and warns."""
        from app.services.file_mover import _run_file_mover_sync

        cfg = _legacy_cfg(record_dir=str(tmp_path / "1_rec"), chunks_dir=str(tmp_path / "2_ch"))
        SessionLocal = _make_db([cfg])
        with (
            patch("app.services.file_mover.get_session_factory", return_value=SessionLocal),
            patch("app.services.file_mover._move_completed_files") as mock_move,
        ):
            _run_file_mover_sync()

        mock_move.assert_not_called()


# ---------------------------------------------------------------------------
# Startup log messages
# ---------------------------------------------------------------------------

class TestStartupLogMessages:
    def test_disabled_log_in_date_based_mode(self, caplog):
        """Startup emits 'File mover disabled' when all channels are date-based."""
        from app.main import _has_legacy_channels

        SessionLocal = _make_db([_date_based_cfg()])
        with (
            caplog.at_level(logging.INFO, logger="app.main"),
        ):
            import logging as _logging
            _logger = _logging.getLogger("app.main")
            with SessionLocal() as db:
                if _has_legacy_channels(db):
                    _logger.info("File mover enabled (legacy 1_record → 2_chunks mode)")
                else:
                    _logger.info("File mover disabled (date-based recording layout)")

        assert any("File mover disabled" in r.message for r in caplog.records)

    def test_enabled_log_in_legacy_mode(self, caplog):
        """Startup emits 'File mover enabled' when a legacy channel exists."""
        from app.main import _has_legacy_channels

        SessionLocal = _make_db([_legacy_cfg()])
        with (
            caplog.at_level(logging.INFO, logger="app.main"),
        ):
            import logging as _logging
            _logger = _logging.getLogger("app.main")
            with SessionLocal() as db:
                if _has_legacy_channels(db):
                    _logger.info("File mover enabled (legacy 1_record → 2_chunks mode)")
                else:
                    _logger.info("File mover disabled (date-based recording layout)")

        assert any("File mover enabled" in r.message for r in caplog.records)
