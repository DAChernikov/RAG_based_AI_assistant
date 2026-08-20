from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_principal, get_runtime_state
from app.api.routes import platform, secrets, setup
from app.auth.security import Principal
from app.catalog.service import CatalogService
from app.operations.repository import OperationsRepository
from app.secrets.store import EncryptedDatabaseSecretStore
from app.setup.repository import SetupRepository
from app.state.auth_repository import AuthRepository
from app.state.models import Base, SystemSetup, Tenant, User


class Redis:
    async def xlen(self, stream):
        return {"rag:inference:dlq": 2}.get(stream, 0)

    async def exists(self, _key):
        return 1


class Queue:
    redis = Redis()

    async def ping(self):
        return True

    async def latest_heartbeat(self):
        return {"model_ready": True, "retriever_ready": True}


class Embedding:
    def __init__(self, status="loading"):
        self.status = status

    async def readiness(self):
        return {"status": self.status, "model_ready": self.status == "ready"}


class GenerationClient:
    async def readiness(self):
        return {"ready": True, "status": "available"}

    async def aclose(self):
        return None


class Registry:
    def model(self, tenant_id, role):
        return SimpleNamespace(tenant_id=tenant_id, role=role)

    def generation_client(self, _model):
        return GenerationClient()


def context():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="platform", name="Platform")
        session.add(tenant)
        session.flush()
        admin = User(
            tenant_id=tenant.id,
            username="admin",
            display_name="Administrator",
            role="admin",
        )
        session.add(admin)
        session.flush()
    store = EncryptedDatabaseSecretStore(factory, Fernet.generate_key())
    operations = OperationsRepository(factory)
    runtime = {
        "catalog_service": CatalogService(SimpleNamespace()),
        "operations_repository": operations,
        "auth_repository": AuthRepository(factory),
        "auth_service": None,
        "secret_store": store,
        "setup_repository": SetupRepository(factory),
        "database_engine": engine,
        "queue": Queue(),
        "embedding_client": Embedding(),
        "runtime_registry": Registry(),
        "startup_error": None,
    }
    principal = Principal(
        tenant_id=tenant.id,
        user_id=admin.id,
        username="admin",
        role="admin",
        auth_method="test",
        scopes=frozenset({"*"}),
    )
    app = FastAPI()

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.correlation_id = uuid.uuid4()
        return await call_next(request)

    app.include_router(secrets.router)
    app.include_router(platform.router)
    app.include_router(setup.router)
    app.dependency_overrides[get_runtime_state] = lambda: runtime
    app.dependency_overrides[get_principal] = lambda: principal
    return TestClient(app), runtime, principal


def test_typed_credentials_are_masked_rotated_deleted_and_audited():
    client, _, _ = context()
    reference = "credential:model-test"
    created = client.put(
        f"/v1/admin/credentials/{reference}",
        json={"reference": reference, "kind": "model_token", "secret": "secret-one"},
    )
    assert created.status_code == 200
    assert created.json()["masked_value"] == "••••••••"
    assert "secret-one" not in created.text
    assert client.get("/v1/admin/credentials").json()[0]["key_version"] == 1
    rotated = client.put(
        f"/v1/admin/credentials/{reference}",
        json={"reference": reference, "kind": "model_token", "secret": "secret-two"},
    )
    assert rotated.json()["key_version"] == 2
    assert (
        client.put(
            f"/v1/admin/credentials/{reference}",
            json={"reference": "credential:other", "kind": "model_token", "secret": "x"},
        ).status_code
        == 422
    )
    assert client.delete(f"/v1/admin/credentials/{reference}").status_code == 204
    assert client.delete(f"/v1/admin/credentials/{reference}").status_code == 404


def test_system_diagnostics_settings_and_cold_embedding_state():
    client, runtime, _ = context()
    system = client.get("/v1/admin/system")
    assert system.status_code == 200
    assert system.json()["components"]["embedding"] == "model_loading"
    assert system.json()["components"]["generation"] == "ready"
    diagnostics = client.get("/v1/admin/diagnostics").json()
    assert diagnostics["dlq"] == {"inference": 2, "ingestion": 0, "indexing": 0}
    configured = client.get("/v1/admin/settings").json()
    assert "model and prompt activation" in configured["dynamic"]
    assert "database and Redis endpoints" in configured["restart_required"]
    runtime["embedding_client"] = Embedding("ready")
    assert client.get("/v1/admin/system").json()["components"]["embedding"] == "ready"


def test_telegram_configuration_uses_references_and_supports_hot_reload():
    client, runtime, _ = context()
    token_ref = "credential:telegram-token"
    key_ref = "credential:telegram-key"
    for reference, kind, value in (
        (token_ref, "telegram_token", "123:bot-token"),
        (key_ref, "telegram_api_key", "rag_key_for_bot"),
    ):
        assert (
            client.put(
                f"/v1/admin/credentials/{reference}",
                json={"reference": reference, "kind": kind, "secret": value},
            ).status_code
            == 200
        )
    assert client.put("/v1/admin/telegram", json={"enabled": True}).status_code == 422
    configured = client.put(
        "/v1/admin/telegram",
        json={
            "enabled": True,
            "token_credential_ref": token_ref,
            "api_key_credential_ref": key_ref,
        },
    )
    assert configured.status_code == 200
    assert configured.json()["config_version"] == 1
    assert "bot-token" not in configured.text

    def handler(request: httpx.Request):
        assert request.url.path.endswith("/getMe")
        return httpx.Response(200, json={"ok": True})

    runtime["telegram_test_client"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert client.post("/v1/admin/telegram/test").json() == {"status": "ready"}
    finally:
        asyncio.run(runtime["telegram_test_client"].aclose())
    reloaded = client.get("/v1/admin/telegram").json()
    assert reloaded["last_test_status"] == "ready"


def test_setup_progress_is_tenant_bound():
    client, runtime, principal = context()
    with runtime["setup_repository"].session_factory.begin() as session:
        session.add(
            SystemSetup(
                id=1,
                tenant_id=principal.tenant_id,
                administrator_id=principal.user_id,
                current_step="models",
                bootstrap_completed_at=datetime.now(UTC),
            )
        )
    progress = client.put("/v1/setup/progress", json={"step": "telegram"})
    assert progress.status_code == 200
    assert progress.json()["current_step"] == "telegram"
    complete = client.put("/v1/setup/progress", json={"step": "complete"})
    assert complete.json()["onboarding_complete"] is True
