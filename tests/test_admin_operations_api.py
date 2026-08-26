from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.config import settings
from app.api.dependencies import get_principal, get_runtime_state
from app.api.routes import indexing, operations
from app.auth.security import Principal
from app.catalog.service import CatalogService
from app.operations.repository import OperationsRepository
from app.state.auth_repository import AuthRepository
from app.state.models import Base, KnowledgeSource, Tenant, User


class IndexRepository:
    def __init__(self, tenant_id, kb_id):
        self.tenant_id = tenant_id
        self.kb_id = kb_id
        self.version = SimpleNamespace(
            id=uuid.uuid4(),
            knowledge_base_id=kb_id,
            version_number=1,
            status="ready",
            manifest={"entry_count": 1},
            pinned=False,
        )
        self.run = SimpleNamespace(
            id=uuid.uuid4(),
            knowledge_base_id=kb_id,
            index_version_id=self.version.id,
            status="queued",
            attempt_count=0,
            checkpoint={},
            cancel_requested=False,
        )

    def create_run(self, tenant_id, kb_id, *_args):
        if tenant_id != self.tenant_id or kb_id != self.kb_id:
            raise LookupError("Knowledge base was not found.")
        return self.run, True

    def list_runs(self, tenant_id, offset, limit):
        return [self.run][offset : offset + limit] if tenant_id == self.tenant_id else []

    def get_run(self, tenant_id, run_id):
        return self.run if tenant_id == self.tenant_id and run_id == self.run.id else None

    def list_events(self, tenant_id, run_id, offset, limit):
        if self.get_run(tenant_id, run_id) is None:
            raise LookupError("Indexing run was not found.")
        row = SimpleNamespace(
            id=uuid.uuid4(),
            run_id=run_id,
            event_type="queued",
            payload={},
            created_at=datetime.now(UTC),
        )
        return [row][offset : offset + limit]

    def list_versions(self, tenant_id, kb_id):
        return [self.version] if tenant_id == self.tenant_id and kb_id == self.kb_id else []

    def cancel(self, tenant_id, run_id):
        if self.get_run(tenant_id, run_id) is None:
            raise LookupError("Indexing run was not found.")
        self.run.cancel_requested = True
        return True

    def activate(self, tenant_id, version_id):
        if tenant_id != self.tenant_id or version_id != self.version.id:
            raise LookupError("Index version was not found.")
        self.version.status = "active"
        return self.version

    def set_pinned(self, tenant_id, version_id, pinned):
        row = self.activate(tenant_id, version_id)
        row.pinned = pinned
        return row


class Queue:
    def __init__(self):
        self.jobs = []

    async def enqueue(self, contract):
        self.jobs.append(contract)


class Embedding:
    async def readiness(self):
        return {"status": "ready", "model_ready": True}


class Generation:
    async def readiness(self):
        return {"ready": True, "authorization": "must-not-leave-runtime"}

    async def aclose(self):
        return None


class RuntimeRegistry:
    def generation_client(self, _model):
        return Generation()

    def endpoint(self, model):
        return model.capabilities.get("base_url", "http://ollama:11434/v1"), None


def build_client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="admin-api", name="Admin API")
        session.add(tenant)
        session.flush()
        admin = User(
            tenant_id=tenant.id,
            username="admin",
            display_name="Admin",
            role="admin",
        )
        source = KnowledgeSource(
            tenant_id=tenant.id,
            name="docs",
            source_type="website",
            config_version="1.0",
            config={},
        )
        session.add_all([admin, source])
        session.flush()
        ids = tenant.id, admin.id, source.id
    operations_repository = OperationsRepository(factory)
    index_repository = IndexRepository(ids[0], uuid.uuid4())
    queue = Queue()
    runtime = {
        "operations_repository": operations_repository,
        "index_repository": index_repository,
        "indexing_queue": queue,
        "embedding_client": Embedding(),
        "catalog_service": CatalogService(SimpleNamespace()),
        "auth_repository": AuthRepository(factory),
        "settings": settings,
        "runtime_registry": RuntimeRegistry(),
    }
    app = FastAPI()

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.correlation_id = uuid.uuid4()
        return await call_next(request)

    app.include_router(operations.router)
    app.include_router(indexing.router)
    app.dependency_overrides[get_runtime_state] = lambda: runtime
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id=ids[0],
        user_id=ids[1],
        username="admin",
        role="admin",
        auth_method="test",
        scopes=frozenset({"*"}),
    )
    return TestClient(app), runtime, ids, index_repository


