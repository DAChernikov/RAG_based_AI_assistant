"""Add UI-first setup, encrypted credential references and Telegram configuration.

Revision ID: 20260820_11
Revises: 20260817_10
"""

import sqlalchemy as sa
from alembic import op

revision = "20260820_11"
down_revision = "20260817_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_setup",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid()),
        sa.Column("administrator_id", sa.Uuid()),
        sa.Column("current_step", sa.String(30), nullable=False, server_default="administrator"),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("bootstrap_completed_at", sa.DateTime(timezone=True)),
        sa.Column("onboarding_completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["administrator_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.execute(
        "INSERT INTO system_setup (id, current_step, config_version) VALUES (1, 'administrator', 1)"
    )
    # Existing installations already have an administrator and must not reopen first-run setup.
    op.execute("""
        UPDATE system_setup
        SET tenant_id = existing.tenant_id,
            administrator_id = existing.id,
            current_step = 'complete',
            bootstrap_completed_at = now(),
            onboarding_completed_at = now()
        FROM (
            SELECT id, tenant_id FROM users
            WHERE role = 'admin'
            ORDER BY created_at, id
            LIMIT 1
        ) AS existing
        WHERE system_setup.id = 1
        """)
    op.create_table(
        "credential_secrets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("reference", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("masked_value", sa.String(32), nullable=False, server_default="••••••••"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "reference", name="uq_credential_secrets_tenant_ref"),
    )
    op.create_index(
        "ix_credential_secrets_tenant_updated",
        "credential_secrets",
        ["tenant_id", "updated_at"],
    )
    op.create_table(
        "telegram_configurations",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("token_credential_ref", sa.String(255)),
        sa.Column("api_key_credential_ref", sa.String(255)),
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
    )


def downgrade() -> None:
    op.drop_table("telegram_configurations")
    op.drop_index("ix_credential_secrets_tenant_updated", table_name="credential_secrets")
    op.drop_table("credential_secrets")
    op.drop_table("system_setup")
