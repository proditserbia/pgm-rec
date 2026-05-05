"""Phase 24 — Daily Archive Export.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-05

Changes
───────
export_jobs
  + job_source  VARCHAR(32)  NOT NULL DEFAULT 'manual'
    Identifies the origin of an export job.
    Values: "manual" (user-created via API) | "daily_archive" (system-created).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "export_jobs",
        sa.Column(
            "job_source",
            sa.String(32),
            nullable=False,
            server_default="manual",
        ),
    )


def downgrade() -> None:
    op.drop_column("export_jobs", "job_source")
