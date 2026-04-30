"""Phase 7 — Alert System + Export Wrap + Segment Flags.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-30

Changes
───────
watchdog_events
  + alert_type    VARCHAR(64)  NULL   — broadcast alert classification
  + severity      INTEGER      NULL   — 0=info | 1=warning | 2=error

segment_records
  + never_expires BOOLEAN      NOT NULL DEFAULT FALSE
  + has_freeze    BOOLEAN      NULL   — schema preparation (detection TBD)
  + has_silence   BOOLEAN      NULL   — schema preparation (detection TBD)

export_jobs
  + preroll_seconds  FLOAT  NOT NULL DEFAULT 0
  + postroll_seconds FLOAT  NOT NULL DEFAULT 0
  + never_expires    BOOLEAN NOT NULL DEFAULT FALSE
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── watchdog_events ───────────────────────────────────────────────────
    op.add_column(
        "watchdog_events",
        sa.Column("alert_type", sa.String(64), nullable=True),
    )
    op.add_column(
        "watchdog_events",
        sa.Column("severity", sa.Integer(), nullable=True, server_default="0"),
    )

    # ── segment_records ───────────────────────────────────────────────────
    op.add_column(
        "segment_records",
        sa.Column(
            "never_expires",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "segment_records",
        sa.Column("has_freeze", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "segment_records",
        sa.Column("has_silence", sa.Boolean(), nullable=True),
    )

    # ── export_jobs ───────────────────────────────────────────────────────
    op.add_column(
        "export_jobs",
        sa.Column(
            "preroll_seconds",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "export_jobs",
        sa.Column(
            "postroll_seconds",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "export_jobs",
        sa.Column(
            "never_expires",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    # ── export_jobs ───────────────────────────────────────────────────────
    op.drop_column("export_jobs", "never_expires")
    op.drop_column("export_jobs", "postroll_seconds")
    op.drop_column("export_jobs", "preroll_seconds")

    # ── segment_records ───────────────────────────────────────────────────
    op.drop_column("segment_records", "has_silence")
    op.drop_column("segment_records", "has_freeze")
    op.drop_column("segment_records", "never_expires")

    # ── watchdog_events ───────────────────────────────────────────────────
    op.drop_column("watchdog_events", "severity")
    op.drop_column("watchdog_events", "alert_type")
