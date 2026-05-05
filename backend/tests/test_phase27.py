"""
Phase 27 — Start diagnostics and date-based path fixes.

Coverage:
  resolve_channel_path(None) raises ValueError (no raw Path(None)).
  build_ffmpeg_command() with date-based config (no record_dir/chunks_dir) builds
    a valid output path using record_root + date_folder_format + filename_pattern.
  ProcessManager._preflight_check() raises ValueError for missing record_root
    in date-based mode.
  ProcessManager._preflight_check() raises ValueError when neither record_root
    nor record_dir is set.
  ProcessManager.start() raises ValueError for a date-based config with no
    record_root, which the API maps to HTTP 400.
  ProcessManager.start() succeeds and calls ensure_date_folders for date-based
    channels (record_dir/chunks_dir may be None).
  API endpoint POST /channels/{id}/start returns HTTP 400 on ValueError and
    HTTP 409 on RuntimeError (already running).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import resolve_channel_path
from app.db.models import Base, Channel
from app.models.schemas import ChannelConfig, PathConfig
from app.services.ffmpeg_builder import build_ffmpeg_command
from app.services.process_manager import ProcessManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _date_based_config(
    *,
    record_root: str | None = "/tmp/rts1",
    use_date_folders: bool | None = None,
    rpo_mode: str = "disabled",
) -> ChannelConfig:
    """
    Minimal date-based ChannelConfig.  record_dir/chunks_dir/final_dir are
    intentionally absent to verify that they are not required.
    """
    path_kwargs: dict = {}
    if record_root is not None:
        path_kwargs["record_root"] = record_root
    if use_date_folders is not None:
        path_kwargs["use_date_folders"] = use_date_folders

    rpo_block = None
    if rpo_mode != "disabled":
        from app.models.schemas import RecordingPreviewOutputConfig
        rpo_block = RecordingPreviewOutputConfig(enabled=True, mode=rpo_mode)

    return ChannelConfig(
        id="rts1",
        name="RTS1",
        display_name="RTS1 Test",
        capture={"device_type": "dshow"},
        paths=path_kwargs,
        recording_preview_output=rpo_block,
    )


def _make_db_mock():
    """Return a minimal SQLAlchemy session mock suitable for process_manager."""
    db = MagicMock()
    db.add = MagicMock()
    db.commit = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    return db


def _mock_process(returncode=None):
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    proc.poll.return_value = returncode
    proc.wait.return_value = returncode
    return proc


def _mock_settings(tmp_path: Path):
    ms = MagicMock()
    ms.logs_dir = tmp_path / "logs"
    ms.logs_dir.mkdir(parents=True, exist_ok=True)
    ms.min_free_disk_bytes = 0
    ms.log_max_files_per_channel = 10
    ms.preview_dir = tmp_path / "preview"
    return ms


# ---------------------------------------------------------------------------
# resolve_channel_path — None guard
# ---------------------------------------------------------------------------

class TestResolveChannelPathNoneGuard:
    def test_raises_value_error_for_none(self):
        """resolve_channel_path(None) must raise ValueError, not TypeError."""
        with pytest.raises(ValueError, match="None"):
            resolve_channel_path(None)

    def test_accepts_valid_absolute_path(self, tmp_path):
        """Sanity check: absolute path returns Path object unchanged."""
        result = resolve_channel_path(str(tmp_path))
        assert result == tmp_path

    def test_error_message_mentions_record_root(self):
        """Error message must guide operator to check path config fields."""
        with pytest.raises(ValueError) as exc_info:
            resolve_channel_path(None)
        assert "record_root" in str(exc_info.value) or "path field" in str(exc_info.value)


# ---------------------------------------------------------------------------
# build_ffmpeg_command — date-based config, no legacy dirs
# ---------------------------------------------------------------------------

class TestBuildCommandDateBased:
    def test_command_built_without_record_dir(self, tmp_path):
        """
        build_ffmpeg_command() with date-based config (record_root set, no
        record_dir/chunks_dir) must return a non-empty command list whose last
        element contains the date-folder pattern.
        """
        root = str(tmp_path / "rts1")
        cfg = _date_based_config(record_root=root)

        cmd = build_ffmpeg_command(cfg)

        assert isinstance(cmd, list)
        assert len(cmd) > 0
        # Last element is the output path pattern; must contain record_root
        output_arg = cmd[-1]
        assert root in output_arg or str(tmp_path) in output_arg

    def test_output_pattern_contains_date_folder_format(self, tmp_path):
        """Output path must embed the strftime date-folder pattern."""
        root = str(tmp_path / "rts1")
        cfg = _date_based_config(record_root=root)

        cmd = build_ffmpeg_command(cfg)
        output_arg = cmd[-1]

        # Default date_folder_format is "%Y_%m_%d"
        assert "%Y_%m_%d" in output_arg

    def test_output_pattern_contains_filename_pattern(self, tmp_path):
        """Output path must embed the segmentation filename pattern."""
        root = str(tmp_path / "rts1")
        cfg = _date_based_config(record_root=root)

        cmd = build_ffmpeg_command(cfg)
        output_arg = cmd[-1]

        # Default filename_pattern is "%d%m%y-%H%M%S"
        assert "%d%m%y-%H%M%S" in output_arg

    def test_command_ends_with_mp4(self, tmp_path):
        """Output path must use the .mp4 extension."""
        root = str(tmp_path / "rts1")
        cfg = _date_based_config(record_root=root)

        cmd = build_ffmpeg_command(cfg)
        assert cmd[-1].endswith(".mp4")

    def test_path_none_never_in_command(self, tmp_path):
        """The command list must not contain 'None' as a string element."""
        root = str(tmp_path / "rts1")
        cfg = _date_based_config(record_root=root)

        cmd = build_ffmpeg_command(cfg)
        for token in cmd:
            assert token is not None, "Command must not contain None tokens"
            assert "None" not in token, f"Command token '{token}' contains 'None'"

    def test_paths_record_dir_none_is_valid(self, tmp_path):
        """
        PathConfig with record_root set and record_dir=None must not raise
        when building the FFmpeg command.
        """
        root = str(tmp_path / "rts1")
        cfg = _date_based_config(record_root=root)
        assert cfg.paths.record_dir is None  # confirm precondition
        assert cfg.paths.chunks_dir is None

        # Must not raise
        cmd = build_ffmpeg_command(cfg)
        assert cmd  # non-empty


# ---------------------------------------------------------------------------
# ProcessManager._preflight_check — validation
# ---------------------------------------------------------------------------

class TestPreflightCheck:
    def test_missing_record_root_raises_value_error(self):
        """
        Date-based config (use_date_folders=True) with no record_root must
        raise ValueError with a message mentioning record_root.
        """
        cfg = _date_based_config(record_root=None, use_date_folders=True)
        pm = ProcessManager()

        with pytest.raises(ValueError, match="record_root"):
            pm._preflight_check("rts1", cfg)

    def test_missing_both_paths_raises_value_error(self):
        """
        Config with neither record_root nor record_dir must raise ValueError.
        """
        cfg = ChannelConfig(
            id="test_ch",
            name="Test",
            display_name="Test",
            capture={"device_type": "dshow"},
            paths={},  # no record_root, no record_dir
        )
        # Disable date-folder auto-detect (record_root is None → effective=False)
        assert not cfg.paths.effective_use_date_folders
        pm = ProcessManager()

        with pytest.raises(ValueError, match="record"):
            pm._preflight_check("test_ch", cfg)

    def test_valid_date_based_config_passes(self, tmp_path):
        """
        A fully valid date-based config must not raise in _preflight_check.
        """
        root = str(tmp_path / "rts1")
        cfg = _date_based_config(record_root=root)
        pm = ProcessManager()

        # _preflight_check checks if ffmpeg binary exists only for absolute paths.
        # Use a known-existing file as a stand-in for the ffmpeg binary.
        import sys
        cfg2 = cfg.model_copy(update={"ffmpeg_path": sys.executable})

        # Should not raise
        pm._preflight_check("rts1", cfg2)

    def test_empty_output_pattern_raises_value_error(self, tmp_path):
        """
        When date-based mode is active but _output_pattern() returns '' (e.g.
        the resolve step fails), _preflight_check must raise ValueError.
        """
        root = str(tmp_path / "rts1")
        cfg = _date_based_config(record_root=root)
        import sys
        cfg = cfg.model_copy(update={"ffmpeg_path": sys.executable})
        pm = ProcessManager()

        # Patch _output_pattern inside ffmpeg_builder (imported with alias in preflight)
        with patch(
            "app.services.ffmpeg_builder._output_pattern",
            return_value="",
        ):
            with pytest.raises(ValueError, match="empty"):
                pm._preflight_check("rts1", cfg)

    def test_hls_direct_preview_dir_created(self, tmp_path):
        """
        hls_direct mode must cause _preflight_check to create the preview dir.
        """
        root = str(tmp_path / "rts1")
        from app.models.schemas import RecordingPreviewOutputConfig
        rpo = RecordingPreviewOutputConfig(enabled=True, mode="hls_direct")
        cfg = _date_based_config(record_root=root)
        cfg = cfg.model_copy(update={"recording_preview_output": rpo})

        pm = ProcessManager()
        import sys
        cfg = cfg.model_copy(update={"ffmpeg_path": sys.executable})

        preview_dir = tmp_path / "preview" / "rts1"
        ms = _mock_settings(tmp_path)
        ms.preview_dir = tmp_path / "preview"

        with patch("app.services.process_manager.get_settings", return_value=ms):
            pm._preflight_check("rts1", cfg)

        assert preview_dir.exists()


# ---------------------------------------------------------------------------
# ProcessManager.start() — date-based path fixes
# ---------------------------------------------------------------------------

class TestProcessManagerStartDateBased:
    def test_start_date_based_no_record_dir_calls_ensure_date_folders(self, tmp_path):
        """
        start() with a date-based channel must call ensure_date_folders()
        instead of trying to create record_dir (which is None).
        """
        root = str(tmp_path / "rts1")
        cfg = _date_based_config(record_root=root)
        import sys
        cfg = cfg.model_copy(update={"ffmpeg_path": sys.executable})

        db = _make_db_mock()
        pm = ProcessManager()
        proc = _mock_process(returncode=None)

        ms = _mock_settings(tmp_path)

        with (
            patch("app.services.process_manager.subprocess.Popen", return_value=proc),
            patch("app.services.process_manager.get_settings", return_value=ms),
            # ensure_date_folders is imported inline inside start(); patch the source module
            patch("app.services.ffmpeg_builder.ensure_date_folders") as mock_edf,
        ):
            pm.start("rts1", cfg, db)

        mock_edf.assert_called_once()

    def test_start_date_based_no_record_dir_does_not_call_resolve_with_none(self, tmp_path):
        """
        start() with a date-based channel must never call
        resolve_channel_path(None), which would have produced the original
        'NoneType' TypeError.
        """
        root = str(tmp_path / "rts1")
        cfg = _date_based_config(record_root=root)
        import sys
        cfg = cfg.model_copy(update={"ffmpeg_path": sys.executable})

        db = _make_db_mock()
        pm = ProcessManager()
        proc = _mock_process(returncode=None)
        ms = _mock_settings(tmp_path)

        resolve_calls: list = []

        _real_resolve = resolve_channel_path

        def _tracking_resolve(path_str):
            resolve_calls.append(path_str)
            if path_str is None:
                raise AssertionError("resolve_channel_path called with None!")
            return _real_resolve(path_str)

        with (
            patch("app.services.process_manager.subprocess.Popen", return_value=proc),
            patch("app.services.process_manager.get_settings", return_value=ms),
            patch("app.services.process_manager.resolve_channel_path", side_effect=_tracking_resolve),
            patch("app.services.ffmpeg_builder.ensure_date_folders"),
        ):
            pm.start("rts1", cfg, db)

        # None must never appear in resolve_channel_path calls
        assert None not in resolve_calls, (
            f"resolve_channel_path was called with None. All calls: {resolve_calls}"
        )

    def test_start_missing_record_root_raises_value_error(self):
        """
        start() with date-based mode (use_date_folders=True) but no record_root
        must raise ValueError (preflight) rather than a cryptic TypeError.
        """
        cfg = _date_based_config(record_root=None, use_date_folders=True)
        db = _make_db_mock()
        pm = ProcessManager()

        with pytest.raises(ValueError, match="record_root"):
            pm.start("rts1", cfg, db)


# ---------------------------------------------------------------------------
# API layer — HTTP 400 on ValueError, HTTP 409 on RuntimeError
# ---------------------------------------------------------------------------

def _make_db_session(cfg_dict: dict):
    """Create an in-memory SQLite DB with a single channel seeded from cfg_dict."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        db.add(Channel(
            id=cfg_dict["id"],
            name=cfg_dict.get("name", cfg_dict["id"]),
            display_name=cfg_dict.get("display_name", cfg_dict["id"]),
            enabled=cfg_dict.get("enabled", True),
            config_json=json.dumps(cfg_dict),
        ))
        db.commit()
    return SessionLocal


