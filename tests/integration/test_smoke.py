from __future__ import annotations

import os
import uuid

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from app.api.services.llm_service import OpenAICompatibleLLMService
from app.inference.application import QueuedInferenceApplication
from app.inference.contracts import parse_event_contract
from app.inference.redis_queue import RedisInferenceQueue
from app.state.models import Conversation, Tenant, User
from app.state.repositories import ApplicationRepository
from app.worker.main import InferenceWorker

pytestmark = pytest.mark.integration


class FakeHTTPProcessor:
    def __init__(self, gateway):
        self.gateway = gateway

    async def execute(self, contract, emit):
        answer = await self.gateway.generate(prompt=contract.question, max_new_tokens=32)
        await emit("meta", {"mode": "rag_docs", "confidence": None, "retrieved": []})
        await emit("token", {"text": answer})
        return {"answer": answer, "mode": "rag_docs", "retrieved": []}


@pytest.mark.asyncio
async def test_postgres_redis_worker_end_to_end(monkeypatch):
    database_url = os.getenv("INTEGRATION_DATABASE_URL")
    redis_url = os.getenv("INTEGRATION_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("Integration PostgreSQL/Redis URLs are not configured.")

    suffix = uuid.uuid4().hex
    tenant_slug = f"integration-{suffix}"
    user_external_id = f"user-{suffix}"
    prefix = f"rag:test:{suffix}"
    monkeypatch.setattr("app.api.config.settings.compatibility_tenant_slug", tenant_slug)
    monkeypatch.setattr("app.api.config.settings.compatibility_user_external_id", user_external_id)
    monkeypatch.setattr("app.api.config.settings.inference_jobs_stream", f"{prefix}:jobs")
    monkeypatch.setattr("app.api.config.settings.inference_consumer_group", f"{prefix}:workers")
    monkeypatch.setattr("app.api.config.settings.inference_events_prefix", f"{prefix}:events")
    monkeypatch.setattr("app.api.config.settings.inference_dlq_stream", f"{prefix}:dlq")
    monkeypatch.setattr("app.api.config.settings.redis_url", redis_url)

    engine = create_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug=tenant_slug, name="Integration")
        session.add(tenant)
        session.flush()
        session.add(
            User(
                tenant_id=tenant.id,
                external_id=user_external_id,
                display_name="Integration",
            )
        )

    redis = Redis.from_url(redis_url, decode_responses=True)
    queue = RedisInferenceQueue(redis)
    await queue.ensure_group()
    repository = ApplicationRepository(factory)
    application = QueuedInferenceApplication(repository, queue)

    fake_model = FastAPI()

    @fake_model.post("/v1/chat/completions")
    async def chat_completions():
        return {"choices": [{"message": {"content": "fake model answer"}}]}

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_model),
        base_url="http://fake-model",
    )
    gateway = OpenAICompatibleLLMService(
        api_base_url="http://fake-model/v1",
        model="fake-model",
        retries=0,
        client=client,
    )
    worker = InferenceWorker(
        repository,
        queue,
        FakeHTTPProcessor(gateway),
        worker_id=f"worker-{suffix}",
    )

    creation, contract = await application.submit(
        {"question": "integration question", "mode": None, "top_k": None, "max_new_tokens": None},
        idempotency_key=f"key-{suffix}",
        conversation_id=None,
    )
    messages = await queue.read_jobs(worker.worker_id)
    assert len(messages) == 1
    await worker.process_message(*messages[0])

    job = repository.get_job(creation.job.id)
    assert job.status == "completed"
    assert job.answer.answer_text == "fake model answer"
    assert job.answer.sources == []

    reused, _ = await application.submit(
        {"question": "integration question", "mode": None, "top_k": None, "max_new_tokens": None},
        idempotency_key=f"key-{suffix}",
        conversation_id=None,
    )
    assert reused.created is False
    assert reused.job.id == creation.job.id

    events_raw = await redis.xrange(queue.event_stream_key(contract.job_id))
    events = [parse_event_contract(fields["contract"]) for _, fields in events_raw]
    assert [event.event_type for event in events] == [
        "queued",
        "started",
        "meta",
        "token",
        "completed",
    ]

    with factory.begin() as session:
        session.execute(delete(Conversation).where(Conversation.tenant_id == contract.tenant_id))
        session.execute(delete(User).where(User.tenant_id == contract.tenant_id))
        session.execute(delete(Tenant).where(Tenant.id == contract.tenant_id))
    async for key in redis.scan_iter(f"{prefix}:*"):
        await redis.delete(key)
    await client.aclose()
    await redis.aclose()
    engine.dispose()
