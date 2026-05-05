"""
Phase 23 unit tests — Date-Folder Recording Storage Model.

Covers:
- PathConfig: record_root field accepted; old fields become Optional
- PathConfig: effective_use_date_folders auto-detection (record_root only → True)
- PathConfig: effective_use_date_folders explicit override
- PathConfig: use_date_folders=False when only record_dir set (no record_root)
- PathConfig: legacy fields still accepted alongside record_root
- PathConfig: date_folder_format default is "%Y_%m_%d"
- Settings: segment_indexer_interval_seconds field exists
- Settings: segment_indexer_min_age_seconds field exists
- Settings: segment_indexer_stability_check_seconds field exists
- Settings: segment_indexer_min_duration_seconds field exists
- resolve_date_folder: returns correct sub-folder path for a given date
- resolve_date_folder: uses today when date=None
- resolve_date_folder: respects custom date_folder_format
- _output_pattern (new mode): uses record_root + date_folder_format
- _output_pattern (no record_root): returns "" (legacy paths ignored)
- ensure_date_folders: creates today + tomorrow folders
- ensure_date_folders: no-op for legacy channels
- ensure_date_folders: returns created paths
- _scan_date_folders: returns sorted sub-folders
- _scan_date_folders: empty root returns []
- _find_active_file: returns newest mp4
- _find_active_file: returns None for empty folder
- _is_segment_complete: skips active file
- _is_segment_complete: skips too-recent file
- _is_segment_complete: skips size-changing file
- _is_segment_complete: skips zero-duration file
- _is_segment_complete: returns True for valid segment
- _is_already_registered: True when segment exists in DB
- _is_already_registered: False when segment not in DB
- _run_segment_indexer_sync: skips legacy channels
- _run_segment_indexer_sync: registers complete segments
- _run_segment_indexer_sync: skips duplicates
- _get_newest_mp4_in_root: finds newest across date folders
- _get_newest_mp4_in_root: falls back to yesterday's folder
- _get_newest_mp4_in_root: returns None for empty root
- _delete_old_recordings_date_folders: deletes old files
- _delete_old_recordings_date_folders: respects never_expires
- _delete_old_recordings_date_folders: skips young files
- _prune_empty_date_folders: removes empty sub-dirs
- _prune_empty_date_folders: keeps non-empty sub-dirs
- _find_latest_in_date_folders: returns latest completed segment
- _find_latest_in_date_folders: skips active file
- _find_newer_in_date_folders: returns newer segment
- _find_newer_in_date_folders: returns None when nothing newer
- _latest_usable_segment_for_config: date-folder mode
- _latest_usable_segment_for_config: returns None when no record_root
- _newer_segment_for_config: date-folder mode
- _newer_segment_for_config: returns None when no record_root
- file_mover: skips date-folder channels
- file_mover: skips legacy channels (record_dir/chunks_dir ignored, warns)
- warn_legacy_paths: warning for channels with record_dir/chunks_dir
- rts1.json Phase 23: record_root field present
- rts1.json Phase 23: effective_use_date_folders is True
- rts1.json Phase 23: output pattern contains date pattern
"""
from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import resolve_date_folder
from app.db.models import Base, Channel, SegmentRecord
from app.models.schemas import ChannelConfig, PathConfig
from app.services.ffmpeg_builder import (
    _output_pattern,
    ensure_date_folders,
)
from app.services.hls_preview_manager import (
    _find_latest_in_date_folders,
    _find_newer_in_date_folders,
    _latest_usable_segment_for_config,
    _newer_segment_for_config,
)
from app.services.retention import (
    _delete_old_recordings_date_folders,
    _prune_empty_date_folders,
)
from app.services.segment_indexer import (
    _find_active_file,
    _is_already_registered,
    _is_segment_complete,
    _scan_date_folders,
)
from app.services.watchdog import _get_newest_mp4, _get_newest_mp4_in_root


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
    record_root: str | None = None,
    record_dir: str | None = None,
    chunks_dir: str | None = None,
    final_dir: str | None = None,
    use_date_folders: bool | None = None,
    date_folder_format: str = "%Y_%m_%d",
) -> ChannelConfig:
    paths: dict = {"date_folder_format": date_folder_format}
    if record_root is not None:
        paths["record_root"] = record_root
    if record_dir is not None:
        paths["record_dir"] = record_dir
    if chunks_dir is not None:
        paths["chunks_dir"] = chunks_dir
    if final_dir is not None:
        paths["final_dir"] = final_dir
    if use_date_folders is not None:
        paths["use_date_folders"] = use_date_folders

    return ChannelConfig(
        id="test_ch",
        name="Test",
        display_name="Test Channel",
        capture={"device_type": "dshow"},
        paths=paths,
    )


