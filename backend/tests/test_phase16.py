"""
Phase 1.6 unit tests.

Covers:
- HealthStatus new values (DEGRADED, COOLDOWN)
- ProcessInfo.update_stall_tracking()
- ProcessInfo.stall_seconds / last_file_size / last_file_size_change_at
- _RestartHistory: record_attempt, count_in_window, cooldown logic
- ProcessManager.attempt_auto_restart() backoff policy
- ProcessManager.get_health() returns COOLDOWN when history says so
- Safe file mover: _is_size_stable() logic
- Settings: new Phase 1.6 fields have expected defaults
- ChannelDebugResponse schema validation
- SystemHealthResponse includes degraded + cooldown fields
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.models.schemas import ChannelDebugResponse, HealthStatus, SystemHealthResponse
from app.services.process_manager import (
    ProcessInfo,
    ProcessManager,
    _RestartHistory,
    get_process_manager,
)


# ─── HealthStatus enum ────────────────────────────────────────────────────────

def test_health_status_has_degraded_and_cooldown():
    assert HealthStatus.DEGRADED == "degraded"
    assert HealthStatus.COOLDOWN == "cooldown"


def test_health_status_all_values():
    values = {s.value for s in HealthStatus}
    assert {"healthy", "unhealthy", "degraded", "cooldown", "unknown"} == values


# ─── ProcessInfo stall tracking ───────────────────────────────────────────────

def _make_info(channel_id: str = "test") -> ProcessInfo:
    return ProcessInfo(
        channel_id=channel_id,
        pid=9999,
        log_path=Path("/tmp/test.log"),
        started_at=datetime.utcnow(),
        process=None,
    )


def test_stall_tracking_new_file_resets_and_returns_growing():
    info = _make_info()
    # First call with a new file should be treated as growing
    result = info.update_stall_tracking("/tmp/seg1.mp4", 1024)
    assert result is True
    assert info.last_file_size == 1024
    assert info.last_file_size_change_at is not None


def test_stall_tracking_size_unchanged_returns_false():
    info = _make_info()
    info.update_stall_tracking("/tmp/seg1.mp4", 1024)
    # Same file, same size → stalled
    result = info.update_stall_tracking("/tmp/seg1.mp4", 1024)
    assert result is False


def test_stall_tracking_size_growing_returns_true():
    info = _make_info()
    info.update_stall_tracking("/tmp/seg1.mp4", 1024)
    # Size grew
    result = info.update_stall_tracking("/tmp/seg1.mp4", 2048)
    assert result is True
    assert info.last_file_size == 2048


def test_stall_tracking_new_file_resets_staleness():
    info = _make_info()
    info.update_stall_tracking("/tmp/seg1.mp4", 512)
    info.update_stall_tracking("/tmp/seg1.mp4", 512)  # stalled
    # New file starts — should reset (return True, not stalled)
    result = info.update_stall_tracking("/tmp/seg2.mp4", 100)
    assert result is True


def test_stall_seconds_none_before_any_tracking():
    info = _make_info()
    assert info.stall_seconds is None


def test_stall_seconds_after_update():
    info = _make_info()
    info.update_stall_tracking("/tmp/seg1.mp4", 1024)
    # Should be a very small number (just updated)
    assert info.stall_seconds is not None
    assert info.stall_seconds < 5.0


# ─── _RestartHistory ──────────────────────────────────────────────────────────

def test_restart_history_count_in_window():
    h = _RestartHistory()
    h.record_attempt()
    h.record_attempt()
    assert h.count_in_window(60) == 2


def test_restart_history_window_excludes_old():
    h = _RestartHistory()
    # Manually inject an old timestamp
    h._timestamps.append(datetime.utcnow() - timedelta(seconds=400))
    h.record_attempt()  # recent
    # Window of 300s should only count the recent one
    assert h.count_in_window(300) == 1


def test_restart_history_cooldown():
    h = _RestartHistory()
    assert not h.is_in_cooldown()
    h.enter_cooldown(60)
    assert h.is_in_cooldown()
    remaining = h.cooldown_remaining_seconds()
    assert 55.0 < remaining <= 60.0


def test_restart_history_exit_cooldown():
    h = _RestartHistory()
    h.enter_cooldown(60)
    assert h.is_in_cooldown()
    h.exit_cooldown()
    assert not h.is_in_cooldown()


def test_restart_history_last_restart_time_none_when_empty():
    h = _RestartHistory()
    assert h.last_restart_time() is None


def test_restart_history_last_restart_time_after_attempt():
    h = _RestartHistory()
    before = datetime.utcnow()
    h.record_attempt()
    after = datetime.utcnow()
    lrt = h.last_restart_time()
    assert lrt is not None
    assert before <= lrt <= after


# ─── ProcessManager restart backoff ──────────────────────────────────────────

def _fresh_manager() -> ProcessManager:
    return ProcessManager()


def test_attempt_auto_restart_allows_within_limit():
    pm = _fresh_manager()
    # With max_restarts=5, first 5 attempts should be allowed
    with patch.object(
        type(pm), "attempt_auto_restart", wraps=pm.attempt_auto_restart
    ):
        # Override settings to small window so we can test quickly
        with patch("app.services.process_manager.get_settings") as mock_settings:
            cfg = MagicMock()
            cfg.restart_backoff_max_restarts = 3
            cfg.restart_backoff_window_seconds = 300
            cfg.restart_cooldown_seconds = 60
            cfg.restart_pre_delay_seconds = 0
            mock_settings.return_value = cfg

            results = [pm.attempt_auto_restart("ch1") for _ in range(3)]
    assert all(results), "First 3 attempts should be allowed"


def test_attempt_auto_restart_blocks_after_limit():
    pm = _fresh_manager()
    with patch("app.services.process_manager.get_settings") as mock_settings:
        cfg = MagicMock()
        cfg.restart_backoff_max_restarts = 3
        cfg.restart_backoff_window_seconds = 300
        cfg.restart_cooldown_seconds = 60
        cfg.stop_timeout_seconds = 5
        cfg.restart_pre_delay_seconds = 0
        mock_settings.return_value = cfg

        # Drain the allowance
        for _ in range(3):
            pm.attempt_auto_restart("ch1")
        # 4th attempt (count > max_restarts=3) should enter cooldown
        result = pm.attempt_auto_restart("ch1")
        assert result is False


def test_get_health_returns_cooldown_when_in_history_cooldown():
    pm = _fresh_manager()
    # Inject a cooldown directly
    hist = pm._get_or_create_history("ch1")
    hist.enter_cooldown(120)
    assert pm.get_health("ch1") == HealthStatus.COOLDOWN


def test_get_cooldown_remaining_returns_positive():
    pm = _fresh_manager()
    hist = pm._get_or_create_history("ch1")
    hist.enter_cooldown(60)
    remaining = pm.get_cooldown_remaining("ch1")
    assert 55.0 < remaining <= 60.0


def test_get_last_restart_time_none_without_history():
    pm = _fresh_manager()
    assert pm.get_last_restart_time("ch1") is None


# ─── Safe file mover: _is_size_stable ────────────────────────────────────────

def test_is_size_stable_stable_file(tmp_path):
    from app.services.file_mover import _is_size_stable
    f = tmp_path / "test.mp4"
    f.write_bytes(b"x" * 1024)
    # Very short check interval for tests
    assert _is_size_stable(f, 0.01) is True


def test_is_size_stable_growing_file(tmp_path):
    from app.services.file_mover import _is_size_stable
    import threading

    f = tmp_path / "growing.mp4"
    f.write_bytes(b"x" * 512)

    # Write more bytes after a tiny delay (simulates FFmpeg still writing)
    def write_more():
        time.sleep(0.02)
        with open(f, "ab") as fh:
            fh.write(b"y" * 512)

    t = threading.Thread(target=write_more)
    t.start()
    result = _is_size_stable(f, 0.05)
    t.join()
    # File grew between the two reads → not stable
    assert result is False


def test_is_size_stable_missing_file(tmp_path):
    from app.services.file_mover import _is_size_stable
    assert _is_size_stable(tmp_path / "nonexistent.mp4", 0.01) is False


def test_is_size_stable_empty_file(tmp_path):
    from app.services.file_mover import _is_size_stable
    f = tmp_path / "empty.mp4"
    f.write_bytes(b"")
    assert _is_size_stable(f, 0.01) is False


# ─── Settings defaults ────────────────────────────────────────────────────────

def test_settings_phase16_defaults():
    from app.config.settings import Settings
    s = Settings()
    assert s.stall_detection_seconds == 60
    assert s.restart_backoff_max_restarts == 5
    assert s.restart_backoff_window_seconds == 300
    assert s.restart_cooldown_seconds == 120
    assert s.restart_pre_delay_seconds == 2.0
    assert s.file_mover_stability_check_seconds == 1.0


# ─── Schema validation ────────────────────────────────────────────────────────

def test_channel_debug_response_schema():
    r = ChannelDebugResponse(
        channel_id="rts1",
        health=HealthStatus.DEGRADED,
        pid=1234,
        last_restart_time=None,
        restart_count_window=3,
        cooldown_remaining_seconds=0.0,
        last_segment_time=None,
        last_file_size=None,
        last_file_size_change_at=None,
        stall_seconds=None,
    )
    assert r.channel_id == "rts1"
    assert r.health == HealthStatus.DEGRADED
    assert r.restart_count_window == 3


def test_system_health_response_includes_degraded_cooldown():
    r = SystemHealthResponse(
        channels=[],
        total=0,
        running=0,
        healthy=0,
        unhealthy=0,
        degraded=2,
        cooldown=1,
        unknown=0,
    )
    assert r.degraded == 2
    assert r.cooldown == 1