class TestStartChannelAPIErrors:
    """
    Integration tests for POST /api/v1/channels/{id}/start via the FastAPI TestClient.

    Verifies:
    - ValueError from preflight → HTTP 400
    - RuntimeError (already running) → HTTP 409
    """

    def _app(self, SessionLocal):
        """Build the FastAPI app with its DB dependency overridden."""
        from fastapi import FastAPI
        from app.api.v1.channels import router as ch_router
        from app.api.v1 import auth as auth_router
        from app.db.session import get_db

        app = FastAPI()
        # channels router already has prefix="/channels"; add the /api/v1 prefix here
        app.include_router(ch_router, prefix="/api/v1")
        app.include_router(auth_router.router, prefix="/api/v1")

        def _override_db():
            with SessionLocal() as db:
                yield db

        app.dependency_overrides[get_db] = _override_db
        return app

    def _client(self, app, SessionLocal):
        from fastapi.testclient import TestClient
        from app.services.auth_service import create_user

        with SessionLocal() as db:
            create_user(db, "testadmin27", "testpw", "admin")

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/auth/login",
            data={"username": "testadmin27", "password": "testpw"},
        )
        token = resp.json()["access_token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        return client

    def test_start_returns_400_on_value_error(self, tmp_path):
        """
        When _preflight_check raises ValueError (bad config), the API must
        return HTTP 400 with the error message in the detail field.
        """
        cfg_dict = {
            "id": "bad_ch",
            "name": "Bad",
            "display_name": "Bad Channel",
            "enabled": True,
            "capture": {"device_type": "dshow"},
            "paths": {"use_date_folders": True},  # date-based but no record_root!
        }
        SessionLocal = _make_db_session(cfg_dict)
        app = self._app(SessionLocal)
        client = self._client(app, SessionLocal)

        resp = client.post("/api/v1/channels/bad_ch/start")

        assert resp.status_code == 400, resp.text
        assert "record_root" in resp.json()["detail"]

    def test_start_returns_409_when_already_running(self, tmp_path):
        """
        When the channel is already recording (RuntimeError), the API must
        return HTTP 409 Conflict.
        """
        root = str(tmp_path / "rts1")
        cfg_dict = {
            "id": "rts1_27",
            "name": "RTS1",
            "display_name": "RTS1",
            "enabled": True,
            "capture": {"device_type": "dshow"},
            "paths": {"record_root": root},
        }
        SessionLocal = _make_db_session(cfg_dict)
        app = self._app(SessionLocal)
        client = self._client(app, SessionLocal)

        from app.services.process_manager import get_process_manager
        pm = get_process_manager()

        # Simulate already-running state
        with patch.object(pm, "is_running", return_value=True):
            resp = client.post("/api/v1/channels/rts1_27/start")

        assert resp.status_code == 409, resp.text
        assert "already recording" in resp.json()["detail"]