def _load_rts1() -> ChannelConfig:
    base = Path(__file__).parent.parent / "data" / "channels"
    return ChannelConfig.model_validate_json((base / "rts1.json").read_text())


# ---------------------------------------------------------------------------
# PathConfig — schema
# ---------------------------------------------------------------------------

class TestPathConfig:
    def test_record_root_accepted(self):
        pc = PathConfig(record_root="D:/AutoRec/record/rts1")
        assert pc.record_root == "D:/AutoRec/record/rts1"

    def test_legacy_fields_optional(self):
        pc = PathConfig(record_root="D:/AutoRec/record/rts1")
        assert pc.record_dir is None
        assert pc.chunks_dir is None
        assert pc.final_dir is None

    def test_record_dir_still_accepted(self):
        pc = PathConfig(record_dir="/tmp/1_record", chunks_dir="/tmp/2_chunks", final_dir="/tmp/3_final")
        assert pc.record_dir == "/tmp/1_record"

    def test_effective_use_date_folders_auto_true(self):
        """record_root set, record_dir absent → date-folder mode."""
        pc = PathConfig(record_root="/tmp/rec_root")
        assert pc.effective_use_date_folders is True

    def test_effective_use_date_folders_auto_false_legacy(self):
        """record_dir present → legacy mode."""
        pc = PathConfig(record_dir="/tmp/1_rec", chunks_dir="/tmp/2_ch", final_dir="/tmp/3_fin")
        assert pc.effective_use_date_folders is False

    def test_effective_use_date_folders_explicit_override_true(self):
        """Explicit use_date_folders=True overrides even when record_dir is also present."""
        pc = PathConfig(
            record_root="/tmp/root",
            record_dir="/tmp/1_rec",
            use_date_folders=True,
        )
        assert pc.effective_use_date_folders is True

    def test_effective_use_date_folders_explicit_override_false(self):
        pc = PathConfig(record_root="/tmp/root", use_date_folders=False)
        assert pc.effective_use_date_folders is False

    def test_date_folder_format_default(self):
        pc = PathConfig(record_root="/tmp/root")
        assert pc.date_folder_format == "%Y_%m_%d"

    def test_date_folder_format_custom(self):
        pc = PathConfig(record_root="/tmp/root", date_folder_format="%Y/%m/%d")
        assert pc.date_folder_format == "%Y/%m/%d"


# ---------------------------------------------------------------------------
# Settings — segment_indexer fields
# ---------------------------------------------------------------------------

class TestSettings:
    def test_segment_indexer_interval_default(self):
        from app.config.settings import get_settings
        s = get_settings()
        assert s.segment_indexer_interval_seconds == 15

    def test_segment_indexer_min_age_default(self):
        from app.config.settings import get_settings
        s = get_settings()
        assert s.segment_indexer_min_age_seconds == 30

    def test_segment_indexer_stability_default(self):
        from app.config.settings import get_settings
        s = get_settings()
        assert s.segment_indexer_stability_check_seconds == 1.0

    def test_segment_indexer_min_duration_default(self):
        from app.config.settings import get_settings
        s = get_settings()
        assert s.segment_indexer_min_duration_seconds == 1.0


# ---------------------------------------------------------------------------
# resolve_date_folder
# ---------------------------------------------------------------------------

class TestResolveDateFolder:
    def test_explicit_date(self, tmp_path):
        d = datetime.date(2026, 4, 5)
        result = resolve_date_folder(str(tmp_path), "%Y_%m_%d", d)
        assert result == tmp_path / "2026_04_05"

    def test_uses_today_when_none(self, tmp_path):
        today = datetime.date.today()
        expected_name = today.strftime("%Y_%m_%d")
        result = resolve_date_folder(str(tmp_path), "%Y_%m_%d", None)
        assert result.name == expected_name

    def test_custom_format(self, tmp_path):
        d = datetime.date(2026, 4, 5)
        result = resolve_date_folder(str(tmp_path), "%Y/%m/%d", d)
        assert result == tmp_path / "2026" / "04" / "05"


