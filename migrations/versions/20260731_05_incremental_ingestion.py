"""Add durable incremental ingestion jobs and content-addressed documents.

Revision ID: 20260731_05
Revises: 20260731_04
"""

import sqlalchemy as sa
from alembic import op

revision = "20260731_05"
down_revision = "20260731_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ingestion_runs") as batch:
        batch.add_column(
            sa.Column(
                "operation",
                sa.String(100),
                server_default="refresh_source",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("idempotency_key", sa.String(255)))
        batch.add_column(sa.Column("request_hash", sa.String(64)))
        batch.add_column(sa.Column("correlation_id", sa.Uuid()))
        batch.add_column(
            sa.Column("auto_activate", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False)
        )
        batch.add_column(
            sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch.add_column(sa.Column("lease_owner", sa.String(255)))
        batch.add_column(sa.Column("lease_token", sa.Uuid()))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
        batch.alter_column("started_at", existing_type=sa.DateTime(timezone=True), nullable=True)
        batch.create_unique_constraint(
            "uq_ingestion_runs_tenant_operation_key",
            ["tenant_id", "operation", "idempotency_key"],
        )
    op.create_index(
        "ix_ingestion_runs_status_lease", "ingestion_runs", ["status", "lease_expires_at"]
    )

    op.create_table(
        "content_blobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "checksum", name="uq_content_blobs_tenant_checksum"),
    )
    op.create_index("ix_content_blobs_tenant_created", "content_blobs", ["tenant_id", "created_at"])

    op.create_table(
        "normalized_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source_version_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(1000), nullable=False),
        sa.Column("canonical_uri", sa.Text(), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("content_blob_id", sa.Uuid(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_version_id"], ["source_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["content_blob_id"], ["content_blobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_version_id", "object_key", name="uq_normalized_documents_version_key"
        ),
    )
    op.create_index(
        "ix_normalized_documents_tenant_version",
        "normalized_documents",
        ["tenant_id", "source_version_id"],
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("content_blob_id", sa.Uuid(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["normalized_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["content_blob_id"], ["content_blobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_index"),
    )
    op.create_index(
        "ix_document_chunks_tenant_document",
        "document_chunks",
        ["tenant_id", "document_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_tenant_document", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_normalized_documents_tenant_version", table_name="normalized_documents")
    op.drop_table("normalized_documents")
    op.drop_index("ix_content_blobs_tenant_created", table_name="content_blobs")
    op.drop_table("content_blobs")
    op.drop_index("ix_ingestion_runs_status_lease", table_name="ingestion_runs")
    op.execute(
        sa.text("UPDATE ingestion_runs SET started_at = created_at WHERE started_at IS NULL")
    )
    with op.batch_alter_table("ingestion_runs") as batch:
        batch.drop_constraint("uq_ingestion_runs_tenant_operation_key", type_="unique")
        batch.alter_column("started_at", existing_type=sa.DateTime(timezone=True), nullable=False)
        for name in (
            "lease_expires_at",
            "lease_token",
            "lease_owner",
            "cancel_requested",
            "max_attempts",
            "attempt_count",
            "auto_activate",
            "correlation_id",
            "request_hash",
            "idempotency_key",
            "operation",
        ):
            batch.drop_column(name)
