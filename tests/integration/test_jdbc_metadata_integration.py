from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from app.catalog.configs import JDBCSourceConfig
from app.connectors.base import CredentialMaterial, CredentialResolver, PreviousObject
from app.connectors.jdbc import JDBCMetadataConnector

pytestmark = pytest.mark.integration


class IntegrationCredentialResolver(CredentialResolver):
    async def resolve(self, _reference: str) -> CredentialMaterial:
        return CredentialMaterial(
            database_parameters={"username": "unused-by-test-factory", "password": "not-stored"}
        )


async def allow_local_integration_host(_host: str) -> None:
    return None


@pytest.mark.asyncio
async def test_postgresql_metadata_connector_reads_catalogs_without_user_rows():
    database_url = os.getenv("INTEGRATION_DATABASE_URL")
    if not database_url:
        pytest.skip("Integration PostgreSQL URL is not configured.")
    parsed_url = make_url(database_url).set(drivername="postgresql")
    psycopg_url = parsed_url.render_as_string(hide_password=False)
    schema = f"jdbc_test_{uuid.uuid4().hex[:12]}"

    with psycopg.connect(psycopg_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.parents (id bigint PRIMARY KEY, name text UNIQUE NOT NULL)"
            ).format(sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.children (id bigint PRIMARY KEY, parent_id bigint NOT NULL "
                "REFERENCES {}.parents(id), created_at timestamptz DEFAULT now())"
            ).format(sql.Identifier(schema), sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL("CREATE INDEX children_parent_idx ON {}.children(parent_id)").format(
                sql.Identifier(schema)
            )
        )
        connection.execute(
            sql.SQL("COMMENT ON TABLE {}.children IS 'Child metadata'").format(
                sql.Identifier(schema)
            )
        )
        connection.execute(
            sql.SQL("COMMENT ON COLUMN {}.children.parent_id IS 'Parent link'").format(
                sql.Identifier(schema)
            )
        )
        connection.execute(
            sql.SQL("CREATE VIEW {}.child_ids AS SELECT id FROM {}.children").format(
                sql.Identifier(schema), sql.Identifier(schema)
            )
        )

    def local_connection_factory(**_secure_parameters):
        return psycopg.connect(psycopg_url)

    config = JDBCSourceConfig.model_validate(
        {
            "source_type": "jdbc",
            "driver_id": "postgresql",
            "connection_ref": "connection:local-integration",
            "jdbc_url": (
                f"jdbc:postgresql://{parsed_url.host}:{parsed_url.port or 5432}/"
                f"{parsed_url.database}?sslmode=verify-full"
            ),
            "host_allowlist": [parsed_url.host],
            "database_allowlist": [parsed_url.database],
            "catalog_allowlist": [parsed_url.database],
            "schema_allowlist": [schema],
        }
    )
    connector = JDBCMetadataConnector(
        IntegrationCredentialResolver(),
        connection_factory=local_connection_factory,
        host_validator=allow_local_integration_host,
    )
    try:
        first = await connector.discover(config)
        assert len(first.documents) == 3
        child = next(item for item in first.documents if item.title == f"{schema}.children")
        assert "Child metadata" in child.text
        assert "Parent link" in child.text
        assert "foreign_key" in child.text
        assert "children_parent_idx" in child.text
        assert "created_at" in child.text
        previous = {
            item.object_key: PreviousObject(item.object_key, item.checksum, item.metadata)
            for item in first.documents
        }
        second = await connector.discover(config, previous)
        assert second.documents == ()
        assert len(second.unchanged) == 3
    finally:
        with psycopg.connect(psycopg_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
