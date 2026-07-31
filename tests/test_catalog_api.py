from __future__ import annotations

import asyncio
import uuid

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_principal, get_runtime_state
from app.api.routes import catalog
from app.auth.security import Principal
from app.catalog.repository import CatalogRepository
from app.catalog.service import CatalogService
from app.state.auth_repository import AuthRepository
from app.state.models import AuditEvent, Base, Tenant, User


class AsyncCallAdapter:
    async def _call(self, method, *args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)


def _principal(tenant, user, role):
    return Principal(
        tenant_id=tenant.id,
        user_id=user.id,
        username=user.username,
        role=role,
        auth_method="test",
        scopes=frozenset({"*"}),
    )


def test_catalog_admin_rbac_tenant_isolation_and_audit():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="catalog-a", name="A")
        other_tenant = Tenant(slug="catalog-b", name="B")
        session.add_all([tenant, other_tenant])
        session.flush()
        admin = User(tenant_id=tenant.id, username="admin", display_name="Admin", role="admin")
        user = User(tenant_id=tenant.id, username="user", display_name="User", role="user")
        outsider = User(
            tenant_id=other_tenant.id,
            username="admin",
            display_name="Other admin",
            role="admin",
        )
        session.add_all([admin, user, outsider])
        session.flush()

    repository = CatalogRepository(factory)
    service = CatalogService(repository)
    runtime = {
        "catalog_repository": repository,
        "catalog_service": service,
        "auth_repository": AuthRepository(factory),
        "auth_service": AsyncCallAdapter(),
    }
    app = FastAPI()

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.correlation_id = uuid.uuid4()
        return await call_next(request)

    app.include_router(catalog.router)
    app.dependency_overrides[get_runtime_state] = lambda: runtime
    selected = {"principal": _principal(tenant, user, "user")}
    app.dependency_overrides[get_principal] = lambda: selected["principal"]
    client = TestClient(app)

    assert client.get("/v1/admin/knowledge-bases").status_code == 403

    selected["principal"] = _principal(tenant, admin, "admin")
    knowledge_base = client.post(
        "/v1/admin/knowledge-bases",
        json={"name": "Engineering", "description": "Internal docs"},
    )
    assert knowledge_base.status_code == 201
    source = client.post(
        "/v1/admin/knowledge-sources",
        json={
            "name": "Docs",
            "config": {
                "source_type": "website",
                "root_url": "https://docs.example.com",
                "allowed_domains": ["example.com"],
            },
        },
    )
    assert source.status_code == 201
    knowledge_base_id = knowledge_base.json()["id"]
    source_id = source.json()["id"]
    assert (
        client.put(f"/v1/admin/knowledge-bases/{knowledge_base_id}/sources/{source_id}").status_code
        == 204
    )

    selected["principal"] = _principal(other_tenant, outsider, "admin")
    assert client.get(f"/v1/admin/knowledge-bases/{knowledge_base_id}").status_code == 404
    assert client.get(f"/v1/admin/knowledge-sources/{source_id}").status_code == 404
    assert client.get("/v1/admin/knowledge-bases").json() == []
    assert client.get("/v1/admin/knowledge-sources").json() == []

    with factory() as session:
        actions = set(session.scalars(select(AuditEvent.action)))
        assert {
            "knowledge_base.create",
            "knowledge_source.create",
            "knowledge_base.source.link",
            "access.denied",
        }.issubset(actions)


def test_catalog_api_activation_uses_application_created_versions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="activation", name="Activation")
        session.add(tenant)
        session.flush()
        admin = User(tenant_id=tenant.id, username="admin", display_name="Admin", role="admin")
        session.add(admin)
        session.flush()
    repository = CatalogRepository(factory)
    service = CatalogService(repository)
    source = repository.create_source(
        tenant.id,
        "Repository",
        "git",
        "1.0",
        {
            "source_type": "git",
            "config_version": "1.0",
            "repository_url": "https://git.example.com/project.git",
            "ref_kind": "branch",
            "ref": "main",
        },
        True,
    )
    version = asyncio.run(
        service.record_fixture_ingestion(
            tenant.id,
            source.id,
            objects=[
                {
                    "object_key": "README.md",
                    "checksum": "a" * 64,
                    "byte_count": 10,
                    "chunk_count": 1,
                }
            ],
        )
    )
    runtime = {
        "catalog_repository": repository,
        "catalog_service": service,
        "auth_repository": AuthRepository(factory),
        "auth_service": AsyncCallAdapter(),
    }
    app = FastAPI()

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.correlation_id = uuid.uuid4()
        return await call_next(request)

    app.include_router(catalog.router)
    app.dependency_overrides[get_runtime_state] = lambda: runtime
    app.dependency_overrides[get_principal] = lambda: _principal(tenant, admin, "admin")
    client = TestClient(app)

    activated = client.post(
        f"/v1/admin/knowledge-sources/{source.id}/versions/{version.id}/activate"
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"
    with factory() as session:
        assert "source_version.activate" in set(session.scalars(select(AuditEvent.action)))
