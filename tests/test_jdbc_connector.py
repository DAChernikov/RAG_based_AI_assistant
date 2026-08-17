from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.catalog.configs import JDBCSourceConfig
from app.connectors.base import (
    CredentialIsolationError,
    CredentialMaterial,
    CredentialResolver,
    EnvironmentCredentialResolver,
    PreviousObject,
    SSRFProtectionError,
    TransientConnectorError,
)
from app.connectors.jdbc import JDBCMetadataConnector, resolve_public_database_host
from app.connectors.jdbc_registry import ManagedDriverError, ManagedDriverRegistry
from app.state.models import Base, JDBCDriverRegistryEntry


class FakeResolver(CredentialResolver):
    def __init__(self, material=None):
        self.material = material or CredentialMaterial(
            database_parameters={"username": "metadata_reader", "password": "private-value"}
        )

    async def resolve(self, _reference: str) -> CredentialMaterial:
        return self.material


class FakeCursor:
    def __init__(self, datasets, statements):
        self.datasets = datasets
        self.statements = statements
        self.current = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if statement == "SET TRANSACTION READ ONLY":
            self.current = []
        elif "pg_catalog.pg_attribute" in statement:
            self.current = self.datasets["columns"]
        elif "pg_catalog.pg_constraint" in statement:
            self.current = self.datasets["constraints"]
        elif "pg_catalog.pg_index" in statement:
            self.current = self.datasets["indexes"]
        else:
            self.current = self.datasets["relations"]

    def fetchall(self):
        return self.current


class FakeConnection:
    def __init__(self, datasets, statements):
        self.datasets = datasets
        self.statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self, **_kwargs):
        return FakeCursor(self.datasets, self.statements)


class FakeConnectionFactory:
    def __init__(self, datasets):
        self.datasets = datasets
        self.parameters = []
        self.statements = []

    def __call__(self, **parameters):
        self.parameters.append(parameters)
        return FakeConnection(self.datasets, self.statements)


def config(**overrides):
    payload = {
        "source_type": "jdbc",
        "driver_id": "postgresql",
        "connection_ref": "connection:neon-demo",
        "jdbc_url": "jdbc:postgresql://db.example.test/demo?sslmode=verify-full",
        "host_allowlist": ["db.example.test"],
        "database_allowlist": ["demo"],
        "catalog_allowlist": ["demo"],
        "schema_allowlist": ["rag_demo_source"],
    }
    payload.update(overrides)
    return JDBCSourceConfig.model_validate(payload)


def metadata(comment="Demo table", include_view=True):
    relations = [
        {
            "schema_name": "rag_demo_source",
            "relation_name": "accounts",
            "relation_kind": "table",
            "comment": comment,
        }
    ]
    if include_view:
        relations.append(
            {
                "schema_name": "rag_demo_source",
                "relation_name": "account_names",
                "relation_kind": "view",
                "comment": None,
            }
        )
    return {
        "relations": relations,
        "columns": [
            {
                "schema_name": "rag_demo_source",
                "relation_name": "accounts",
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "uuid",
                "nullable": False,
                "column_default": "gen_random_uuid()",
                "comment": "Primary identifier",
            }
        ],
        "constraints": [
            {
                "schema_name": "rag_demo_source",
                "relation_name": "accounts",
                "constraint_name": "accounts_pkey",
                "constraint_type": "p",
                "definition": "PRIMARY KEY (id)",
                "referenced_schema": None,
                "referenced_relation": None,
            }
        ],
        "indexes": [
            {
                "schema_name": "rag_demo_source",
                "relation_name": "accounts",
                "index_name": "accounts_pkey",
                "is_unique": True,
                "is_primary": True,
                "definition": "CREATE UNIQUE INDEX accounts_pkey ON accounts USING btree (id)",
                "comment": None,
            }
        ],
    }


async def allow_test_host(_host: str) -> tuple[str, ...]:
    return ("203.0.113.10",)


@pytest.fixture
def registry():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(
            JDBCDriverRegistryEntry(
                driver_id="postgresql",
                registry_version="1",
                dialect="postgresql",
                adapter="psycopg",
                adapter_version="3.2.4",
                manifest_checksum=(
                    "f9a977f43ffd5fa678871b0b563c76613ba9ab70fb3ca6480b138df47e5ed1b1"
                ),
                allowed_properties=["sslmode"],
                is_enabled=True,
            )
        )
    return ManagedDriverRegistry(factory)