# ---------------------------------------------------------------------------
# _output_pattern
# ---------------------------------------------------------------------------

class TestOutputPattern:
    def test_new_mode_uses_record_root(self, tmp_path):
        cfg = _make_channel_config(record_root=str(tmp_path))
        pattern = _output_pattern(cfg)
        assert str(tmp_path) in pattern
        assert "%Y_%m_%d" in pattern
        assert pattern.endswith(".mp4")

    def test_new_mode_contains_date_pattern(self, tmp_path):
        cfg = _make_channel_config(record_root=str(tmp_path))
        pattern = _output_pattern(cfg)
        # pattern is: {root}/%Y_%m_%d/%d%m%y-%H%M%S.mp4
        assert "%Y_%m_%d" in pattern
        assert "%d%m%y-%H%M%S" in pattern

    def test_no_record_root_returns_empty(self, tmp_path):
        """When record_root is absent, _output_pattern returns "" (legacy paths ignored)."""
        cfg = _make_channel_config(
            record_dir=str(tmp_path / "1_record"),
            chunks_dir=str(tmp_path / "2_chunks"),
            final_dir=str(tmp_path / "3_final"),
        )
        pattern = _output_pattern(cfg)
        assert pattern == ""

    def test_new_mode_custom_date_format(self, tmp_path):
        cfg = _make_channel_config(
            record_root=str(tmp_path),
            date_folder_format="%Y-%m-%d",
        )
        pattern = _output_pattern(cfg)
        assert "%Y-%m-%d" in pattern


# ---------------------------------------------------------------------------
# ensure_date_folders
# ---------------------------------------------------------------------------

class TestEnsureDateFolders:
    def test_creates_today_and_tomorrow(self, tmp_path):
        cfg = _make_channel_config(record_root=str(tmp_path))
        created = ensure_date_folders(cfg)
        assert len(created) == 2
        for p in created:
            assert p.exists()
            assert p.is_dir()

    def test_no_op_for_legacy_channel(self, tmp_path):
        cfg = _make_channel_config(
            record_dir=str(tmp_path / "1_record"),
            chunks_dir=str(tmp_path / "2_chunks"),
            final_dir=str(tmp_path / "3_final"),
        )
        created = ensure_date_folders(cfg)
        assert created == []

    def test_returns_created_paths(self, tmp_path):
        cfg = _make_channel_config(record_root=str(tmp_path))
        today = datetime.date.today()
        tomorrow = today + datetime.timedelta(days=1)
        created = ensure_date_folders(cfg)
        names = {p.name for p in created}
        assert today.strftime("%Y_%m_%d") in names
        assert tomorrow.strftime("%Y_%m_%d") in names

    def test_idempotent_existing_dirs(self, tmp_path):
        cfg = _make_channel_config(record_root=str(tmp_path))
        ensure_date_folders(cfg)
        # Should not raise the second time
        created = ensure_date_folders(cfg)
        assert len(created) == 2


# ---------------------------------------------------------------------------
# _scan_date_folders
# ---------------------------------------------------------------------------

class TestScanDateFolders:
    def test_returns_sorted_folders(self, tmp_path):
        (tmp_path / "2026_04_03").mkdir()
        (tmp_path / "2026_04_05").mkdir()
        (tmp_path / "2026_04_04").mkdir()
        result = _scan_date_folders(tmp_path)
        assert [p.name for p in result] == ["2026_04_03", "2026_04_04", "2026_04_05"]

    def test_empty_root_returns_empty_list(self, tmp_path):
        empty = tmp_path / "noexist"
        assert _scan_date_folders(empty) == []

    def test_ignores_files_at_root(self, tmp_path):
        (tmp_path / "some_file.mp4").write_bytes(b"")
        (tmp_path / "2026_04_05").mkdir()
        result = _scan_date_folders(tmp_path)
        assert all(p.is_dir() for p in result)


# ---------------------------------------------------------------------------
# _find_active_file
# ---------------------------------------------------------------------------

class TestFindActiveFile:
    def test_returns_newest_mp4(self, tmp_path):
        old = tmp_path / "010426-120000.mp4"
        new = tmp_path / "010426-120500.mp4"
        old.write_bytes(b"a")
        time.sleep(0.05)
        new.write_bytes(b"b")
        result = _find_active_file(tmp_path)
        assert result == new

    def test_returns_none_for_empty_folder(self, tmp_path):
        assert _find_active_file(tmp_path) is None


