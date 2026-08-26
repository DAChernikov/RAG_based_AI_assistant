"""Add conversation actions and update the default generation model.

Revision ID: 20260826_14
Revises: 20260826_13
"""

import hashlib
import uuid

import sqlalchemy as sa
from alembic import op

revision = "20260826_14"
down_revision = "20260826_13"
branch_labels = None
depends_on = None

_GENERATION_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")
_NEW_MODEL = "qwen2.5-coder:14b"
_NEW_VERSION = "qwen2.5-coder/14b"
_OLD_MODEL = "qwen2.5-coder:7b"
_OLD_VERSION = "qwen2.5-coder/7b-q4_k_m"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _set_global_default(model_id: str, version: str) -> None:
    op.execute(
        sa.text(
            "UPDATE model_definitions SET model_id = :model_id, version = :version, "
            "config_checksum = :checksum WHERE id = :model_definition_id AND tenant_id IS NULL"
        ).bindparams(
            model_id=model_id,
            version=version,
            checksum=_sha(f"generation:{version}"),
            model_definition_id=_GENERATION_ID,
        )
    )


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_conversations_owner_pinned_updated",
        "conversations",
        ["tenant_id", "user_id", "pinned", "updated_at"],
    )
    _set_global_default(_NEW_MODEL, _NEW_VERSION)


def downgrade() -> None:
    _set_global_default(_OLD_MODEL, _OLD_VERSION)
    op.drop_index("ix_conversations_owner_pinned_updated", table_name="conversations")
    op.drop_column("conversations", "pinned")
