"""
Tests for Phase 25 — Automatic Recording Retention Cleanup.

Coverage:
  Settings: recording_retention_enabled, recording_retention_days,
            prune_segment_db_after_delete defaults and env overrides.

  DB model: SegmentRecord.file_exists, deleted_at columns.

  _get_local_today: timezone handling and fallback.

  _parse_folder_date: happy path, garbage, partial.

  _scan_date_folders_for_retention: expiry by folder date, current-day
    protection, retention window, never_expires, non-date folder, dry_run
    no-delete, dry_run count + files_to_delete, folder pruning,
    missing root, byte counting.

  _delete_old_recordings_date_folders (Phase 23 compat): 3-arg int return.

  _delete_old_recordings_legacy: delete/keep/never_expires/dry_run.

  _mark_segments_deleted_in_db: sets fields, skips already-deleted row.

  _run_channel_retention_sync: global disabled, channel disabled,
    no target, date-folder route, legacy route.

  run_channel_retention (async): executed=False for dry, executed=True for live.

  API POST /api/v1/retention/run: 200 dry/live, 401 unauth, 403 non-admin,
    response schema, non-existent channel filter.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import auth as auth_router
from app.api.v1 import retention as retention_router
from app.db.models import Base, Channel, SegmentRecord
from app.db.session import get_db
from app.models.schemas import ChannelConfig
from app.services.auth_service import create_user
from app.services.retention import (
    _RetentionResult,
    _delete_old_recordings_date_folders,
    _delete_old_recordings_legacy,
    _get_local_today,
    _mark_segments_deleted_in_db,
    _parse_folder_date,
    _run_channel_retention_sync,
    _scan_date_folders_for_retention,
    run_channel_retention,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mp4(path: Path, age_seconds: float = 0.0, size: int = 16) -> Path:
    path.write_bytes(b"x" * size)
    if age_seconds > 0:
        t = time.time() - age_seconds
        os.utime(path, (t, t))
    return path


def _channel_json(
    record_root: str | None = None,
    final_dir: str | None = None,
    retention_enabled: bool = True,
    retention_days: int = 30,
) -> str:
    import json
    paths: dict = {}
    if record_root:
        paths["record_root"] = record_root
    if final_dir:
        paths["final_dir"] = final_dir
    return json.dumps({
        "id": "test_ch",
        "name": "TEST_CH",
        "display_name": "Test Channel",
        "capture": {
            "device_type": "dshow",
            "video_device": "V",
            "audio_device": "A",
            "resolution": "720x576",
            "framerate": 25,
        },
        "encoding": {"video_codec": "libx264"},
        "segmenting": {"segment_time": 300},
        "paths": paths,
        "retention": {"enabled": retention_enabled, "days": retention_days},
    })


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
    SL = sessionmaker(autocommit=False, autoflush=False, bind=in_memory_engine)
    with SL() as db:
        yield db


def _seed(
    db: Session,
    channel_id: str = "p25",
    filename: str = "seg.mp4",
    never_expires: bool = False,
    file_exists: bool = True,
    deleted_at: datetime | None = None,
) -> SegmentRecord:
    if db.query(Channel).filter(Channel.id == channel_id).first() is None:
        db.add(Channel(
            id=channel_id, name="T", display_name="T", enabled=True,
            config_json=_channel_json(),
        ))
        db.flush()
    seg = SegmentRecord(
        channel_id=channel_id,
        filename=filename,
        path=f"/tmp/{filename}",
        start_time=datetime(2026, 4, 5, 0, 0),
        end_time=datetime(2026, 4, 5, 1, 0),
        duration_seconds=3600.0,
        size_bytes=1024,
        manifest_date="2026-04-05",
        never_expires=never_expires,
        file_exists=file_exists,
        deleted_at=deleted_at,
    )
    db.add(seg)
    db.commit()
    return seg


def _make_app(engine):
    SL = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1")
    app.include_router(retention_router.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: (yield SL().__enter__())
    return app, SL


def _mock_settings(*, enabled=True, prune_db=False, tz="UTC"):
    s = MagicMock()
    s.recording_retention_enabled = enabled
    s.prune_segment_db_after_delete = prune_db
    s.manifest_timezone = tz
    return s


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class TestPhase25Settings:
    def test_recording_retention_enabled_default(self):
        from app.config.settings import Settings
        assert Settings().recording_retention_enabled is True

    def test_recording_retention_days_default(self):
        from app.config.settings import Settings
        assert Settings().recording_retention_days == 30

    def test_prune_segment_db_after_delete_default(self):
        from app.config.settings import Settings
        assert Settings().prune_segment_db_after_delete is False

    def test_recording_retention_enabled_override(self, monkeypatch):
        monkeypatch.setenv("PGMREC_RECORDING_RETENTION_ENABLED", "false")
        from importlib import reload
        import app.config.settings as m
        reload(m)
        assert m.Settings().recording_retention_enabled is False

    def test_recording_retention_days_override(self, monkeypatch):
        monkeypatch.setenv("PGMREC_RECORDING_RETENTION_DAYS", "90")
        from importlib import reload
        import app.config.settings as m
        reload(m)
        assert m.Settings().recording_retention_days == 90

    def test_prune_segment_db_after_delete_override(self, monkeypatch):
        monkeypatch.setenv("PGMREC_PRUNE_SEGMENT_DB_AFTER_DELETE", "true")
        from importlib import reload
        import app.config.settings as m
        reload(m)
        assert m.Settings().prune_segment_db_after_delete is True


# ---------------------------------------------------------------------------
# DB model
# ---------------------------------------------------------------------------

class TestPhase25DbModel:
    def test_file_exists_attribute_present(self):
        seg = SegmentRecord(
            channel_id="x", filename="f.mp4", path="/f.mp4",
            start_time=datetime(2026, 4, 5), end_time=datetime(2026, 4, 5, 1),
            duration_seconds=3600, size_bytes=1, manifest_date="2026-04-05",
        )
        assert hasattr(seg, "file_exists")

    def test_deleted_at_none_by_default(self):
        seg = SegmentRecord(
            channel_id="x", filename="f.mp4", path="/f.mp4",
            start_time=datetime(2026, 4, 5), end_time=datetime(2026, 4, 5, 1),
            duration_seconds=3600, size_bytes=1, manifest_date="2026-04-05",
        )
        assert seg.deleted_at is None

    def test_file_exists_and_deleted_at_persisted(self, db_session):
        seg = _seed(db_session, file_exists=False, deleted_at=datetime(2026, 4, 1))
        db_session.refresh(seg)
        assert seg.file_exists is False
        assert seg.deleted_at == datetime(2026, 4, 1)


# ---------------------------------------------------------------------------
# _get_local_today
# ---------------------------------------------------------------------------

class TestGetLocalToday:
    def test_returns_date_object(self):
        assert isinstance(_get_local_today("Europe/Belgrade"), date)

    def test_invalid_tz_falls_back_to_utc(self):
        assert isinstance(_get_local_today("Not/A/Timezone"), date)


# ---------------------------------------------------------------------------
# _parse_folder_date
# ---------------------------------------------------------------------------

class TestParseFolderDate:
    def test_valid_name(self, tmp_path):
        f = tmp_path / "2026_04_05"; f.mkdir()
        assert _parse_folder_date(f, "%Y_%m_%d") == date(2026, 4, 5)

    def test_garbage_name_returns_none(self, tmp_path):
        f = tmp_path / "not_a_date"; f.mkdir()
        assert _parse_folder_date(f, "%Y_%m_%d") is None

    def test_partial_name_returns_none(self, tmp_path):
        f = tmp_path / "2026_04"; f.mkdir()
        assert _parse_folder_date(f, "%Y_%m_%d") is None


# ---------------------------------------------------------------------------
# _scan_date_folders_for_retention
# ---------------------------------------------------------------------------

class TestScanDateFolders:
    FMT = "%Y_%m_%d"

    def _folder(self, root, d: date) -> Path:
        p = root / d.strftime(self.FMT)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _call(self, root, *, days=30, dry_run=False):
        with patch("app.services.retention._get_never_expires_filenames", return_value=set()):
            return _scan_date_folders_for_retention(
                channel_id="ch", record_root=root,
                retention_days=days, date_folder_format=self.FMT,
                channel_tz="UTC", dry_run=dry_run, prune_db=False,
            )

    def test_deletes_expired_file(self, tmp_path):
        today = _get_local_today("UTC")
        folder = self._folder(tmp_path, today - timedelta(days=35))
        mp4 = _mp4(folder / "s.mp4")
        result = self._call(tmp_path)
        assert result.files_deleted == 1
        assert not mp4.exists()

    def test_keeps_current_day_folder(self, tmp_path):
        folder = self._folder(tmp_path, _get_local_today("UTC"))
        mp4 = _mp4(folder / "s.mp4")
        result = self._call(tmp_path)
        assert result.files_deleted == 0
        assert mp4.exists()

    def test_keeps_within_retention_window(self, tmp_path):
        today = _get_local_today("UTC")
        folder = self._folder(tmp_path, today - timedelta(days=5))
        mp4 = _mp4(folder / "s.mp4")
        result = self._call(tmp_path)
        assert result.files_deleted == 0
        assert mp4.exists()

    def test_keeps_never_expires_file(self, tmp_path):
        today = _get_local_today("UTC")
        folder = self._folder(tmp_path, today - timedelta(days=35))
        mp4 = _mp4(folder / "protected.mp4")
        with patch("app.services.retention._get_never_expires_filenames",
                   return_value={"protected.mp4"}):
            result = _scan_date_folders_for_retention(
                channel_id="ch", record_root=tmp_path,
                retention_days=30, date_folder_format=self.FMT,
                channel_tz="UTC", dry_run=False, prune_db=False,
            )
        assert result.files_deleted == 0
        assert mp4.exists()

    def test_skips_non_date_folder(self, tmp_path):
        misc = tmp_path / "archive"; misc.mkdir()
        mp4 = _mp4(misc / "a.mp4")
        result = self._call(tmp_path)
        assert result.files_deleted == 0
        assert mp4.exists()

    def test_dry_run_no_deletion(self, tmp_path):
        today = _get_local_today("UTC")
        folder = self._folder(tmp_path, today - timedelta(days=35))
        mp4 = _mp4(folder / "s.mp4")
        result = self._call(tmp_path, dry_run=True)
        assert mp4.exists()
        assert result.files_deleted == 1

    def test_dry_run_files_to_delete_populated(self, tmp_path):
        today = _get_local_today("UTC")
        folder = self._folder(tmp_path, today - timedelta(days=35))
        mp4 = _mp4(folder / "s.mp4")
        result = self._call(tmp_path, dry_run=True)
        assert str(mp4) in result.files_to_delete

    def test_prunes_empty_folder(self, tmp_path):
        today = _get_local_today("UTC")
        folder = self._folder(tmp_path, today - timedelta(days=35))
        _mp4(folder / "s.mp4")
        self._call(tmp_path)
        assert not folder.exists()

    def test_skips_nonexistent_root(self, tmp_path):
        result = self._call(tmp_path / "missing")
        assert result.files_deleted == 0

    def test_total_bytes_reported(self, tmp_path):
        today = _get_local_today("UTC")
        folder = self._folder(tmp_path, today - timedelta(days=35))
        _mp4(folder / "s.mp4", size=512)
        result = self._call(tmp_path, dry_run=True)
        assert result.total_bytes == 512


# ---------------------------------------------------------------------------
# _delete_old_recordings_date_folders — Phase 23 compat
# ---------------------------------------------------------------------------

class TestLegacyDateFolderCompat:
    def test_three_arg_returns_int(self, tmp_path):
        with patch("app.services.retention._get_never_expires_filenames", return_value=set()):
            assert isinstance(_delete_old_recordings_date_folders("ch", tmp_path, 86400.0), int)

    def test_deletes_by_mtime(self, tmp_path):
        folder = tmp_path / "old"; folder.mkdir()
        mp4 = _mp4(folder / "o.mp4", age_seconds=40 * 86400)
        with patch("app.services.retention._get_never_expires_filenames", return_value=set()):
            count = _delete_old_recordings_date_folders("ch", tmp_path, 30 * 86400)
        assert count == 1
        assert not mp4.exists()

    def test_respects_never_expires(self, tmp_path):
        folder = tmp_path / "old"; folder.mkdir()
        mp4 = _mp4(folder / "p.mp4", age_seconds=40 * 86400)
        with patch("app.services.retention._get_never_expires_filenames",
                   return_value={"p.mp4"}):
            count = _delete_old_recordings_date_folders("ch", tmp_path, 30 * 86400)
        assert count == 0
        assert mp4.exists()


# ---------------------------------------------------------------------------
# _delete_old_recordings_legacy
# ---------------------------------------------------------------------------

class TestLegacyFinalDir:
    def test_deletes_old(self, tmp_path):
        mp4 = _mp4(tmp_path / "o.mp4", age_seconds=40 * 86400)
        with patch("app.services.retention._get_never_expires_filenames", return_value=set()):
            r = _delete_old_recordings_legacy("ch", tmp_path, 30 * 86400)
        assert r.files_deleted == 1
        assert not mp4.exists()

    def test_keeps_recent(self, tmp_path):
        mp4 = _mp4(tmp_path / "r.mp4", age_seconds=5 * 86400)
        with patch("app.services.retention._get_never_expires_filenames", return_value=set()):
            r = _delete_old_recordings_legacy("ch", tmp_path, 30 * 86400)
        assert r.files_deleted == 0
        assert mp4.exists()

    def test_respects_never_expires(self, tmp_path):
        mp4 = _mp4(tmp_path / "k.mp4", age_seconds=40 * 86400)
        with patch("app.services.retention._get_never_expires_filenames",
                   return_value={"k.mp4"}):
            r = _delete_old_recordings_legacy("ch", tmp_path, 30 * 86400)
        assert r.files_deleted == 0
        assert mp4.exists()

    def test_dry_run_no_deletion(self, tmp_path):
        mp4 = _mp4(tmp_path / "o.mp4", age_seconds=40 * 86400)
        with patch("app.services.retention._get_never_expires_filenames", return_value=set()):
            r = _delete_old_recordings_legacy("ch", tmp_path, 30 * 86400, dry_run=True)
        assert mp4.exists()
        assert r.files_deleted == 1


# ---------------------------------------------------------------------------
# _mark_segments_deleted_in_db
# ---------------------------------------------------------------------------

class TestMarkSegmentsDeleted:
    def test_sets_file_exists_false_and_deleted_at(self, in_memory_engine):
        SL = sessionmaker(autocommit=False, autoflush=False, bind=in_memory_engine)
        with SL() as db:
            _seed(db, channel_id="mark1", filename="f1.mp4")
        with patch("app.services.retention.get_session_factory", return_value=SL):
            _mark_segments_deleted_in_db("mark1", ["f1.mp4"])
        with SL() as db:
            row = db.query(SegmentRecord).filter_by(channel_id="mark1", filename="f1.mp4").first()
        assert row.file_exists is False
        assert row.deleted_at is not None

    def test_skips_already_deleted(self, in_memory_engine):
        SL = sessionmaker(autocommit=False, autoflush=False, bind=in_memory_engine)
        orig = datetime(2026, 1, 1)
        with SL() as db:
            _seed(db, channel_id="mark2", filename="f2.mp4",
                  file_exists=False, deleted_at=orig)
        with patch("app.services.retention.get_session_factory", return_value=SL):
            _mark_segments_deleted_in_db("mark2", ["f2.mp4"])
        with SL() as db:
            row = db.query(SegmentRecord).filter_by(channel_id="mark2", filename="f2.mp4").first()
        assert row.deleted_at == orig  # unchanged


# ---------------------------------------------------------------------------
# _run_channel_retention_sync
# ---------------------------------------------------------------------------

class TestRunChannelRetentionSync:
    def _cfg(self, **kw) -> ChannelConfig:
        return ChannelConfig.model_validate_json(_channel_json(**kw))

    def test_skips_global_disabled(self, tmp_path):
        cfg = self._cfg(record_root=str(tmp_path))
        with patch("app.services.retention.get_settings",
                   return_value=_mock_settings(enabled=False)):
            r = _run_channel_retention_sync("ch", cfg)
        assert r.skipped and "recording_retention_enabled=False" in (r.skip_reason or "")

    def test_skips_channel_disabled(self, tmp_path):
        cfg = self._cfg(record_root=str(tmp_path), retention_enabled=False)
        with patch("app.services.retention.get_settings",
                   return_value=_mock_settings()):
            r = _run_channel_retention_sync("ch", cfg)
        assert r.skipped

    def test_skips_no_target(self):
        cfg = self._cfg()
        with patch("app.services.retention.get_settings",
                   return_value=_mock_settings()):
            r = _run_channel_retention_sync("ch", cfg)
        assert r.skipped

    def test_routes_date_folder_mode(self, tmp_path):
        cfg = self._cfg(record_root=str(tmp_path))
        with (
            patch("app.services.retention.get_settings", return_value=_mock_settings()),
            patch("app.services.retention.resolve_channel_path", return_value=tmp_path),
            patch("app.services.retention._scan_date_folders_for_retention") as mock_fn,
        ):
            mock_fn.return_value = _RetentionResult(channel_id="ch")
            _run_channel_retention_sync("ch", cfg)
        mock_fn.assert_called_once()

    def test_routes_legacy_mode(self, tmp_path):
        cfg = self._cfg(final_dir=str(tmp_path))
        with (
            patch("app.services.retention.get_settings", return_value=_mock_settings()),
            patch("app.services.retention.resolve_channel_path", return_value=tmp_path),
            patch("app.services.retention._delete_old_recordings_legacy") as mock_fn,
        ):
            mock_fn.return_value = _RetentionResult(channel_id="ch")
            _run_channel_retention_sync("ch", cfg)
        mock_fn.assert_called_once()


# ---------------------------------------------------------------------------
# run_channel_retention (async)
# ---------------------------------------------------------------------------

class TestRunChannelRetentionAsync:
    def test_dry_run_executed_false(self, in_memory_engine):
        SL = sessionmaker(autocommit=False, autoflush=False, bind=in_memory_engine)
        with SL() as db:
            _seed(db, channel_id="async1")
        with (
            patch("app.services.retention.get_session_factory", return_value=SL),
            patch("app.services.retention._run_channel_retention_sync",
                  return_value=_RetentionResult(channel_id="async1")),
        ):
            result = _run(run_channel_retention(channel_id=None, dry_run=True))
        assert result.dry_run is True
        assert result.executed is False

    def test_live_run_executed_true(self, in_memory_engine):
        SL = sessionmaker(autocommit=False, autoflush=False, bind=in_memory_engine)
        with SL() as db:
            _seed(db, channel_id="async2")
        with (
            patch("app.services.retention.get_session_factory", return_value=SL),
            patch("app.services.retention._run_channel_retention_sync",
                  return_value=_RetentionResult(channel_id="async2")),
        ):
            result = _run(run_channel_retention(channel_id=None, dry_run=False))
        assert result.executed is True


# ---------------------------------------------------------------------------
# API endpoint  POST /api/v1/retention/run
# ---------------------------------------------------------------------------

class TestRetentionApi:
    def _setup(self, in_memory_engine):
        app, SL = _make_app(in_memory_engine)
        with SL() as db:
            create_user(db, "adm25", "adminpw", "admin")
            create_user(db, "exp25", "exportpw", "export")
        client = TestClient(app, raise_server_exceptions=False)
        return client, SL

    def _tok(self, client, username, password):
        r = client.post("/api/v1/auth/login",
                        data={"username": username, "password": password})
        return r.json()["access_token"]

    def _empty_db_patch(self):
        """Patch get_session_factory so retention scan returns no channels."""
        mock_db = MagicMock()
        mock_db.__enter__ = lambda s: s
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.first.return_value = None
        return patch("app.services.retention.get_session_factory",
                     return_value=lambda: mock_db)

    def test_dry_run_200(self, in_memory_engine):
        client, _ = self._setup(in_memory_engine)
        tok = self._tok(client, "adm25", "adminpw")
        with self._empty_db_patch():
            r = client.post("/api/v1/retention/run", json={"dry_run": True},
                            headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["dry_run"] is True
        assert r.json()["executed"] is False

    def test_live_run_200(self, in_memory_engine):
        client, _ = self._setup(in_memory_engine)
        tok = self._tok(client, "adm25", "adminpw")
        with self._empty_db_patch():
            r = client.post("/api/v1/retention/run", json={"dry_run": False},
                            headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["executed"] is True

    def test_unauthenticated_401(self, in_memory_engine):
        client, _ = self._setup(in_memory_engine)
        r = client.post("/api/v1/retention/run", json={"dry_run": True})
        assert r.status_code == 401

    def test_non_admin_403(self, in_memory_engine):
        client, _ = self._setup(in_memory_engine)
        tok = self._tok(client, "exp25", "exportpw")
        r = client.post("/api/v1/retention/run", json={"dry_run": True},
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403

    def test_response_schema(self, in_memory_engine):
        client, _ = self._setup(in_memory_engine)
        tok = self._tok(client, "adm25", "adminpw")
        with self._empty_db_patch():
            r = client.post("/api/v1/retention/run", json={"dry_run": True},
                            headers={"Authorization": f"Bearer {tok}"})
        data = r.json()
        for key in ("channels", "total_files_deleted", "total_folders_deleted", "total_bytes"):
            assert key in data
        assert isinstance(data["channels"], list)

    def test_nonexistent_channel_empty_result(self, in_memory_engine):
        client, _ = self._setup(in_memory_engine)
        tok = self._tok(client, "adm25", "adminpw")
        with self._empty_db_patch():
            r = client.post(
                "/api/v1/retention/run",
                json={"channel_id": "no_such_channel", "dry_run": True},
                headers={"Authorization": f"Bearer {tok}"},
            )
        assert r.status_code == 200
        assert r.json()["channels"] == []