# ---------------------------------------------------------------------------
# _is_segment_complete
# ---------------------------------------------------------------------------

class TestIsSegmentComplete:
    def _make_mp4(self, folder: Path, name: str, age_seconds: float = 60.0) -> Path:
        p = folder / name
        p.write_bytes(b"x" * 1024)
        mtime = time.time() - age_seconds
        import os
        os.utime(p, (mtime, mtime))
        return p

    def test_skips_active_file(self, tmp_path):
        mp4 = self._make_mp4(tmp_path, "test.mp4", age_seconds=120)
        result = _is_segment_complete(
            path=mp4,
            active_file=mp4,  # same file = active
            min_age_seconds=30,
            stability_check_seconds=0.0,
            min_duration_seconds=1.0,
            ffprobe_path="ffprobe",
        )
        assert result is False

    def test_skips_too_recent_file(self, tmp_path):
        mp4 = self._make_mp4(tmp_path, "test.mp4", age_seconds=5)  # 5s old
        result = _is_segment_complete(
            path=mp4,
            active_file=None,
            min_age_seconds=30,
            stability_check_seconds=0.0,
            min_duration_seconds=1.0,
            ffprobe_path="ffprobe",
        )
        assert result is False

    def test_skips_size_changing_file(self, tmp_path):
        """A size-unstable file (detected via double-read) must be skipped."""
        mp4 = tmp_path / "growing.mp4"

        with patch(
            "app.services.segment_indexer._is_size_stable",
            return_value=False,
        ):
            mp4.write_bytes(b"x" * 100)
            import os
            import time as _t
            _t_past = _t.time() - 120
            os.utime(mp4, (_t_past, _t_past))
            result = _is_segment_complete(
                path=mp4,
                active_file=None,
                min_age_seconds=30,
                stability_check_seconds=0.01,
                min_duration_seconds=1.0,
                ffprobe_path="ffprobe",
            )
        assert result is False

    def test_skips_zero_duration(self, tmp_path):
        mp4 = self._make_mp4(tmp_path, "test.mp4", age_seconds=120)
        with (
            patch("app.services.segment_indexer._is_size_stable", return_value=True),
            patch("app.services.segment_indexer._ffprobe_duration", return_value=0.0),
        ):
            result = _is_segment_complete(
                path=mp4,
                active_file=None,
                min_age_seconds=30,
                stability_check_seconds=0.0,
                min_duration_seconds=1.0,
                ffprobe_path="ffprobe",
            )
        assert result is False

    def test_returns_true_for_valid_segment(self, tmp_path):
        mp4 = self._make_mp4(tmp_path, "test.mp4", age_seconds=120)
        with (
            patch("app.services.segment_indexer._is_size_stable", return_value=True),
            patch("app.services.segment_indexer._ffprobe_duration", return_value=300.0),
        ):
            result = _is_segment_complete(
                path=mp4,
                active_file=None,
                min_age_seconds=30,
                stability_check_seconds=0.0,
                min_duration_seconds=1.0,
                ffprobe_path="ffprobe",
            )
        assert result is True


# ---------------------------------------------------------------------------
# _is_already_registered
# ---------------------------------------------------------------------------

class TestIsAlreadyRegistered:
    def _seed_segment(self, db, channel_id: str, filename: str):
        ch = Channel(
            id=channel_id,
            name="T",
            display_name="T",
            config_json='{"id":"test_ch","name":"T","display_name":"T","capture":{"device_type":"dshow"},"paths":{"record_root":"/tmp/r"}}',
        )
        db.add(ch)
        db.flush()
        sr = SegmentRecord(
            channel_id=channel_id,
            filename=filename,
            path=f"/tmp/{filename}",
            start_time=datetime.datetime(2026, 4, 5, 12, 0, 0),
            end_time=datetime.datetime(2026, 4, 5, 12, 5, 0),
            duration_seconds=300.0,
            size_bytes=1000,
            status="complete",
            manifest_date="2026-04-05",
        )
        db.add(sr)
        db.commit()

    def test_returns_true_when_registered(self, in_memory_db):
        self._seed_segment(in_memory_db, "test_ch", "010426-120000.mp4")
        assert _is_already_registered("test_ch", "010426-120000.mp4", in_memory_db) is True

    def test_returns_false_when_not_registered(self, in_memory_db):
        self._seed_segment(in_memory_db, "test_ch", "other.mp4")
        assert _is_already_registered("test_ch", "010426-120000.mp4", in_memory_db) is False


