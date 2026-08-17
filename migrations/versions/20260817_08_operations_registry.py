"""scheduler, retention, registries and feedback

Revision ID: 20260817_08
Revises: 20260817_07
"""

import sqlalchemy as sa
from alembic import op

revision = "20260817_08"
down_revision = "20260817_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inference_jobs",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "source_schedules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "source_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "source_id", name="uq_source_schedules_tenant_source"),
    )
    op.create_index("ix_source_schedules_due", "source_schedules", ["is_enabled", "next_run_at"])
    op.create_table(
        "retention_policies",
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_versions_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("index_versions_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("run_history_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("audit_days", sa.Integer(), nullable=False, server_default="365"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "model_definitions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE")),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("endpoint_ref", sa.String(255), nullable=False),
        sa.Column("credential_ref", sa.String(255)),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("config_checksum", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "role", "model_id", "version", name="uq_models_version"),
    )
    op.create_index("ix_models_active", "model_definitions", ["tenant_id", "role", "is_active"])
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "name", "version", name="uq_prompts_version"),
    )
    op.create_index("ix_prompts_active", "prompt_templates", ["tenant_id", "name", "is_active"])
    op.create_table(
        "answer_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "answer_id", sa.Uuid(), sa.ForeignKey("answers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.String(1000)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("answer_id", "user_id", name="uq_feedback_answer_user"),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),
    )


def downgrade() -> None:
    op.drop_table("answer_feedback")
    op.drop_index("ix_prompts_active", table_name="prompt_templates")
    op.drop_table("prompt_templates")
    op.drop_index("ix_models_active", table_name="model_definitions")
    op.drop_table("model_definitions")
    op.drop_table("retention_policies")
    op.drop_index("ix_source_schedules_due", table_name="source_schedules")
    op.drop_table("source_schedules")
    op.drop_column("inference_jobs", "cancel_requested")
