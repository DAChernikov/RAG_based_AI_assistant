import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.config import settings
from app.api.dependencies import get_runtime_state
from app.api.routes import api_keys, auth, users
from app.auth.security import AuthenticationError, PasswordManager, TokenReuseError
from app.auth.service import AuthService
from app.state.auth_repository import AuthRepository
from app.state.models import APIKey, AuditEvent, Base, RefreshSession, Tenant, User


class FakeRateLimiter:
    def __init__(self, attempts=2):
        self.attempts = attempts
        self.counts = {}

    async def check(self, key):
        count = self.counts.get(key, 0) + 1
        self.counts[key] = count
        if count > self.attempts:
            raise AuthenticationError("Too many login attempts. Try again later.")

    async def reset(self, key):
        self.counts.pop(key, None)


@pytest.fixture
def auth_context(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "test-only-secret-with-at-least-32-characters")
    monkeypatch.setattr(settings, "access_token_ttl_sec", 60)
    monkeypatch.setattr(settings, "refresh_token_ttl_sec", 3600)
    monkeypatch.setattr(settings, "login_rate_limit_attempts", 2)
    monkeypatch.setattr(settings, "login_rate_limit_window_sec", 60)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    passwords = PasswordManager()
    with factory.begin() as session:
        tenant = Tenant(slug="tenant-a", name="Tenant A")
        other_tenant = Tenant(slug="tenant-b", name="Tenant B")
        session.add_all([tenant, other_tenant])
        session.flush()
        admin = User(
            tenant_id=tenant.id,
            username="admin",
            display_name="Admin",
            password_hash=passwords.hash("admin-password-123"),
            role="admin",
        )
        user = User(
            tenant_id=tenant.id,
            username="alice",
            display_name="Alice",
            password_hash=passwords.hash("alice-password-123"),
            role="user",
        )
        outsider = User(
            tenant_id=other_tenant.id,
            username="alice",
            display_name="Other Alice",
            password_hash=passwords.hash("other-password-123"),
            role="admin",
        )
        session.add_all([admin, user, outsider])
        session.flush()
    repository = AuthRepository(factory)
    service = AuthService(repository, FakeRateLimiter())
    return service, repository, factory, admin, user, outsider


@pytest.mark.asyncio
async def test_login_refresh_rotation_logout_and_reuse_detection(auth_context):
    service, _, _, _, user, _ = auth_context
    correlation_id = uuid.uuid4()

    tokens = await service.login("tenant-a", "alice", "alice-password-123", correlation_id)
    principal = await service.authenticate_access_token(tokens["access_token"])
    assert principal.user_id == user.id

    rotated = await service.refresh(tokens["refresh_token"], correlation_id)
    assert rotated["refresh_token"] != tokens["refresh_token"]
    with pytest.raises(TokenReuseError):
        await service.refresh(tokens["refresh_token"], correlation_id)
    with pytest.raises(TokenReuseError):
        await service.refresh(rotated["refresh_token"], correlation_id)

    fresh = await service.login("tenant-a", "alice", "alice-password-123", correlation_id)
    await service.logout(fresh["refresh_token"], correlation_id)
    with pytest.raises(TokenReuseError):
        await service.refresh(fresh["refresh_token"], correlation_id)


@pytest.mark.asyncio
async def test_wrong_password_rate_limit_and_expired_refresh(auth_context):
    service, repository, _, _, user, _ = auth_context
    correlation_id = uuid.uuid4()
    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        await service.login("tenant-a", "alice", "wrong-password", correlation_id)
    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        await service.login("tenant-a", "alice", "wrong-password", correlation_id)
    with pytest.raises(AuthenticationError, match="Too many"):
        await service.login("tenant-a", "alice", "wrong-password", correlation_id)

    expired = "rrt_expired-test-token-value"
    repository.create_refresh_session(
        user.id,
        uuid.uuid4(),
        service_hash(expired),
        datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(TokenReuseError):
        await service.refresh(expired, correlation_id)


def service_hash(value: str) -> str:
    from app.auth.security import secret_hash

    return secret_hash(value)


@pytest.mark.asyncio
async def test_api_key_scope_revocation_and_no_plaintext_storage(auth_context):
    service, repository, factory, _, user, _ = auth_context
    principal = service.principal_for_user(user)
    item, value = await service.create_api_key(
        principal,
        "telegram",
        ["inference:read", "inference:write"],
        datetime.now(UTC) + timedelta(days=1),
    )
    api_principal = await service.authenticate_api_key(value)
    assert api_principal.user_id == user.id
    assert api_principal.has_scope("inference:write")
    assert not api_principal.has_scope("profile:read")
    repository.revoke_api_key(principal, item.id)
    with pytest.raises(AuthenticationError):
        await service.authenticate_api_key(value)

    with factory() as session:
        stored_key = session.scalar(select(APIKey).where(APIKey.id == item.id))
        stored_user = session.get(User, user.id)
        assert stored_key.key_hash != value
        assert len(stored_key.key_hash) == 64
        assert stored_user.password_hash != "alice-password-123"
        assert stored_user.password_hash.startswith("$argon2id$")


def build_auth_client(auth_context):
    service, repository, _, _, _, _ = auth_context
    runtime = {"auth_service": service, "auth_repository": repository}
    app = FastAPI()

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.correlation_id = uuid.uuid4()
        return await call_next(request)

    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(api_keys.router)
    app.dependency_overrides[get_runtime_state] = lambda: runtime
    return TestClient(app), runtime


def test_auth_endpoints_admin_permissions_and_api_key(auth_context):
    client, _ = build_auth_client(auth_context)
    assert client.get("/v1/auth/me").status_code == 401
    user_login = client.post(
        "/v1/auth/login",
        json={
            "tenant_slug": "tenant-a",
            "username": "alice",
            "password": "alice-password-123",
        },
    )
    assert user_login.status_code == 200
    user_header = {"Authorization": f"Bearer {user_login.json()['access_token']}"}
    assert client.get("/v1/auth/me", headers=user_header).status_code == 200
    assert client.get("/v1/admin/users", headers=user_header).status_code == 403

    admin_login = client.post(
        "/v1/auth/login",
        json={
            "tenant_slug": "tenant-a",
            "username": "admin",
            "password": "admin-password-123",
        },
    )
    admin_header = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    users_response = client.get("/v1/admin/users", headers=admin_header)
    assert users_response.status_code == 200
    assert {item["username"] for item in users_response.json()} == {"admin", "alice"}

    created = client.post(
        "/v1/api-keys",
        headers=user_header,
        json={"name": "telegram", "scopes": ["profile:read", "inference:write"]},
    )
    assert created.status_code == 201
    key_value = created.json()["api_key"]
    listed = client.get("/v1/api-keys", headers=user_header)
    assert listed.status_code == 200
    assert "api_key" not in listed.json()[0]
    me = client.get("/v1/auth/me", headers={"X-API-Key": key_value})
    assert me.status_code == 200
    assert me.json()["auth_method"] == "api_key"
    assert client.get("/v1/api-keys", headers={"X-API-Key": key_value}).status_code == 403


def test_audit_metadata_excludes_secrets(auth_context):
    _, repository, factory, _, user, _ = auth_context
    repository.audit(
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="test",
        outcome="success",
        correlation_id=uuid.uuid4(),
        metadata={
            "password": "never-store",
            "token": "never-store",
            "question": "never-store",
            "safe": "value",
        },
    )
    with factory() as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.action == "test"))
        assert event.metadata_json == {"safe": "value"}
        assert session.scalar(select(RefreshSession).limit(1)) is None
