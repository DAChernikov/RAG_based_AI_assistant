from __future__ import annotations

import concurrent.futures
import uuid
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.config import settings
from app.api.dependencies import get_runtime_state
from app.api.routes import auth, setup
from app.auth.service import AuthService
from app.catalog.service import CatalogService
from app.secrets.store import EncryptedDatabaseSecretStore, SecretStoreError
from app.setup.repository import SetupClosedError, SetupRepository
from app.state.auth_repository import AuthRepository
from app.state.models import Base, CredentialSecret, Tenant, User


class RateLimiter:
    async def check(self, _key):
        return None

    async def reset(self, _key):
        return None


def database():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_setup_repository_is_atomic_and_permanently_closes():
    factory = database()
    repository = SetupRepository(factory)

    def attempt(number: int):
        try:
            return repository.bootstrap(
                tenant_slug=f"tenant-{number}",
                tenant_name=f"Tenant {number}",
                username="admin",
                display_name="Administrator",
                password_hash="$argon2id$test",
            )
        except SetupClosedError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (1, 2)))
    assert sum(item is not None for item in results) == 1
    with factory() as session:
        assert session.scalar(select(func.count(Tenant.id))) == 1
        assert session.scalar(select(func.count(User.id)).where(User.role == "admin")) == 1
    assert repository.status()["required"] is False
    try:
        attempt(3)
    except SetupClosedError:
        pass
    assert sum(item is not None for item in results) == 1


def test_setup_http_flow_creates_session_and_rejects_repeat(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "setup_bootstrap_token", None)
    monkeypatch.setattr(settings, "setup_bootstrap_token_file", None)
    monkeypatch.setattr(settings, "jwt_secret", "test-only-secret-with-at-least-32-characters")
    factory = database()
    auth_repository = AuthRepository(factory)
    service = AuthService(auth_repository, RateLimiter())
    runtime = {
        "catalog_service": CatalogService(SimpleNamespace()),
        "setup_repository": SetupRepository(factory),
        "auth_repository": auth_repository,
        "auth_service": service,
    }
    app = FastAPI()

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.correlation_id = uuid.uuid4()
        return await call_next(request)

    app.include_router(setup.router)
    app.include_router(auth.router)
    app.dependency_overrides[get_runtime_state] = lambda: runtime
    client = TestClient(app)
    status = client.get("/v1/setup/status").json()
    assert status == {
        "required": True,
        "current_step": "administrator",
        "onboarding_complete": False,
        "config_version": 1,
        "setup_available": False,
        "token_required": True,
    }
    payload = {
        "tenant_slug": "local",
        "tenant_name": "Local workspace",
        "username": "admin",
        "display_name": "Local administrator",
        "password": "strong-local-password",
    }
    assert client.post("/v1/setup/bootstrap", json=payload).status_code == 404
    token = "production-setup-token-with-at-least-32-characters"
    monkeypatch.setattr(settings, "setup_bootstrap_token", token)
    assert client.post("/v1/setup/bootstrap", json=payload).status_code == 403
    created = client.post("/v1/setup/bootstrap", json=payload, headers={"X-Setup-Token": token})
    assert created.status_code == 201
    assert created.json()["access_token"]
    assert "rag_refresh=" in created.headers["set-cookie"]
    assert (
        client.post(
            "/v1/setup/bootstrap", json=payload, headers={"X-Setup-Token": token}
        ).status_code
        == 409
    )
    assert client.get("/v1/setup/status").json()["required"] is False


def test_production_setup_token_can_be_read_from_secret_file(monkeypatch, tmp_path):
    token = "file-backed-production-setup-token-32-characters"
    token_file = tmp_path / "setup-token"
    token_file.write_text(f"{token}\n")
    monkeypatch.setattr(settings, "setup_bootstrap_token", None)
    monkeypatch.setattr(settings, "setup_bootstrap_token_file", str(token_file))
    assert setup._setup_token() == token
    token_file.unlink()
    assert setup._setup_token() is None


def test_encrypted_secret_store_masks_rotates_and_is_tenant_bound():
    factory = database()
    with factory.begin() as session:
        tenant = Tenant(slug="secret-a", name="Secret A")
        other = Tenant(slug="secret-b", name="Secret B")
        session.add_all([tenant, other])
        session.flush()
    store = EncryptedDatabaseSecretStore(factory, Fernet.generate_key())
    row = store.put(tenant.id, "credential:telegram", "telegram_token", {"value": "first"})
    assert row.masked_value == "••••••••"
    assert store.resolve(tenant.id, "credential:telegram") == {"value": "first"}
    rotated = store.put(tenant.id, "credential:telegram", "telegram_token", {"value": "second"})
    assert rotated.key_version == 2
    with factory() as session:
        stored = session.scalar(select(CredentialSecret))
        assert b"first" not in stored.ciphertext and b"second" not in stored.ciphertext
    try:
        store.resolve(other.id, "credential:telegram")
    except SecretStoreError:
        pass
    else:
        raise AssertionError("Cross-tenant credential lookup must fail.")
    assert store.delete(tenant.id, "credential:telegram") is True
    assert store.delete(tenant.id, "credential:telegram") is False
