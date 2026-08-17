"""Add pgvector hybrid index lifecycle and harden JDBC registry.

Revision ID: 20260817_07
Revises: 20260731_06
"""

import uuid

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "20260817_07"
down_revision = "20260731_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        UPDATE jdbc_driver_registry
        SET manifest_checksum = 'f9a977f43ffd5fa678871b0b563c76613ba9ab70fb3ca6480b138df47e5ed1b1'
        WHERE driver_id = 'postgresql' AND registry_version = '1'
        """
    )
    embedding_models = op.create_table(
        "embedding_model_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(20), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_id", "version", name="uq_embedding_model_version"),
    )
    op.bulk_insert(
        embedding_models,
        [
            {
                "id": uuid.UUID("e51ea62c-8f1a-4ea3-bf67-b9d4db7f6585"),
                "model_id": "BAAI/bge-m3",
                "version": "bge-m3/1",
                "dimensions": 1024,
                "contract_version": "1.0",
                "checksum": "bc785c3c9b927e7a143633351a7c9baea8eabd7d513e583051565bfdba0b088d",
                "is_enabled": True,
            }
        ],
    )
    op.create_table(
        "knowledge_index_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_model_version_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("failure_code", sa.String(100)),
        sa.Column("failure_message", sa.String(500)),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["embedding_model_version_id"], ["embedding_model_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "knowledge_base_id", "version_number", name="uq_knowledge_index_version_number"
        ),
    )
    op.create_index(
        "ix_knowledge_index_tenant_kb_status",
        "knowledge_index_versions",
        ["tenant_id", "knowledge_base_id", "status"],
    )
    op.create_index(
        "uq_knowledge_index_active",
        "knowledge_index_versions",
        ["tenant_id", "knowledge_base_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "chunk_embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_model_version_id", sa.Uuid(), nullable=False),
        sa.Column("text_checksum", sa.String(64), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["embedding_model_version_id"], ["embedding_model_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "text_checksum",
            "embedding_model_version_id",
            name="uq_chunk_embeddings_reuse",
        ),
    )
    op.create_index(
        "ix_chunk_embeddings_tenant_checksum", "chunk_embeddings", ["tenant_id", "text_checksum"]
    )
    op.create_index(
        "ix_chunk_embeddings_hnsw",
        "chunk_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "knowledge_index_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("index_version_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_version_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_embedding_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_uri", sa.Text(), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("path", sa.String(1000)),
        sa.Column("schema_name", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["index_version_id"], ["knowledge_index_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["source_id"], ["knowledge_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_version_id"], ["source_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["normalized_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["chunk_embedding_id"], ["chunk_embeddings.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("index_version_id", "chunk_id", name="uq_index_entries_chunk"),
    )
    op.create_index(
        "ix_index_entries_tenant_version",
        "knowledge_index_entries",
        ["tenant_id", "index_version_id"],
    )
    op.create_table(
        "indexing_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("index_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("checkpoint", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("lease_owner", sa.String(255)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["index_version_id"], ["knowledge_index_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_indexing_runs_tenant_key"),
    )
    op.create_index(
        "ix_indexing_runs_status_lease", "indexing_runs", ["status", "lease_expires_at"]
    )
    op.create_table(
        "indexing_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["indexing_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_indexing_events_run_created", "indexing_events", ["run_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_indexing_events_run_created", table_name="indexing_events")
    op.drop_table("indexing_events")
    op.drop_index("ix_indexing_runs_status_lease", table_name="indexing_runs")
    op.drop_table("indexing_runs")
    op.drop_index("ix_index_entries_tenant_version", table_name="knowledge_index_entries")
    op.drop_table("knowledge_index_entries")
    op.drop_index("ix_chunk_embeddings_hnsw", table_name="chunk_embeddings")
    op.drop_index("ix_chunk_embeddings_tenant_checksum", table_name="chunk_embeddings")
    op.drop_table("chunk_embeddings")
    op.drop_index("uq_knowledge_index_active", table_name="knowledge_index_versions")
    op.drop_index("ix_knowledge_index_tenant_kb_status", table_name="knowledge_index_versions")
    op.drop_table("knowledge_index_versions")
    op.drop_table("embedding_model_versions")