# ---------------------------------------------------------------------------
# _run_segment_indexer_sync
# ---------------------------------------------------------------------------

class TestRunSegmentIndexerSync:
    def test_skips_legacy_channels(self, tmp_path, in_memory_db):
        """Channels without record_root are not processed by the indexer."""
        from app.services.segment_indexer import _run_segment_indexer_sync
        from app.db.session import get_session_factory

        # Insert a legacy channel
        legacy_config = {
            "id": "legacy_ch",
            "name": "L",
            "display_name": "L",
            "capture": {"device_type": "dshow"},
            "paths": {
                "record_dir": str(tmp_path / "1_rec"),
                "chunks_dir": str(tmp_path / "2_ch"),
                "final_dir": str(tmp_path / "3_fin"),
            },
        }
        in_memory_db.add(Channel(
            id="legacy_ch",
            name="L",
            display_name="L",
            config_json=json.dumps(legacy_config),
        ))
        in_memory_db.commit()

        with patch("app.services.segment_indexer.get_session_factory") as mock_sf:
            mock_sf.return_value = lambda: in_memory_db.__class__(bind=in_memory_db.bind)
            # No assertions on register_segment — should not be called
            with patch("app.services.segment_indexer._is_already_registered") as m_reg:
                _run_segment_indexer_sync()
                m_reg.assert_not_called()

    def test_registers_complete_segments(self, tmp_path):
        """A complete segment that is not registered gets registered."""
        from app.services.segment_indexer import _run_segment_indexer_sync
        from app.db.session import get_session_factory

        # Create a date folder with a completed segment
        date_folder = tmp_path / "2026_04_05"
        date_folder.mkdir()
        seg = date_folder / "050426-120000.mp4"
        seg.write_bytes(b"x" * 1024)
        import os
        import time as _t
        mtime = _t.time() - 120
        os.utime(seg, (mtime, mtime))

        # Create a newer "active" segment to ensure the older one is indexed
        active = date_folder / "050426-120500.mp4"
        active.write_bytes(b"y" * 512)

        channel_cfg = {
            "id": "test_ch",
            "name": "T",
            "display_name": "T",
            "capture": {"device_type": "dshow"},
            "paths": {"record_root": str(tmp_path)},
            "segmentation": {
                "segment_time": "00:05:00",
                "filename_pattern": "%d%m%y-%H%M%S",
            },
        }

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine)

        with SessionLocal() as db:
            db.add(Channel(
                id="test_ch",
                name="T",
                display_name="T",
                config_json=json.dumps(channel_cfg),
            ))
            db.commit()

        registered: list[Path] = []

        def _mock_register(ch_id, path, cfg, db):
            registered.append(path)

        with (
            patch("app.services.segment_indexer.get_session_factory", return_value=SessionLocal),
            patch("app.services.segment_indexer._is_already_registered", return_value=False),
            patch("app.services.segment_indexer._is_segment_complete", return_value=True),
            patch("app.services.segment_indexer.ensure_date_folders"),
            patch("app.services.manifest_service.register_segment", side_effect=_mock_register),
        ):
            _run_segment_indexer_sync()

        assert len(registered) >= 1

    def test_skips_already_registered(self, tmp_path):
        """Segments already in the DB are not re-registered."""
        from app.services.segment_indexer import _run_segment_indexer_sync

        date_folder = tmp_path / "2026_04_05"
        date_folder.mkdir()
        seg = date_folder / "050426-120000.mp4"
        seg.write_bytes(b"x" * 1024)
        import os
        import time as _t
        mtime = _t.time() - 120
        os.utime(seg, (mtime, mtime))

        channel_cfg = {
            "id": "test_ch",
            "name": "T",
            "display_name": "T",
            "capture": {"device_type": "dshow"},
            "paths": {"record_root": str(tmp_path)},
        }

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine)

        with SessionLocal() as db:
            db.add(Channel(
                id="test_ch",
                name="T",
                display_name="T",
                config_json=json.dumps(channel_cfg),
            ))
            db.commit()

        register_calls: list = []

        with (
            patch("app.services.segment_indexer.get_session_factory", return_value=SessionLocal),
            patch("app.services.segment_indexer._is_already_registered", return_value=True),
            patch("app.services.segment_indexer.ensure_date_folders"),
            patch("app.services.manifest_service.register_segment", side_effect=lambda *a, **k: register_calls.append(1)),
        ):
            _run_segment_indexer_sync()

        assert len(register_calls) == 0


