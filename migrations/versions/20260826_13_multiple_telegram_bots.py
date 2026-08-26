"""Allow multiple independently managed Telegram bots per tenant.

Revision ID: 20260826_13
Revises: 20260826_12
"""

import sqlalchemy as sa
from alembic import op

revision = "20260826_13"
down_revision = "20260826_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_bot_configurations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("token_credential_ref", sa.String(255), nullable=False),
        sa.Column("api_key_credential_ref", sa.String(255), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_test_status", sa.String(30)),
        sa.Column("last_tested_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_telegram_bots_tenant_name"),
    )
    op.create_index(
        "ix_telegram_bots_tenant_enabled",
        "telegram_bot_configurations",
        ["tenant_id", "is_enabled"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telegram_bots_tenant_enabled",
        table_name="telegram_bot_configurations",
    )
    op.drop_table("telegram_bot_configurations")
