"""Enforce tenant ownership through composite database constraints.

Revision ID: 20260817_09
Revises: 20260817_08
"""

from alembic import op

revision = "20260817_09"
down_revision = "20260817_08"
branch_labels = None
depends_on = None


_UNIQUE = (
    ("knowledge_bases", "uq_knowledge_bases_tenant_id"),
    ("knowledge_sources", "uq_knowledge_sources_tenant_id"),
    ("source_versions", "uq_source_versions_tenant_id"),
    ("normalized_documents", "uq_documents_tenant_id"),
    ("document_chunks", "uq_chunks_tenant_id"),
    ("chunk_embeddings", "uq_embeddings_tenant_id"),
    ("knowledge_index_versions", "uq_indexes_tenant_id"),
    ("indexing_runs", "uq_indexing_runs_tenant_id"),
)

_FOREIGN_KEYS = (
    (
        "fk_kb_sources_tenant_kb",
        "knowledge_base_sources",
        "knowledge_bases",
        ["tenant_id", "knowledge_base_id"],
        ["tenant_id", "id"],
    ),
    (
        "fk_kb_sources_tenant_source",
        "knowledge_base_sources",
        "knowledge_sources",
        ["tenant_id", "source_id"],
        ["tenant_id", "id"],
    ),
    (
        "fk_source_versions_tenant_source",
        "source_versions",
        "knowledge_sources",
        ["tenant_id", "source_id"],
        ["tenant_id", "id"],
    ),
    (
        "fk_indexes_tenant_kb",
        "knowledge_index_versions",
        "knowledge_bases",
        ["tenant_id", "knowledge_base_id"],
        ["tenant_id", "id"],
    ),
    (
        "fk_index_entries_tenant_index",
        "knowledge_index_entries",
        "knowledge_index_versions",
        ["tenant_id", "index_version_id"],
        ["tenant_id", "id"],
    ),
    (
        "fk_index_entries_tenant_source",
        "knowledge_index_entries",
        "knowledge_sources",
        ["tenant_id", "source_id"],
        ["tenant_id", "id"],
    ),
    (
        "fk_index_entries_tenant_version",
        "knowledge_index_entries",
        "source_versions",
        ["tenant_id", "source_version_id"],
        ["tenant_id", "id"],
    ),
    (
        "fk_index_entries_tenant_document",
        "knowledge_index_entries",
        "normalized_documents",
        ["tenant_id", "document_id"],
        ["tenant_id", "id"],
    ),
    (
        "fk_index_entries_tenant_chunk",
        "knowledge_index_entries",
        "document_chunks",
        ["tenant_id", "chunk_id"],
        ["tenant_id", "id"],
    ),
    (
        "fk_index_entries_tenant_embedding",
        "knowledge_index_entries",
        "chunk_embeddings",
        ["tenant_id", "chunk_embedding_id"],
        ["tenant_id", "id"],
    ),
    (
        "fk_index_runs_tenant_kb",
        "indexing_runs",
        "knowledge_bases",
        ["tenant_id", "knowledge_base_id"],
        ["tenant_id", "id"],
    ),
    (
        "fk_index_runs_tenant_index",
        "indexing_runs",
        "knowledge_index_versions",
        ["tenant_id", "index_version_id"],
        ["tenant_id", "id"],
    ),
)


def upgrade() -> None:
    for table, name in _UNIQUE:
        op.create_unique_constraint(name, table, ["tenant_id", "id"])
    for name, source, target, local, remote in _FOREIGN_KEYS:
        op.create_foreign_key(name, source, target, local, remote, ondelete="CASCADE")


def downgrade() -> None:
    for name, source, _target, _local, _remote in reversed(_FOREIGN_KEYS):
        op.drop_constraint(name, source, type_="foreignkey")
    for table, name in reversed(_UNIQUE):
        op.drop_constraint(name, table, type_="unique")
