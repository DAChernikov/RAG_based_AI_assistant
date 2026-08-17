from __future__ import annotations

import asyncio
import uuid

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_principal, get_runtime_state
from app.api.routes import users
from app.auth.security import PasswordManager, Principal
from app.state.auth_repository import AuthRepository
from app.state.models import Base, Tenant, User


class Service:
    passwords = PasswordManager()

    async def _call(self, function, *args, **kwargs):
        return await asyncio.to_thread(function, *args, **kwargs)


def client_context():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="users", name="Users")
        session.add(tenant)
        session.flush()
        admin = User(
            tenant_id=tenant.id,
            username="admin",
            display_name="Admin",
            role="admin",
        )
        session.add(admin)
        session.flush()
        ids = tenant.id, admin.id
    runtime = {"auth_repository": AuthRepository(factory), "auth_service": Service()}
    app = FastAPI()

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.correlation_id = uuid.uuid4()
        return await call_next(request)

    app.include_router(users.router)
    app.dependency_overrides[get_runtime_state] = lambda: runtime
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id=ids[0],
        user_id=ids[1],
        username="admin",
        role="admin",
        auth_method="test",
        scopes=frozenset({"*"}),
    )
    return TestClient(app), ids


def test_admin_user_crud_validates_roles_passwords_and_self_protection():
    client, (_, admin_id) = client_context()
    created = client.post(
        "/v1/admin/users",
        json={
            "username": "analyst",
            "display_name": "Analyst",
            "password": "long-secure-password",
            "role": "user",
        },
    )
    assert created.status_code == 201
    user_id = created.json()["id"]
    assert client.get("/v1/admin/users").json()[1]["username"] == "analyst"
    assert client.get(f"/v1/admin/users/{user_id}").status_code == 200
    updated = client.patch(
        f"/v1/admin/users/{user_id}",
        json={"display_name": "Senior Analyst", "role": "admin", "is_active": True},
    )
    assert updated.json()["role"] == "admin"
    assert client.delete(f"/v1/admin/users/{user_id}").status_code == 204
    assert client.delete(f"/v1/admin/users/{admin_id}").status_code == 409
    assert client.get(f"/v1/admin/users/{uuid.uuid4()}").status_code == 404