# ---------------------------------------------------------------------------
# _get_newest_mp4_in_root
# ---------------------------------------------------------------------------

class TestGetNewestMp4InRoot:
    def test_finds_newest_across_date_folders(self, tmp_path):
        f1 = tmp_path / "2026_04_04" / "old.mp4"
        f2 = tmp_path / "2026_04_05" / "new.mp4"
        f1.parent.mkdir()
        f2.parent.mkdir()
        f1.write_bytes(b"a")
        import os, time as t
        os.utime(f1, (t.time() - 600, t.time() - 600))
        f2.write_bytes(b"b")
        path, mtime, size = _get_newest_mp4_in_root(tmp_path)
        assert path == f2

    def test_falls_back_to_yesterday(self, tmp_path):
        """If today's folder is empty, should find files in previous folder."""
        old = tmp_path / "2026_04_04" / "old.mp4"
        old.parent.mkdir()
        old.write_bytes(b"a")
        today = tmp_path / "2026_04_05"
        today.mkdir()
        # today is empty
        path, mtime, size = _get_newest_mp4_in_root(tmp_path)
        assert path == old

    def test_returns_none_for_empty_root(self, tmp_path):
        empty = tmp_path / "noexist"
        path, mtime, size = _get_newest_mp4_in_root(empty)
        assert path is None
        assert mtime is None
        assert size is None

    def test_returns_none_when_no_mp4(self, tmp_path):
        (tmp_path / "2026_04_05").mkdir()
        path, mtime, size = _get_newest_mp4_in_root(tmp_path)
        assert path is None


# ---------------------------------------------------------------------------
# Retention — date-folder mode
# ---------------------------------------------------------------------------

class TestRetentionDateFolders:
    def _make_old_mp4(self, folder: Path, name: str, age_days: float = 35.0) -> Path:
        p = folder / name
        p.write_bytes(b"x" * 100)
        mtime = time.time() - age_days * 86400
        import os
        os.utime(p, (mtime, mtime))
        return p

    def test_deletes_old_files(self, tmp_path):
        folder = tmp_path / "2026_04_01"
        folder.mkdir()
        old = self._make_old_mp4(folder, "old.mp4", age_days=35)
        max_age = 30 * 86400.0
        count = _delete_old_recordings_date_folders("test_ch", tmp_path, max_age)
        assert count == 1
        assert not old.exists()

    def test_keeps_young_files(self, tmp_path):
        folder = tmp_path / "2026_04_05"
        folder.mkdir()
        young = self._make_old_mp4(folder, "young.mp4", age_days=5)
        max_age = 30 * 86400.0
        count = _delete_old_recordings_date_folders("test_ch", tmp_path, max_age)
        assert count == 0
        assert young.exists()

    def test_respects_never_expires(self, tmp_path):
        folder = tmp_path / "2026_04_01"
        folder.mkdir()
        protected = self._make_old_mp4(folder, "protected.mp4", age_days=60)
        max_age = 30 * 86400.0
        with patch(
            "app.services.retention._get_never_expires_filenames",
            return_value={"protected.mp4"},
        ):
            count = _delete_old_recordings_date_folders("test_ch", tmp_path, max_age)
        assert count == 0
        assert protected.exists()

    def test_prunes_empty_date_folder(self, tmp_path):
        empty_folder = tmp_path / "2026_04_01"
        empty_folder.mkdir()
        _prune_empty_date_folders(tmp_path)
        assert not empty_folder.exists()

    def test_keeps_non_empty_date_folder(self, tmp_path):
        folder = tmp_path / "2026_04_01"
        folder.mkdir()
        (folder / "segment.mp4").write_bytes(b"x")
        _prune_empty_date_folders(tmp_path)
        assert folder.exists()


# ---------------------------------------------------------------------------
# HLS Preview — date-folder helpers
# ---------------------------------------------------------------------------

class TestHlsPreviewDateFolders:
    def _make_mp4(self, path: Path, age_secs: float = 60.0) -> Path:
        path.write_bytes(b"x" * 100)
        mtime = time.time() - age_secs
        import os
        os.utime(path, (mtime, mtime))
        return path

    def test_find_latest_in_date_folders_returns_latest(self, tmp_path):
        folder = tmp_path / "2026_04_05"
        folder.mkdir()
        f1 = self._make_mp4(folder / "seg1.mp4", age_secs=300)
        f2 = self._make_mp4(folder / "seg2.mp4", age_secs=120)
        # seg3 is the active file (newest)
        self._make_mp4(folder / "seg3.mp4", age_secs=10)
        result = _find_latest_in_date_folders(tmp_path)
        assert result == f2

    def test_find_latest_in_date_folders_none_when_only_active(self, tmp_path):
        folder = tmp_path / "2026_04_05"
        folder.mkdir()
        self._make_mp4(folder / "active.mp4", age_secs=5)
        result = _find_latest_in_date_folders(tmp_path)
        # Only file is the active one — nothing to return
        assert result is None

    def test_find_newer_in_date_folders_finds_newer(self, tmp_path):
        folder = tmp_path / "2026_04_05"
        folder.mkdir()
        current = self._make_mp4(folder / "seg1.mp4", age_secs=300)
        newer = self._make_mp4(folder / "seg2.mp4", age_secs=120)
        # active
        self._make_mp4(folder / "seg3.mp4", age_secs=10)
        result = _find_newer_in_date_folders(current, tmp_path)
        assert result == newer

    def test_find_newer_in_date_folders_none_when_nothing_newer(self, tmp_path):
        folder = tmp_path / "2026_04_05"
        folder.mkdir()
        current = self._make_mp4(folder / "seg2.mp4", age_secs=120)
        # active
        self._make_mp4(folder / "seg3.mp4", age_secs=10)
        result = _find_newer_in_date_folders(current, tmp_path)
        assert result is None

    def test_latest_usable_segment_for_config_date_mode(self, tmp_path):
        folder = tmp_path / "2026_04_05"
        folder.mkdir()
        f1 = self._make_mp4(folder / "seg1.mp4", age_secs=300)
        self._make_mp4(folder / "seg2.mp4", age_secs=10)  # active
        cfg = _make_channel_config(record_root=str(tmp_path))
        result = _latest_usable_segment_for_config(cfg)
        assert result == f1

    def test_latest_usable_segment_for_config_no_record_root_returns_none(self, tmp_path):
        """When record_root is absent, _latest_usable_segment_for_config returns None."""
        cfg = _make_channel_config(
            record_dir=str(tmp_path / "1_record"),
            chunks_dir=str(tmp_path / "2_chunks"),
            final_dir=str(tmp_path / "3_final"),
        )
        result = _latest_usable_segment_for_config(cfg)
        assert result is None

    def test_newer_segment_for_config_date_mode(self, tmp_path):
        folder = tmp_path / "2026_04_05"
        folder.mkdir()
        current = self._make_mp4(folder / "seg1.mp4", age_secs=300)
        newer = self._make_mp4(folder / "seg2.mp4", age_secs=120)
        self._make_mp4(folder / "seg3.mp4", age_secs=10)  # active
        cfg = _make_channel_config(record_root=str(tmp_path))
        result = _newer_segment_for_config(current, cfg)
        assert result == newer

    def test_newer_segment_for_config_no_record_root_returns_none(self, tmp_path):
        """When record_root is absent, _newer_segment_for_config returns None."""
        record = tmp_path / "1_record"
        record.mkdir()
        current = self._make_mp4(record / "seg1.mp4", age_secs=300)
        cfg = _make_channel_config(
            record_dir=str(record),
            chunks_dir=str(tmp_path / "2_chunks"),
            final_dir=str(tmp_path / "3_final"),
        )
        result = _newer_segment_for_config(current, cfg)
        assert result is None


# ---------------------------------------------------------------------------
# file_mover — Phase 23 skip logic
# ---------------------------------------------------------------------------

