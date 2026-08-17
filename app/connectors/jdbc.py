from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.catalog.configs import JDBCSourceConfig
from app.catalog.jdbc_urls import ParsedJDBCUrl, parse_jdbc_url
from app.concurrency import connector_blocking_io
from app.connectors.base import (
    ConnectorDocument,
    CredentialIsolationError,
    CredentialResolver,
    DiscoveryResult,
    ParsedChunk,
    PreviousObject,
    SSRFProtectionError,
    TransientConnectorError,
    validate_credential_material,
)
from app.connectors.jdbc_registry import ManagedDriverRegistry
from app.connectors.parsing import checksum_text

_RELATIONS_SQL = """
SELECT n.nspname AS schema_name,
       c.relname AS relation_name,
       CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'table' WHEN 'v' THEN 'view'
            WHEN 'm' THEN 'materialized_view' END AS relation_kind,
       obj_description(c.oid, 'pg_class') AS comment
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%s) AND c.relkind = ANY(%s)
ORDER BY n.nspname, c.relname
"""

_COLUMNS_SQL = """
SELECT n.nspname AS schema_name,
       c.relname AS relation_name,
       a.attname AS column_name,
       a.attnum AS ordinal_position,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
       NOT a.attnotnull AS nullable,
       pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS column_default,
       pg_catalog.col_description(c.oid, a.attnum) AS comment
FROM pg_catalog.pg_attribute AS a
JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_attrdef AS d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE n.nspname = ANY(%s) AND c.relkind = ANY(%s)
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY n.nspname, c.relname, a.attnum
"""

_CONSTRAINTS_SQL = """
SELECT n.nspname AS schema_name,
       c.relname AS relation_name,
       con.conname AS constraint_name,
       con.contype AS constraint_type,
       pg_catalog.pg_get_constraintdef(con.oid, true) AS definition,
       rn.nspname AS referenced_schema,
       rc.relname AS referenced_relation
FROM pg_catalog.pg_constraint AS con
JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_class AS rc ON rc.oid = con.confrelid
LEFT JOIN pg_catalog.pg_namespace AS rn ON rn.oid = rc.relnamespace
WHERE n.nspname = ANY(%s) AND c.relkind = ANY(%s) AND con.contype = ANY(%s)
ORDER BY n.nspname, c.relname, con.conname
"""

_INDEXES_SQL = """
SELECT n.nspname AS schema_name,
       c.relname AS relation_name,
       i.relname AS index_name,
       ix.indisunique AS is_unique,
       ix.indisprimary AS is_primary,
       pg_catalog.pg_get_indexdef(i.oid) AS definition,
       obj_description(i.oid, 'pg_class') AS comment
FROM pg_catalog.pg_index AS ix
JOIN pg_catalog.pg_class AS c ON c.oid = ix.indrelid
JOIN pg_catalog.pg_class AS i ON i.oid = ix.indexrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%s) AND c.relkind = ANY(%s)
ORDER BY n.nspname, c.relname, i.relname
"""

_CONSTRAINT_TYPES = {"p": "primary_key", "f": "foreign_key", "u": "unique"}


