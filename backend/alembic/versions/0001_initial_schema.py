"""Initial schema — all tables for PGMRec Phase 1–6.2.

Revision ID: 0001
Revises:
Create Date: 2026-04-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── channels ──────────────────────────────────────────────────────────
    op.create_table(
        "channels",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # ── process_records ───────────────────────────────────────────────────
    op.create_table(
        "process_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(64), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="stopped"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("stopped_at", sa.DateTime(), nullable=True),
        sa.Column("log_path", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("adopted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_process_records_channel_id", "process_records", ["channel_id"])

    # ── watchdog_events ───────────────────────────────────────────────────
    op.create_table(
        "watchdog_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(64), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
    )
    op.create_index("ix_watchdog_events_channel_id", "watchdog_events", ["channel_id"])

    # ── segment_anomalies ─────────────────────────────────────────────────
    op.create_table(
        "segment_anomalies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(64), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("last_segment_time", sa.DateTime(), nullable=True),
        sa.Column("expected_interval_seconds", sa.Float(), nullable=False),
        sa.Column("actual_gap_seconds", sa.Float(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_segment_anomalies_channel_id", "segment_anomalies", ["channel_id"])

    # ── segment_records ───────────────────────────────────────────────────
    op.create_table(
        "segment_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(64), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("start_time", sa.DateTime(), nullable=False),
        sa.Column("end_time", sa.DateTime(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="complete"),
        sa.Column("ffprobe_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("manifest_date", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("channel_id", "filename", name="uq_segment_channel_filename"),
    )
    op.create_index("ix_segment_records_channel_id", "segment_records", ["channel_id"])
    op.create_index("ix_segment_records_start_time", "segment_records", ["start_time"])
    op.create_index("ix_segment_records_manifest_date", "segment_records", ["manifest_date"])

    # ── manifest_gaps ─────────────────────────────────────────────────────
    op.create_table(
        "manifest_gaps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(64), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manifest_date", sa.String(10), nullable=False),
        sa.Column("gap_start", sa.DateTime(), nullable=False),
        sa.Column("gap_end", sa.DateTime(), nullable=False),
        sa.Column("gap_seconds", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_manifest_gaps_channel_id", "manifest_gaps", ["channel_id"])
    op.create_index("ix_manifest_gaps_manifest_date", "manifest_gaps", ["manifest_date"])

    # ── export_jobs ───────────────────────────────────────────────────────
    op.create_table(
        "export_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("date", sa.String(10), nullable=False),
        sa.Column("in_time", sa.String(8), nullable=False),
        sa.Column("out_time", sa.String(8), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("progress_percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("output_path", sa.Text(), nullable=True),
        sa.Column("log_path", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("has_gaps", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("actual_duration_seconds", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_export_jobs_channel_id", "export_jobs", ["channel_id"])
    op.create_index("ix_export_jobs_status", "export_jobs", ["status"])

    # ── users ─────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="preview"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"])

    # ── restart_history (Phase 6.2) ───────────────────────────────────────
    op.create_table(
        "restart_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(64), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempted_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_restart_history_channel_id", "restart_history", ["channel_id"])
    op.create_index("ix_restart_history_attempted_at", "restart_history", ["attempted_at"])


def downgrade() -> None:
    op.drop_table("restart_history")
    op.drop_table("users")
    op.drop_table("export_jobs")
    op.drop_table("manifest_gaps")
    op.drop_table("segment_records")
    op.drop_table("segment_anomalies")
    op.drop_table("watchdog_events")
    op.drop_table("process_records")
    op.drop_table("channels")
