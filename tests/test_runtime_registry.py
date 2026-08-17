from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.operations.repository import OperationsRepository
from app.operations.runtime_registry import (
    RegistryEmbeddingGateway,
    RuntimeConfigurationError,
    RuntimeRegistry,
    render_prompt,
)
from app.state.models import Base, ModelDefinition, PromptTemplate, Tenant


def registry_context():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="runtime", name="Runtime")
        session.add(tenant)
        session.flush()
        global_model = ModelDefinition(
            role="generation",
            model_id="global",
            version="1",
            endpoint_ref="endpoint:generation",
            capabilities={"stream": True},
            config_checksum="a" * 64,
            is_active=True,
        )
        global_embedding = ModelDefinition(
            role="embedding",
            model_id="BAAI/bge-m3",
            version="bge-m3/1",
            endpoint_ref="endpoint:embedding",
            capabilities={"dimensions": 1024},
            config_checksum="d" * 64,
            is_active=True,
        )
        global_prompt = PromptTemplate(
            name="grounded-answer",
            version="1",
            template="{mode}:{question}:{context}",
            checksum="b" * 64,
            is_active=True,
        )
        session.add_all([global_model, global_embedding, global_prompt])
        session.flush()
        tenant_id = tenant.id
    return OperationsRepository(factory), factory, tenant_id


def test_registry_tenant_override_global_fallback_and_prompt_contract():
    repository, _, tenant_id = registry_context()
    registry = RuntimeRegistry(repository)
    assert registry.model(tenant_id, "generation").model_id == "global"
    prompt = registry.prompt(tenant_id, "grounded-answer")
    assert render_prompt(prompt, question="q", context="c", mode="rag_docs") == "rag_docs:q:c"

    override = repository.create_model(
        tenant_id,
        {
            "role": "generation",
            "model_id": "tenant-model",
            "version": "2",
            "endpoint_ref": "endpoint:generation",
            "credential_ref": None,
            "capabilities": {},
        },
    )
    repository.activate_model(tenant_id, override.id)
    assert registry.model(tenant_id, "generation").definition_id == override.id


def test_registry_rejects_missing_roles_bad_refs_and_invalid_prompts():
    repository, factory, tenant_id = registry_context()
    registry = RuntimeRegistry(repository)
    with pytest.raises(RuntimeConfigurationError, match="reranker"):
        registry.model(tenant_id, "reranker")
    bad = repository.create_model(
        tenant_id,
        {
            "role": "embedding",
            "model_id": "embed",
            "version": "1",
            "endpoint_ref": "endpoint:unknown",
            "credential_ref": None,
            "capabilities": {},
        },
    )
    repository.activate_model(tenant_id, bad.id)
    with pytest.raises(RuntimeConfigurationError, match="Endpoint reference"):
        registry.endpoint(registry.model(tenant_id, "embedding"))
    with factory.begin() as session:
        broken = PromptTemplate(
            tenant_id=tenant_id,
            name="broken",
            version="1",
            template="{missing}",
            checksum="c" * 64,
            is_active=True,
        )
        session.add(broken)
    with pytest.raises(RuntimeConfigurationError, match="invalid template"):
        render_prompt(registry.prompt(tenant_id, "broken"), question="q", context="c", mode="docs")


def test_registry_activation_keeps_one_active_per_tenant_scope():
    repository, _, tenant_id = registry_context()
    prompts = []
    for version in ("2", "3"):
        prompts.append(
            repository.create_prompt(
                tenant_id,
                {
                    "name": "grounded-answer",
                    "version": version,
                    "template": f"{version}:{{question}}:{{context}}:{{mode}}",
                },
            )
        )
    repository.activate_prompt(tenant_id, prompts[0].id)
    repository.activate_prompt(tenant_id, prompts[1].id)
    active = [row for row in repository.list_prompts(tenant_id) if row.is_active]
    assert [row.id for row in active if row.tenant_id == tenant_id] == [prompts[1].id]


@pytest.mark.asyncio
async def test_registry_embedding_gateway_uses_pinned_and_active_index_models(monkeypatch):
    repository, _, tenant_id = registry_context()
    definition = repository.create_model(
        tenant_id,
        {
            "role": "embedding",
            "model_id": "BAAI/bge-m3",
            "version": "contract-v1",
            "endpoint_ref": "endpoint:embedding",
            "credential_ref": "credential:embedding",
            "capabilities": {"dimensions": 1024},
        },
    )
    repository.activate_model(tenant_id, definition.id)
    registry = RuntimeRegistry(repository)
    calls: list[list[str]] = []

    class Client:
        async def embed(self, texts):
            calls.append(texts)
            return [[1.0, 0.0]] * len(texts)

        async def readiness(self):
            return {"status": "ready", "model_ready": True}

        async def close(self):
            calls.append(["closed"])

    monkeypatch.setattr(registry, "embedding_client", lambda _model: Client())
    gateway = RegistryEmbeddingGateway(registry)
    assert await gateway.embed_for_model(definition.id, ["one"]) == [[1.0, 0.0]]
    monkeypatch.setattr(
        repository,
        "resolve_active_index_model",
        lambda _tenant_id, _knowledge_base_id: definition,
    )
    assert await gateway.embed_for_index(tenant_id, uuid.uuid4(), ["two"]) == [[1.0, 0.0]]
    assert await gateway.readiness(tenant_id) == {"status": "ready", "model_ready": True}
    assert await gateway.readiness() == {"status": "ready", "model_ready": True}
    await gateway.close()
    assert calls.count(["closed"]) == 4


@pytest.mark.asyncio
async def test_registry_embedding_gateway_rejects_missing_pins(monkeypatch):
    repository, _, tenant_id = registry_context()
    gateway = RegistryEmbeddingGateway(RuntimeRegistry(repository))
    with pytest.raises(RuntimeConfigurationError, match="removed"):
        await gateway.embed_for_model(uuid.uuid4(), ["text"])
    monkeypatch.setattr(
        repository,
        "resolve_active_index_model",
        lambda _tenant_id, _knowledge_base_id: None,
    )
    with pytest.raises(RuntimeConfigurationError, match="pinned embedding"):
        await gateway.embed_for_index(tenant_id, uuid.uuid4(), ["text"])
