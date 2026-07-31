"""Add authentication, RBAC, API keys, audit log and job leases.

Revision ID: 20260731_02
Revises: 20260730_01
"""

import sqlalchemy as sa
from alembic import op

revision = "20260731_02"
down_revision = "20260730_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(100), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.String(500), nullable=True))
    op.add_column("users", sa.Column("role", sa.String(20), server_default="user", nullable=False))
    op.add_column(
        "users", sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False)
    )
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True)))
    op.execute(
        """
        UPDATE users
        SET username = COALESCE(NULLIF(external_id, ''), 'user-' || substring(id::text, 1, 12))
        """
    )
    op.alter_column("users", "username", nullable=False)
    op.create_unique_constraint("uq_users_tenant_username", "users", ["tenant_id", "username"])

    op.add_column(
        "conversations",
        sa.Column("next_message_sequence", sa.Integer(), server_default="1", nullable=False),
    )
    op.execute(
        """
        UPDATE conversations AS c
        SET next_message_sequence = COALESCE(
            (SELECT MAX(m.sequence_number) + 1 FROM messages AS m WHERE m.conversation_id = c.id),
            1
        )
        """
    )

    op.add_column("inference_jobs", sa.Column("lease_owner", sa.String(255)))
    op.add_column("inference_jobs", sa.Column("lease_token", sa.Uuid()))
    op.add_column("inference_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.create_index("ix_inference_jobs_lease_expires", "inference_jobs", ["lease_expires_at"])

    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("replaced_by_id", sa.Uuid()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["replaced_by_id"], ["refresh_sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_refresh_sessions_user_created", "refresh_sessions", ["user_id", "created_at"]
    )
    op.create_index("ix_refresh_sessions_family", "refresh_sessions", ["family_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(20), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_user_created", "api_keys", ["user_id", "created_at"])
    op.create_index("ix_api_keys_tenant_prefix", "api_keys", ["tenant_id", "prefix"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid()),
        sa.Column("actor_user_id", sa.Uuid()),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(100)),
        sa.Column("resource_id", sa.String(255)),
        sa.Column("correlation_id", sa.Uuid()),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_tenant_created", "audit_events", ["tenant_id", "created_at"])
    op.create_index("ix_audit_events_action_created", "audit_events", ["action", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_action_created", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_api_keys_tenant_prefix", table_name="api_keys")
    op.drop_index("ix_api_keys_user_created", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_refresh_sessions_family", table_name="refresh_sessions")
    op.drop_index("ix_refresh_sessions_user_created", table_name="refresh_sessions")
    op.drop_table("refresh_sessions")
    op.drop_index("ix_inference_jobs_lease_expires", table_name="inference_jobs")
    op.drop_column("inference_jobs", "lease_expires_at")
    op.drop_column("inference_jobs", "lease_token")
    op.drop_column("inference_jobs", "lease_owner")
    op.drop_column("conversations", "next_message_sequence")
    op.drop_constraint("uq_users_tenant_username", "users", type_="unique")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "is_active")
    op.drop_column("users", "role")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "username")
