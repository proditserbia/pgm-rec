"""SQLAlchemy ORM models for PGMRec."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Phase 4 — valid role values
ROLES = ("admin", "export", "preview")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Channel(Base):
    """Persisted channel record; config_json holds the full ChannelConfig."""

    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    process_records: Mapped[list["ProcessRecord"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan", order_by="ProcessRecord.id"
    )
    watchdog_events: Mapped[list["WatchdogEvent"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan", order_by="WatchdogEvent.id"
    )
    segment_anomalies: Mapped[list["SegmentAnomaly"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan", order_by="SegmentAnomaly.id"
    )
    segment_records: Mapped[list["SegmentRecord"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan", order_by="SegmentRecord.start_time"
    )
    manifest_gaps: Mapped[list["ManifestGap"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan", order_by="ManifestGap.gap_start"
    )


class ProcessRecord(Base):
    """Audit trail for each start/stop event of a channel recording process."""

    __tablename__ = "process_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="stopped", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    log_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set to True when we adopted an orphaned PID from a previous server run
    adopted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    channel: Mapped["Channel"] = relationship(back_populates="process_records")


class WatchdogEvent(Base):
    """
    One row per watchdog detection event (process dead, stale output, auto-restart).

    event_type values: process_dead | no_new_files | segment_gap | auto_restarted | adopted
    alert_type values: loss_of_recording | freeze | silence | black  (Phase 7, nullable)
    severity: 0=info | 1=warning | 2=error  (Phase 7, nullable — defaults to 0)
    """

    __tablename__ = "watchdog_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)  # free-text / JSON snippet
    # Phase 7 — broadcast alert classification
    alert_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)

    channel: Mapped["Channel"] = relationship(back_populates="watchdog_events")


class SegmentAnomaly(Base):
    """
    Detected gap in the segment output stream for a channel.

    Created when the newest file in 1_record is older than
    (segment_time + tolerance) seconds.
    """

    __tablename__ = "segment_anomalies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    last_segment_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expected_interval_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    actual_gap_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    channel: Mapped["Channel"] = relationship(back_populates="segment_anomalies")


class SegmentRecord(Base):
    """
    Index record for one completed recording segment — Phase 2A.

    Mirrors the segment entries in the per-channel daily JSON manifest.
    The JSON manifest is the source of truth; this table enables fast API
    queries without reading files from disk.

    Unique constraint: (channel_id, filename) — one row per segment file.
    """

    __tablename__ = "segment_records"
    __table_args__ = (
        UniqueConstraint("channel_id", "filename", name="uq_segment_channel_filename"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    # Both stored as UTC datetime objects
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # "complete" | "partial" | "error"
    status: Mapped[str] = mapped_column(String(32), default="complete", nullable=False)
    ffprobe_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # YYYY-MM-DD string in the channel's local timezone (for manifest partitioning)
    manifest_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    # Phase 7 — broadcast segment flags (schema preparation; detection not yet implemented)
    never_expires: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_freeze: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_silence: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    channel: Mapped["Channel"] = relationship(back_populates="segment_records")


class ManifestGap(Base):
    """
    A detected gap between two consecutive recording segments — Phase 2A.

    Created when the time between seg[n].end_time and seg[n+1].start_time
    exceeds manifest_gap_tolerance_seconds.
    """

    __tablename__ = "manifest_gaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    manifest_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    gap_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    gap_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    gap_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    channel: Mapped["Channel"] = relationship(back_populates="manifest_gaps")


class ExportJob(Base):
    """
    An asynchronous video export job — Phase 2B.

    Lifecycle: queued → running → completed | failed | cancelled

    The job stores everything needed to reproduce the export (channel, date,
    time range) and track its progress.  Output files live under
    data/exports/{channel_id}/{date}/.
    """

    __tablename__ = "export_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # channel_id is stored as a plain indexed string — no FK so export history
    # survives channel deletions or renames.
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False)       # YYYY-MM-DD
    in_time: Mapped[str] = mapped_column(String(8), nullable=False)     # HH:MM:SS
    out_time: Mapped[str] = mapped_column(String(8), nullable=False)    # HH:MM:SS
    # "queued" | "running" | "completed" | "failed" | "cancelled"
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    progress_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True if gaps were detected in the resolved range (warning, not a blocker)
    has_gaps: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Phase 2C: actual duration measured by ffprobe after export completes
    actual_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Phase 7 — export pre/post roll wrap (seconds; 0 = no wrap)
    preroll_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    postroll_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Phase 7 — preservation flag; if True, retention cleanup skips this job's output
    never_expires: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class User(Base):
    """
    System user — Phase 4.

    role: "admin" | "export" | "preview"
    password_hash: bcrypt-hashed; never exposed in API responses.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="preview", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class RestartHistoryRecord(Base):
    """
    Persisted per-channel FFmpeg restart attempt — Phase 6.2.

    Supplements the in-memory _RestartHistory so that backoff counters survive
    a server restart.  Loaded into memory on startup; written on every auto-restart
    attempt.  Old rows are pruned by the retention scheduler.
    """

    __tablename__ = "restart_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False, index=True)