async def resolve_public_database_host(host: str) -> tuple[str, ...]:
    rows = await connector_blocking_io.call(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    if not rows:
        raise SSRFProtectionError("Database host did not resolve.")
    addresses = []
    for row in rows:
        address = ipaddress.ip_address(str(row[4][0]).split("%", 1)[0])
        if not address.is_global:
            raise SSRFProtectionError(
                "Private, link-local and metadata database addresses are blocked."
            )
        addresses.append(address.compressed)
    return tuple(sorted(set(addresses), key=lambda value: (":" in value, value)))


class JDBCMetadataConnector:
    connector_version = "jdbc-postgresql-metadata/1.0"

    def __init__(
        self,
        credential_resolver: CredentialResolver,
        *,
        registry: ManagedDriverRegistry,
        connection_factory: Callable[..., Any] = psycopg.connect,
        host_validator: Callable[[str], Awaitable[tuple[str, ...]]] = resolve_public_database_host,
    ):
        self.credential_resolver = credential_resolver
        self.registry = registry
        self.connection_factory = connection_factory
        self.host_validator = host_validator

    async def discover(
        self,
        config: JDBCSourceConfig,
        previous: dict[str, PreviousObject] | None = None,
    ) -> DiscoveryResult:
        previous = previous or {}
        driver = self.registry.require(config.driver_id, config.driver_registry_version)
        parsed = parse_jdbc_url(config.jdbc_url)
        if parsed.dialect != driver.dialect or parsed.host is None or parsed.database is None:
            raise CredentialIsolationError("JDBC target does not match the managed driver.")
        if parsed.host not in config.host_allowlist:
            raise CredentialIsolationError("Database host is outside the configured allowlist.")
        if parsed.database not in config.database_allowlist:
            raise CredentialIsolationError("Database is outside the configured allowlist.")
        if parsed.database not in config.catalog_allowlist:
            raise CredentialIsolationError("Database catalog is outside the configured allowlist.")
        if set(key.casefold() for key in parsed.properties) - driver.allowed_properties:
            raise CredentialIsolationError("JDBC connection property is not allowed.")
        if parsed.properties.get("sslmode", "").casefold() != "verify-full":
            raise CredentialIsolationError("TLS certificate and hostname verification is required.")
        verified_addresses = await self.host_validator(parsed.host)
        if not verified_addresses:
            raise SSRFProtectionError("Database host did not resolve to a verified public IP.")

        credentials = validate_credential_material(
            await self.credential_resolver.resolve(config.connection_ref)
        )
        if credentials.http_headers or credentials.git_environment:
            raise CredentialIsolationError(
                "JDBC credential reference contains non-database credential material."
            )
        username = credentials.database_parameters.get("username")
        password = credentials.database_parameters.get("password")
        if not username or not password:
            raise CredentialIsolationError(
                "PostgreSQL metadata credentials require username and password."
            )

        connection_parameters: dict[str, Any] = {
            "host": parsed.host,
            "hostaddr": verified_addresses[0],
            "port": parsed.port or 5432,
            "dbname": parsed.database,
            "user": username,
            "password": password,
            "sslmode": "verify-full",
            "connect_timeout": config.connect_timeout_sec,
            "application_name": "rag-metadata-connector",
            "options": (
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={config.statement_timeout_ms}"
            ),
        }
        for name in ("sslrootcert", "sslcert", "sslkey"):
            if value := credentials.database_parameters.get(name):
                connection_parameters[name] = value
        try:
            relations = await connector_blocking_io.call(
                self._read_metadata, config, connection_parameters
            )
        except (psycopg.Error, OSError, TimeoutError) as exc:
            raise TransientConnectorError("Database metadata introspection failed.") from exc

        documents = tuple(
            self._document(parsed, driver.adapter_version, relation) for relation in relations
        )
        by_key = {document.object_key: document for document in documents}
        added = set(by_key) - set(previous)
        deleted = set(previous) - set(by_key)
        unchanged = {
            key
            for key, document in by_key.items()
            if key in previous and previous[key].checksum == document.checksum
        }
        modified = set(by_key) - added - unchanged
        materialized = added | modified
        source_revision = hashlib.sha256(
            json.dumps(
                [(key, by_key[key].checksum) for key in sorted(by_key)],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return DiscoveryResult(
            documents=tuple(
                document for document in documents if document.object_key in materialized
            ),
            added=tuple(sorted(added)),
            modified=tuple(sorted(modified)),
            unchanged=tuple(sorted(unchanged)),
            deleted=tuple(sorted(deleted)),
            source_revision=source_revision,
        )

    def _read_metadata(
        self, config: JDBCSourceConfig, connection_parameters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        relation_kinds: list[str] = []
        if config.metadata_policy.include_tables:
            relation_kinds.extend(("r", "p"))
        if config.metadata_policy.include_views:
            relation_kinds.extend(("v", "m"))
        if not relation_kinds:
            return []
        parameters = (list(config.schema_allowlist), relation_kinds)
        try:
            with self.connection_factory(**connection_parameters) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    relations = self._fetch(cursor, _RELATIONS_SQL, parameters)
                    columns = (
                        self._fetch(cursor, _COLUMNS_SQL, parameters)
                        if config.metadata_policy.include_columns
                        else []
                    )
                    constraints = (
                        self._fetch(
                            cursor,
                            _CONSTRAINTS_SQL,
                            (*parameters, list(_CONSTRAINT_TYPES)),
                        )
                        if config.metadata_policy.include_constraints
                        else []
                    )
                    indexes = (
                        self._fetch(cursor, _INDEXES_SQL, parameters)
                        if config.metadata_policy.include_indexes
                        else []
                    )
        except (psycopg.Error, OSError, TimeoutError):
            raise

        by_relation: dict[tuple[str, str], dict[str, Any]] = {}
        for row in relations:
            item = self._clean_row(row)
            if not config.metadata_policy.include_comments:
                item["comment"] = None
            item.update(columns=[], constraints=[], indexes=[])
            by_relation[(item["schema_name"], item["relation_name"])] = item
        for row in columns:
            item = self._clean_row(row)
            if not config.metadata_policy.include_comments:
                item["comment"] = None
            key = (item.pop("schema_name"), item.pop("relation_name"))
            if key in by_relation:
                by_relation[key]["columns"].append(item)
        for row in constraints:
            item = self._clean_row(row)
            key = (item.pop("schema_name"), item.pop("relation_name"))
            item["constraint_type"] = _CONSTRAINT_TYPES[item["constraint_type"]]
            if key in by_relation:
                by_relation[key]["constraints"].append(item)
        for row in indexes:
            item = self._clean_row(row)
            if not config.metadata_policy.include_comments:
                item["comment"] = None
            key = (item.pop("schema_name"), item.pop("relation_name"))
            if key in by_relation:
                by_relation[key]["indexes"].append(item)
        return [by_relation[key] for key in sorted(by_relation)]

    @staticmethod
    def _fetch(cursor, statement: str, parameters: Sequence[Any]) -> list[Mapping[str, Any]]:
        cursor.execute(statement, parameters)
        return list(cursor.fetchall())

    @staticmethod
    def _clean_row(row: Mapping[str, Any]) -> dict[str, Any]:
        return {key: row[key] for key in sorted(row)}

    @staticmethod
    def _document(
        parsed: ParsedJDBCUrl, adapter_version: str, relation: dict[str, Any]
    ) -> ConnectorDocument:
        schema = relation["schema_name"]
        name = relation["relation_name"]
        object_key = f"{parsed.database}.{schema}.{name}"
        canonical_uri = (
            f"jdbc:postgresql://{parsed.host}:{parsed.port or 5432}/"
            f"{parsed.database}/{schema}/{name}"
        )
        text = json.dumps(relation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        chunks: list[ParsedChunk] = []
        overview = {
            key: relation[key]
            for key in ("schema_name", "relation_name", "relation_kind", "comment")
        }
        for section, content in (
            ("relation", overview),
            ("columns", relation["columns"]),
            ("constraints", relation["constraints"]),
            ("indexes", relation["indexes"]),
        ):
            if not content and section != "relation":
                continue
            chunk_text = json.dumps(
                content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            chunks.append(
                ParsedChunk(
                    text=chunk_text,
                    checksum=checksum_text(chunk_text),
                    chunk_index=len(chunks),
                    metadata={
                        "section": section,
                        "database": parsed.database,
                        "schema": schema,
                        "relation": name,
                    },
                )
            )
        return ConnectorDocument(
            object_key=object_key,
            canonical_uri=canonical_uri,
            title=f"{schema}.{name}",
            text=text,
            checksum=checksum_text(text),
            byte_count=len(text.encode("utf-8")),
            metadata={
                "dialect": "postgresql",
                "adapter_version": adapter_version,
                "database": parsed.database,
                "schema": schema,
                "relation": name,
                "relation_kind": relation["relation_kind"],
            },
            chunks=tuple(chunks),
        )