@pytest.mark.asyncio
async def test_jdbc_metadata_is_deterministic_read_only_and_incremental(registry):
    factory = FakeConnectionFactory(metadata())
    connector = JDBCMetadataConnector(
        FakeResolver(),
        registry=registry,
        connection_factory=factory,
        host_validator=allow_test_host,
    )

    first = await connector.discover(config())
    assert first.added == (
        "demo.rag_demo_source.account_names",
        "demo.rag_demo_source.accounts",
    )
    assert first.deleted == ()
    assert [item.title for item in first.documents] == [
        "rag_demo_source.account_names",
        "rag_demo_source.accounts",
    ]
    accounts = next(item for item in first.documents if item.title.endswith("accounts"))
    assert [chunk.metadata["section"] for chunk in accounts.chunks] == [
        "relation",
        "columns",
        "constraints",
        "indexes",
    ]
    assert "private-value" not in accounts.text

    previous = {
        item.object_key: PreviousObject(item.object_key, item.checksum, item.metadata)
        for item in first.documents
    }
    second = await connector.discover(config(), previous)
    assert second.documents == ()
    assert second.unchanged == tuple(sorted(previous))
    assert second.source_revision == first.source_revision

    connection = factory.parameters[0]
    assert connection["sslmode"] == "verify-full"
    assert connection["host"] == "db.example.test"
    assert connection["hostaddr"] == "203.0.113.10"
    assert connection["connect_timeout"] == 10
    assert "default_transaction_read_only=on" in connection["options"]
    assert "statement_timeout=15000" in connection["options"]
    assert factory.statements[0] == ("SET TRANSACTION READ ONLY", None)
    for statement, parameters in factory.statements[1:5]:
        assert "pg_catalog" in statement
        assert "rag_demo_source" not in statement
        assert parameters[0] == ["rag_demo_source"]


@pytest.mark.asyncio
async def test_jdbc_incremental_change_and_delete(registry):
    first_connector = JDBCMetadataConnector(
        FakeResolver(),
        registry=registry,
        connection_factory=FakeConnectionFactory(metadata()),
        host_validator=allow_test_host,
    )
    first = await first_connector.discover(config())
    previous = {
        item.object_key: PreviousObject(item.object_key, item.checksum, item.metadata)
        for item in first.documents
    }
    second_connector = JDBCMetadataConnector(
        FakeResolver(),
        registry=registry,
        connection_factory=FakeConnectionFactory(metadata("Changed", include_view=False)),
        host_validator=allow_test_host,
    )
    second = await second_connector.discover(config(), previous)
    assert second.modified == ("demo.rag_demo_source.accounts",)
    assert second.deleted == ("demo.rag_demo_source.account_names",)
    assert len(second.documents) == 1


@pytest.mark.asyncio
async def test_jdbc_rejects_cross_channel_credentials_and_private_network(registry):
    connector = JDBCMetadataConnector(
        FakeResolver(CredentialMaterial(http_headers={"Authorization": "Bearer private"})),
        registry=registry,
        connection_factory=FakeConnectionFactory(metadata()),
        host_validator=allow_test_host,
    )
    with pytest.raises(CredentialIsolationError, match="non-database"):
        await connector.discover(config())

    async def reject_host(_host):
        raise SSRFProtectionError("blocked")

    blocked = JDBCMetadataConnector(FakeResolver(), registry=registry, host_validator=reject_host)
    with pytest.raises(SSRFProtectionError):
        await blocked.discover(config())


@pytest.mark.asyncio
async def test_jdbc_connection_errors_are_sanitized(registry):
    def fail_connection(**_kwargs):
        import psycopg

        raise psycopg.OperationalError("password=private-value")

    connector = JDBCMetadataConnector(
        FakeResolver(),
        registry=registry,
        connection_factory=fail_connection,
        host_validator=allow_test_host,
    )
    with pytest.raises(TransientConnectorError) as error:
        await connector.discover(config())
    assert str(error.value) == "Database metadata introspection failed."
    assert "private-value" not in str(error.value)


def test_managed_driver_registry_rejects_unknown_or_unversioned_drivers(registry):
    assert registry.require("postgresql", "1").adapter == "psycopg"
    with pytest.raises(ManagedDriverError):
        registry.require("postgresql", "2")
    with pytest.raises(ManagedDriverError):
        registry.require("custom-jar", "1")


@pytest.mark.asyncio
async def test_environment_connection_reference_resolves_database_channel(monkeypatch):
    reference = "connection:neon-demo"
    suffix = hashlib.sha256(reference.encode()).hexdigest()[:16].upper()
    variable = f"RAG_CREDENTIAL_{suffix}"
    monkeypatch.setenv(
        variable,
        json.dumps(
            {
                "database_parameters": {
                    "username": "metadata_fixture",
                    "password": "fixture-only-value",
                }
            }
        ),
    )
    material = await EnvironmentCredentialResolver().resolve(reference)
    assert set(material.database_parameters) == {"username", "password"}
    assert material.http_headers == material.git_environment == {}


@pytest.mark.asyncio
async def test_jdbc_dns_resolution_accepts_public_ipv4_ipv6_and_is_stable(monkeypatch, registry):
    calls = 0

    def addresses(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [
            (2, 1, 6, "", ("8.8.8.8", 0)),
            (10, 1, 6, "", ("2606:4700:4700::1111", 0, 0, 0)),
        ]

    monkeypatch.setattr("app.connectors.jdbc.socket.getaddrinfo", addresses)
    resolved = await resolve_public_database_host("db.example.test")
    assert resolved == ("8.8.8.8", "2606:4700:4700::1111")

    factory = FakeConnectionFactory(metadata())
    connector = JDBCMetadataConnector(FakeResolver(), registry=registry, connection_factory=factory)
    await connector.discover(config())
    assert calls == 2  # one direct check and exactly one connector resolution
    assert factory.parameters[0]["hostaddr"] == "8.8.8.8"
    assert factory.parameters[0]["host"] == "db.example.test"


@pytest.mark.asyncio
async def test_jdbc_dns_rebinding_to_private_address_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "app.connectors.jdbc.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 0))],
    )
    with pytest.raises(SSRFProtectionError):
        await resolve_public_database_host("db.example.test")