def test_typed_operations_api_crud_activation_retention_and_audit_export():
    client, _, (_, _, source_id), _ = build_client()
    schedule = client.put(
        f"/v1/admin/schedules/{source_id}",
        json={"source_id": str(source_id), "interval_seconds": 300, "is_enabled": True},
    )
    assert schedule.status_code == 200
    assert len(client.get("/v1/admin/schedules").json()) == 1
    policy = {
        "source_versions_days": 30,
        "index_versions_days": 30,
        "run_history_days": 7,
        "audit_days": 30,
    }
    assert client.put("/v1/admin/retention", json=policy).json() == policy
    assert "audit_event_ids" in client.post("/v1/admin/retention/dry-run").json()

    model = client.post(
        "/v1/admin/models",
        json={
            "role": "generation",
            "model_id": "qwen",
            "version": "1",
            "endpoint_ref": "endpoint:generation",
            "capabilities": {"stream": True},
        },
    )
    assert model.status_code == 201
    repeated = client.post(
        "/v1/admin/models",
        json={
            "role": "generation",
            "model_id": "qwen",
            "version": "1",
            "endpoint_ref": "endpoint:generation",
            "capabilities": {"stream": True},
        },
    )
    assert repeated.status_code == 201 and repeated.json()["id"] == model.json()["id"]
    checked = client.post(f"/v1/admin/models/{model.json()['id']}/test")
    assert checked.json() == {
        "status": "ready",
        "model_id": "qwen",
        "version": "1",
    }
    assert "authorization" not in checked.text
    assert client.post(f"/v1/admin/models/{model.json()['id']}/activate").json()["is_active"]
    assert client.get("/v1/admin/models").json()[0]["model_id"] == "qwen"

    prompt = client.post(
        "/v1/admin/prompts",
        json={
            "name": "grounded-answer",
            "version": "1",
            "template": "{question} {context} {mode}",
        },
    )
    assert prompt.status_code == 201
    assert client.post(f"/v1/admin/prompts/{prompt.json()['id']}/activate").status_code == 200
    assert client.get("/v1/admin/prompts").json()[0]["name"] == "grounded-answer"
    assert client.get("/v1/admin/audit-events").json()
    export = client.get("/v1/admin/audit-events/export")
    assert export.status_code == 200 and "action" in export.text
    assert client.delete(f"/v1/admin/schedules/{schedule.json()['id']}").status_code == 204


def test_indexing_admin_api_lifecycle_tenant_safe_contract():
    client, runtime, _, repository = build_client()
    started = client.post(
        f"/v1/admin/knowledge-bases/{repository.kb_id}/indexing-runs",
        headers={"Idempotency-Key": "index-1"},
    )
    assert started.status_code == 202
    assert runtime["indexing_queue"].jobs
    assert client.get("/v1/admin/indexing-runs").json()[0]["id"] == str(repository.run.id)
    assert client.get(f"/v1/admin/indexing-runs/{repository.run.id}").status_code == 200
    assert client.get(f"/v1/admin/indexing-runs/{repository.run.id}/events").status_code == 200
    versions = client.get(f"/v1/admin/knowledge-bases/{repository.kb_id}/index-versions")
    assert versions.json()[0]["status"] == "ready"
    pinned = client.put(
        f"/v1/admin/index-versions/{repository.version.id}/pin", json={"pinned": True}
    )
    assert pinned.json()["pinned"] is True
    assert (
        client.post(f"/v1/admin/index-versions/{repository.version.id}/rollback").json()["status"]
        == "active"
    )
    assert client.post(f"/v1/admin/indexing-runs/{repository.run.id}/cancel").status_code == 204
    assert client.get("/v1/admin/embedding-runtime").json()["model_ready"] is True
    assert client.get(f"/v1/admin/indexing-runs/{uuid.uuid4()}").status_code == 404


def test_ollama_catalog_and_explicit_pull_use_configured_self_hosted_endpoint():
    client, runtime, _, _ = build_client()
    observed = []

    def handler(request: httpx.Request):
        observed.append((request.method, str(request.url), request.content))
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen2.5-coder:7b", "size": 4_700_000_000}]},
            )
        if request.url.path == "/api/pull":
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404)

    runtime["ollama_http_transport"] = httpx.MockTransport(handler)
    model = client.post(
        "/v1/admin/models",
        json={
            "role": "generation",
            "model_id": "qwen2.5-coder:7b",
            "version": "1",
            "base_url": "http://ollama.internal:11434/v1",
            "capabilities": {"provider": "ollama"},
        },
    ).json()
    listed = client.get(f"/v1/admin/models/{model['id']}/ollama/models")
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "qwen2.5-coder:7b"
    pulled = client.post(
        f"/v1/admin/models/{model['id']}/ollama/pull",
        json={"model": "qwen2.5:7b"},
    )
    assert pulled.json() == {"status": "success", "model": "qwen2.5:7b"}
    assert [method for method, _, _ in observed] == ["GET", "POST"]
    assert all("/v1/api/" not in url for _, url, _ in observed)
