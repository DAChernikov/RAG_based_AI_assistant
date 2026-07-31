from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration


def test_catalog_migration_upgrade_downgrade_upgrade():
    base_url = os.getenv("INTEGRATION_DATABASE_URL")
    if not base_url:
        pytest.skip("Integration PostgreSQL URL is not configured.")
    parsed = make_url(base_url)
    database_name = f"rag_migration_{uuid.uuid4().hex[:16]}"
    admin_url = parsed.set(database="postgres")
    test_url = parsed.set(database=database_name)
    psycopg_admin_url = admin_url.set(drivername="postgresql")

    with psycopg.connect(
        psycopg_admin_url.render_as_string(hide_password=False), autocommit=True
    ) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    try:
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = test_url.render_as_string(hide_password=False)
        command.upgrade(config, "head")
        engine = create_engine(test_url)
        assert {
            "knowledge_bases",
            "knowledge_sources",
            "knowledge_base_sources",
            "source_versions",
            "source_objects",
            "ingestion_runs",
            "content_blobs",
            "normalized_documents",
            "document_chunks",
        }.issubset(inspect(engine).get_table_names())
        engine.dispose()

        command.downgrade(config, "20260731_02")
        engine = create_engine(test_url)
        assert "knowledge_sources" not in inspect(engine).get_table_names()
        engine.dispose()

        command.upgrade(config, "head")
        engine = create_engine(test_url)
        assert "source_versions" in inspect(engine).get_table_names()
        engine.dispose()
    finally:
        with psycopg.connect(
            psycopg_admin_url.render_as_string(hide_password=False), autocommit=True
        ) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )
