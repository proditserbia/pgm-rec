"""Phase 25 — Recording Retention Cleanup.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-05

Changes
───────
segment_records
  + file_exists  BOOLEAN  NOT NULL DEFAULT TRUE
    When False, the retention cleaner has deleted the physical segment file.
    The DB/manifest row is preserved as an audit trail.

  + deleted_at   DATETIME  NULL
    UTC timestamp when the segment file was deleted by the retention cleaner.
    NULL means the file has not been deleted.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "segment_records",
        sa.Column(
            "file_exists",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "segment_records",
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("segment_records", "deleted_at")
    op.drop_column("segment_records", "file_exists")
