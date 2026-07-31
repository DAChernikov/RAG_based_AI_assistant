"""Persist sanitized source-version ingestion failures.

Revision ID: 20260731_04
Revises: 20260731_03
"""

import sqlalchemy as sa
from alembic import op

revision = "20260731_04"
down_revision = "20260731_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_versions", sa.Column("failure_message", sa.String(500)))


def downgrade() -> None:
    op.drop_column("source_versions", "failure_message")
