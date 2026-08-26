"""Add the default prompt for retrieval-free model conversations.

Revision ID: 20260826_12
Revises: 20260820_11
"""

import hashlib
import uuid

import sqlalchemy as sa
from alembic import op

revision = "20260826_12"
down_revision = "20260820_11"
branch_labels = None
depends_on = None

_PROMPT_ID = uuid.UUID("20000000-0000-4000-8000-000000000003")
_TEMPLATE = (
    "You are a helpful self-hosted assistant. Answer from the model's own knowledge. "
    "Be explicit when information may be uncertain or outdated. Do not invent sources or "
    "claim that a knowledge base was consulted.\n\nQuestion:\n{question}\n\nAnswer:"
)


def upgrade() -> None:
    prompts = sa.table(
        "prompt_templates",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("version", sa.String()),
        sa.column("template", sa.Text()),
        sa.column("checksum", sa.String()),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(
        prompts,
        [
            {
                "id": _PROMPT_ID,
                "tenant_id": None,
                "name": "general-answer",
                "version": "1.0",
                "template": _TEMPLATE,
                "checksum": hashlib.sha256(_TEMPLATE.encode()).hexdigest(),
                "is_active": True,
            }
        ],
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM prompt_templates WHERE id = :prompt_id").bindparams(
            prompt_id=_PROMPT_ID
        )
    )
