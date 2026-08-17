from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.connectors.base import CredentialMaterial
from app.sql.explain import SQLExplainContext
from app.state.models import Base, KnowledgeSource, Tenant


class Resolver:
    async def resolve(self, reference):
        assert reference == "connection:warehouse"
        return CredentialMaterial(
            database_parameters={
                "username": "readonly",
                "password": "secret-value",
                "sslrootcert": "/certs/ca.pem",
            }
        )


@pytest.fixture
def explain_context():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="sql", name="SQL")
        session.add(tenant)
        session.flush()
        source = KnowledgeSource(
            tenant_id=tenant.id,
            name="warehouse",
            source_type="jdbc",
            config_version="1.0",
            config={
                "source_type": "jdbc",
                "driver_id": "postgresql",
                "connection_ref": "connection:warehouse",
                "jdbc_url": "jdbc:postgresql://db.example.test/warehouse?sslmode=verify-full",
                "host_allowlist": ["db.example.test"],
                "database_allowlist": ["warehouse"],
                "catalog_allowlist": ["warehouse"],
                "schema_allowlist": ["public"],
            },
        )
        session.add(source)
        session.flush()
        ids = tenant.id, source.id
    return SQLExplainContext(factory, Resolver()), ids


@pytest.mark.asyncio
async def test_explain_context_pins_verified_ip_and_preserves_tls_hostname(
    explain_context, monkeypatch
):
    context, (tenant_id, source_id) = explain_context

    async def resolved(_host):
        return ("8.8.8.8", "2001:4860:4860::8888")

    monkeypatch.setattr("app.sql.explain.resolve_public_database_host", resolved)
    parameters = await context.parameters(
        tenant_id, [{"source": "jdbc", "source_id": str(source_id)}]
    )
    assert parameters["host"] == "db.example.test"
    assert parameters["hostaddr"] == "8.8.8.8"
    assert parameters["sslmode"] == "verify-full"
    assert parameters["sslrootcert"] == "/certs/ca.pem"


@pytest.mark.asyncio
async def test_explain_context_rejects_ambiguous_or_cross_tenant_sources(explain_context):
    context, (tenant_id, source_id) = explain_context
    with pytest.raises(RuntimeError, match="exactly one"):
        await context.parameters(tenant_id, [])
    with pytest.raises(RuntimeError, match="unavailable"):
        await context.parameters(uuid.uuid4(), [{"source": "jdbc", "source_id": str(source_id)}])
