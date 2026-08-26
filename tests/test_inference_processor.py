from __future__ import annotations

import uuid

import pytest

from app.inference.contracts import InferenceJobContract
from app.operations.runtime_registry import ResolvedModel, ResolvedPrompt, RuntimeConfigurationError
from app.retrieval.contracts import RetrievedChunk
from app.worker.processor import InferenceProcessor


class ModelClient:
    async def readiness(self):
        return {"ready": True, "status": "available"}

    async def stream_generate(self, **_kwargs):
        for chunk in ("grounded ", "answer"):
            yield chunk

    async def generate(self, **_kwargs):
        return "```sql\nSELECT order_id FROM public.orders\n```"

    async def aclose(self):
        return None


class Registry:
    generation = ResolvedModel(
        uuid.UUID(int=10),
        "generation",
        "qwen",
        "v2",
        "endpoint:generation",
        None,
        {},
    )

    def model(self, _tenant_id, role):
        if role == "generation":
            return self.generation
        raise RuntimeConfigurationError("optional reranker is absent")

    def prompt(self, _tenant_id, name):
        prompt_id = {
            "grounded-answer": 11,
            "sql-generation": 12,
            "general-answer": 13,
        }[name]
        return ResolvedPrompt(
            uuid.UUID(int=prompt_id),
            name,
            "2.0",
            "Mode {mode}\nQuestion {question}\nContext {context}",
        )

    def generation_client(self, _model):
        return ModelClient()


class Embeddings:
    async def embed_for_index(self, _tenant, _kb, _texts):
        return [[0.0, 1.0]]

    async def readiness(self):
        return {"status": "ready", "model_ready": True}

    async def close(self):
        return None


class Repository:
    def search(self, _tenant, _kb, _query, _vector, _top_k, filters):
        source_type = filters.source_types[0]
        metadata = (
            {"schema": "public", "table": "orders", "columns": ["order_id"]}
            if source_type == "jdbc"
            else {"path": "README.md"}
        )
        return [
            RetrievedChunk(
                chunk_id=uuid.uuid4(),
                source_id=uuid.uuid4(),
                source_version_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                source_type=source_type,
                title="public.orders" if source_type == "jdbc" else "README",
                canonical_uri="local://source",
                text="order_id" if source_type == "jdbc" else "safe documentation",
                score=0.03,
                dense_rank=1,
                sparse_rank=1,
                metadata=metadata,
            )
        ]


def processor():
    instance = object.__new__(InferenceProcessor)
    instance.registry = Registry()
    instance.embeddings = Embeddings()
    instance.retriever = __import__(
        "app.retrieval.hybrid", fromlist=["HybridRetriever"]
    ).HybridRetriever(Repository(), instance.embeddings)
    instance.sql_explain = None
    return instance


def contract(question, mode=None):
    return InferenceJobContract(
        job_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        question=question,
        requested_mode=mode,
    )


def model_only_contract(question):
    value = contract(question, "model")
    value.knowledge_base_id = None
    return value


@pytest.mark.asyncio
async def test_processor_uses_registry_prompt_and_model_for_grounded_stream():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    result = await processor().execute(contract("Explain the documentation"), emit)
    assert result["answer"] == "grounded answer"
    assert result["model_name"] == "qwen@v2"
    assert result["model_definition_id"] == uuid.UUID(int=10)
    assert result["prompt_template_id"] == uuid.UUID(int=11)
    assert result["prompt_version"] == "grounded-answer/2.0"
    assert [kind for kind, _ in events] == ["meta", "token", "token"]


@pytest.mark.asyncio
async def test_processor_uses_registry_sql_prompt_and_preserves_validation(monkeypatch):
    monkeypatch.setattr(
        "app.api.services.sql_service.settings.sql_enable_explain_validation", False
    )
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    result = await processor().execute(contract("Create SQL for orders", "sql"), emit)
    assert result["mode"] == "sql"
    assert result["prompt_version"] == "sql-generation/2.0"
    assert result["prompt_template_id"] == uuid.UUID(int=12)
    assert result["confidence"]["validation"]["is_valid"] is True
    assert events[-1][0] == "token"


@pytest.mark.asyncio
async def test_processor_model_only_uses_general_prompt_without_retrieval():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    result = await processor().execute(model_only_contract("What do you know?"), emit)

    assert result["mode"] == "model"
    assert result["retrieved"] == []
    assert result["prompt_template_id"] == uuid.UUID(int=13)
    assert result["prompt_version"] == "general-answer/2.0"
    assert events[0][1]["retrieved"] == []


@pytest.mark.asyncio
async def test_processor_readiness_is_sanitized_and_registry_aware():
    result = await processor().readiness()
    assert result["model"]["ready"] is True
    assert result["retriever_ready"] is True