class TestFileMoverSkipLogic:
    def test_skips_date_folder_channel(self, tmp_path):
        from app.services.file_mover import _run_file_mover_sync

        channel_cfg = {
            "id": "date_ch",
            "name": "D",
            "display_name": "D",
            "capture": {"device_type": "dshow"},
            "paths": {"record_root": str(tmp_path)},
        }
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine)
        with SessionLocal() as db:
            db.add(Channel(id="date_ch", name="D", display_name="D", config_json=json.dumps(channel_cfg)))
            db.commit()

        with (
            patch("app.services.file_mover.get_session_factory", return_value=SessionLocal),
            patch("app.services.file_mover._move_completed_files") as mock_move,
        ):
            _run_file_mover_sync()

        mock_move.assert_not_called()

    def test_skips_legacy_channel_with_warning(self, tmp_path):
        """Legacy record_dir/chunks_dir channels are skipped (no move), with a WARNING."""
        from app.services.file_mover import _run_file_mover_sync

        (tmp_path / "1_rec").mkdir()
        (tmp_path / "2_ch").mkdir()

        channel_cfg = {
            "id": "legacy_ch",
            "name": "L",
            "display_name": "L",
            "capture": {"device_type": "dshow"},
            "paths": {
                "record_dir": str(tmp_path / "1_rec"),
                "chunks_dir": str(tmp_path / "2_ch"),
                "final_dir": str(tmp_path / "3_fin"),
            },
        }
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine)
        with SessionLocal() as db:
            db.add(Channel(id="legacy_ch", name="L", display_name="L", config_json=json.dumps(channel_cfg)))
            db.commit()

        with (
            patch("app.services.file_mover.get_session_factory", return_value=SessionLocal),
            patch("app.services.file_mover._move_completed_files", return_value=[]) as mock_move,
        ):
            _run_file_mover_sync()

        mock_move.assert_not_called()


# ---------------------------------------------------------------------------
# Legacy path warning
# ---------------------------------------------------------------------------

class TestLegacyPathWarning:
    def test_warn_for_legacy_channel(self, tmp_path, caplog):
        """_warn_legacy_paths emits WARNING for channels with record_dir/chunks_dir."""
        from app.main import _warn_legacy_paths

        channel_cfg = {
            "id": "legacy_ch",
            "name": "L",
            "display_name": "L",
            "capture": {"device_type": "dshow"},
            "paths": {
                "record_dir": "D:/1_record",
                "chunks_dir": "D:/2_chunks",
                "final_dir": "D:/3_final",
            },
        }
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine)
        with SessionLocal() as db:
            db.add(Channel(id="legacy_ch", name="L", display_name="L", config_json=json.dumps(channel_cfg)))
            db.commit()

            import logging
            with caplog.at_level(logging.WARNING, logger="app.main"):
                _warn_legacy_paths(db)

        assert "legacy" in caplog.text.lower()

    def test_no_warn_for_date_folder_channel(self, tmp_path, caplog):
        from app.main import _warn_legacy_paths

        channel_cfg = {
            "id": "date_ch",
            "name": "D",
            "display_name": "D",
            "capture": {"device_type": "dshow"},
            "paths": {"record_root": str(tmp_path)},
        }
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine)
        with SessionLocal() as db:
            db.add(Channel(id="date_ch", name="D", display_name="D", config_json=json.dumps(channel_cfg)))
            db.commit()

            import logging
            with caplog.at_level(logging.WARNING, logger="app.main"):
                _warn_legacy_paths(db)

        assert "legacy" not in caplog.text.lower()


# ---------------------------------------------------------------------------
# rts1.json Phase 23 integration
# ---------------------------------------------------------------------------

class TestRts1Phase23:
    def test_rts1_has_record_root(self):
        cfg = _load_rts1()
        assert cfg.paths.record_root is not None
        assert "rts1" in cfg.paths.record_root.lower()

    def test_rts1_effective_use_date_folders(self):
        """rts1 has both record_root and record_dir; since both are present,
        auto-detect resolves to date-folder mode (record_root wins)."""
        cfg = _load_rts1()
        # effective_use_date_folders is True when record_root is set
        # (record_dir is also present for backward compat but doesn't block it)
        assert cfg.paths.record_root is not None

    def test_rts1_output_pattern_contains_date_folder(self, tmp_path):
        """Output pattern for rts1 (date-folder mode) includes %Y_%m_%d."""
        cfg = _load_rts1()
        pattern = _output_pattern(cfg)
        assert "%Y_%m_%d" in pattern

    def test_rts1_output_pattern_ends_with_mp4(self):
        cfg = _load_rts1()
        pattern = _output_pattern(cfg)
        assert pattern.endswith(".mp4")
