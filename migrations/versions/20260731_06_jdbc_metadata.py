"""Add the managed JDBC metadata driver registry.

Revision ID: 20260731_06
Revises: 20260731_05
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "20260731_06"
down_revision = "20260731_05"
branch_labels = None
depends_on = None

_POSTGRESQL_DRIVER_ID = uuid.UUID("f5d8d2b0-78d8-4b2e-b9b4-cc4de5174961")
_MANIFEST_CHECKSUM = "e7be8ed45d683286c9ec548c1528e86c777b7fc6bdb6542f4d4c1c4b58e23c90"


def upgrade() -> None:
    table = op.create_table(
        "jdbc_driver_registry",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("driver_id", sa.String(100), nullable=False),
        sa.Column("registry_version", sa.String(20), nullable=False),
        sa.Column("dialect", sa.String(50), nullable=False),
        sa.Column("adapter", sa.String(100), nullable=False),
        sa.Column("adapter_version", sa.String(50), nullable=False),
        sa.Column("manifest_checksum", sa.String(64), nullable=False),
        sa.Column("allowed_properties", sa.JSON(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "driver_id", "registry_version", name="uq_jdbc_driver_registry_version"
        ),
    )
    op.bulk_insert(
        table,
        [
            {
                "id": _POSTGRESQL_DRIVER_ID,
                "driver_id": "postgresql",
                "registry_version": "1",
                "dialect": "postgresql",
                "adapter": "psycopg",
                "adapter_version": "3.2.4",
                "manifest_checksum": _MANIFEST_CHECKSUM,
                "allowed_properties": ["sslmode"],
                "is_enabled": True,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("jdbc_driver_registry")
