from __future__ import annotations

import uuid

from sqlalchemy import select

from app.catalog.configs import JDBCSourceConfig, parse_source_config
from app.catalog.jdbc_urls import parse_jdbc_url
from app.concurrency import api_blocking_io
from app.connectors.base import EnvironmentCredentialResolver, validate_credential_material
from app.connectors.jdbc import resolve_public_database_host
from app.state.models import KnowledgeSource


class SQLExplainContext:
    def __init__(self, session_factory, resolver=None):
        self.session_factory = session_factory
        self.resolver = resolver or EnvironmentCredentialResolver()

    async def parameters(self, tenant_id: uuid.UUID, retrieved: list[dict]) -> dict:
        source_ids = {
            uuid.UUID(row["source_id"])
            for row in retrieved
            if row.get("source") == "jdbc" and row.get("source_id")
        }
        if len(source_ids) != 1:
            raise RuntimeError("Safe EXPLAIN requires exactly one JDBC schema source.")

        def load():
            with self.session_factory() as session:
                return session.scalar(
                    select(KnowledgeSource).where(
                        KnowledgeSource.id == next(iter(source_ids)),
                        KnowledgeSource.tenant_id == tenant_id,
                        KnowledgeSource.source_type == "jdbc",
                        KnowledgeSource.is_enabled.is_(True),
                    )
                )

        source = await api_blocking_io.call(load)
        if source is None:
            raise RuntimeError("JDBC schema source is unavailable.")
        config = parse_source_config(source.config)
        if not isinstance(config, JDBCSourceConfig):
            raise RuntimeError("JDBC schema source is invalid.")
        parsed = parse_jdbc_url(config.jdbc_url)
        if parsed.host is None:
            raise RuntimeError("JDBC schema host is invalid.")
        addresses = await resolve_public_database_host(parsed.host)
        credentials = validate_credential_material(
            await self.resolver.resolve(config.connection_ref)
        )
        username = credentials.database_parameters.get("username")
        password = credentials.database_parameters.get("password")
        if not username or not password:
            raise RuntimeError("JDBC read-only credentials are unavailable.")
        result = {
            "host": parsed.host,
            "hostaddr": addresses[0],
            "port": parsed.port or 5432,
            "dbname": parsed.database,
            "user": username,
            "password": password,
            "sslmode": "verify-full",
            "connect_timeout": config.connect_timeout_sec,
            "application_name": "rag-safe-explain",
        }
        for name in ("sslrootcert", "sslcert", "sslkey"):
            if value := credentials.database_parameters.get(name):
                result[name] = value
        return result
