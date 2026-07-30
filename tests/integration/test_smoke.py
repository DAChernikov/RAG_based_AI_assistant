from __future__ import annotations

import os
import uuid

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from app.api.services.llm_service import LLMTemporaryUnavailableError, OpenAICompatibleLLMService
from app.inference.application import QueuedInferenceApplication
from app.inference.contracts import parse_event_contract
from app.inference.redis_queue import RedisInferenceQueue
from app.state.models import Conversation, Tenant, User
from app.state.repositories import ApplicationRepository, IdempotencyConflictError
from app.worker.main import InferenceWorker

pytestmark = pytest.mark.integration


class FakeHTTPProcessor:
    def __init__(self, gateway):
        self.gateway = gateway

    async def execute(self, contract, emit):
        answer = await self.gateway.generate(prompt=contract.question, max_new_tokens=32)
        retrieved = [
            {
                "source": "documentation",
                "doc_id": "integration-source",
                "title": "Integration source",
                "score": 0.9,
            }
        ]
        await emit("meta", {"mode": "rag_docs", "confidence": None, "retrieved": retrieved})
        await emit("token", {"text": answer})
        return {"answer": answer, "mode": "rag_docs", "retrieved": retrieved}


class TransientThenSuccessProcessor:
    def __init__(self):
        self.calls = 0

    async def execute(self, contract, emit):
        self.calls += 1
        if self.calls == 1:
            raise LLMTemporaryUnavailableError()
        await emit("token", {"text": "recovered answer"})
        return {"answer": "recovered answer", "mode": "rag_docs", "retrieved": []}


class PermanentFailureProcessor:
    async def execute(self, contract, emit):
        raise ValueError("sensitive implementation detail")


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
    assert len(job.answer.sources) == 1
    assert job.answer.sources[0].source_id == "integration-source"

    reused, _ = await application.submit(
        {"question": "integration question", "mode": None, "top_k": None, "max_new_tokens": None},
        idempotency_key=f"key-{suffix}",
        conversation_id=None,
    )
    assert reused.created is False
    assert reused.job.id == creation.job.id
    with pytest.raises(IdempotencyConflictError):
        await application.submit(
            {
                "question": "different integration question",
                "mode": None,
                "top_k": None,
                "max_new_tokens": None,
            },
            idempotency_key=f"key-{suffix}",
            conversation_id=None,
        )

    events_raw = await redis.xrange(queue.event_stream_key(contract.job_id))
    events = [parse_event_contract(fields["contract"]) for _, fields in events_raw]
    assert [event.event_type for event in events] == [
        "queued",
        "started",
        "meta",
        "token",
        "completed",
    ]
    resumed = [
        event.event_type
        async for _, event in queue.iter_events(
            contract.job_id,
            last_event_id=events_raw[0][0],
            block_ms=1,
        )
    ]
    assert resumed == ["started", "meta", "token", "completed"]

    with factory.begin() as session:
        session.execute(delete(Conversation).where(Conversation.tenant_id == contract.tenant_id))
        session.execute(delete(User).where(User.tenant_id == contract.tenant_id))
        session.execute(delete(Tenant).where(Tenant.id == contract.tenant_id))
    async for key in redis.scan_iter(f"{prefix}:*"):
        await redis.delete(key)
    await client.aclose()
    await redis.aclose()
    engine.dispose()


@pytest.mark.asyncio
async def test_real_queue_retry_and_terminal_dlq(monkeypatch):
    database_url = os.getenv("INTEGRATION_DATABASE_URL")
    redis_url = os.getenv("INTEGRATION_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("Integration PostgreSQL/Redis URLs are not configured.")

    suffix = uuid.uuid4().hex
    tenant_slug = f"integration-failure-{suffix}"
    user_external_id = f"user-{suffix}"
    prefix = f"rag:test:failure:{suffix}"
    monkeypatch.setattr("app.api.config.settings.compatibility_tenant_slug", tenant_slug)
    monkeypatch.setattr("app.api.config.settings.compatibility_user_external_id", user_external_id)
    monkeypatch.setattr("app.api.config.settings.inference_jobs_stream", f"{prefix}:jobs")
    monkeypatch.setattr("app.api.config.settings.inference_consumer_group", f"{prefix}:workers")
    monkeypatch.setattr("app.api.config.settings.inference_events_prefix", f"{prefix}:events")
    monkeypatch.setattr("app.api.config.settings.inference_dlq_stream", f"{prefix}:dlq")
    monkeypatch.setattr("app.api.config.settings.inference_retry_backoff_sec", 0)
    monkeypatch.setattr("app.api.config.settings.redis_url", redis_url)

    engine = create_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug=tenant_slug, name="Integration failures")
        session.add(tenant)
        session.flush()
        session.add(
            User(
                tenant_id=tenant.id,
                external_id=user_external_id,
                display_name="Integration failures",
            )
        )

    redis = Redis.from_url(redis_url, decode_responses=True)
    queue = RedisInferenceQueue(redis)
    await queue.ensure_group()
    repository = ApplicationRepository(factory)
    application = QueuedInferenceApplication(repository, queue)

    retry_processor = TransientThenSuccessProcessor()
    retry_worker = InferenceWorker(repository, queue, retry_processor, worker_id=f"retry-{suffix}")
    retry_creation, retry_contract = await application.submit(
        {"question": "retry", "mode": None, "top_k": None, "max_new_tokens": None},
        idempotency_key=f"retry-{suffix}",
        conversation_id=None,
    )
    first_delivery = (await queue.read_jobs(retry_worker.worker_id))[0]
    await retry_worker.process_message(*first_delivery)
    assert repository.get_job(retry_creation.job.id).status == "queued"
    second_delivery = (await queue.read_jobs(retry_worker.worker_id))[0]
    await retry_worker.process_message(*second_delivery)
    assert repository.get_job(retry_creation.job.id).status == "completed"
    retry_events = [
        parse_event_contract(fields["contract"]).event_type
        for _, fields in await redis.xrange(queue.event_stream_key(retry_contract.job_id))
    ]
    assert "retrying" in retry_events
    assert retry_events[-1] == "completed"

    failed_creation, failed_contract = await application.submit(
        {"question": "fail", "mode": None, "top_k": None, "max_new_tokens": None},
        idempotency_key=f"fail-{suffix}",
        conversation_id=None,
    )
    failed_worker = InferenceWorker(
        repository,
        queue,
        PermanentFailureProcessor(),
        worker_id=f"failed-{suffix}",
    )
    failed_delivery = (await queue.read_jobs(failed_worker.worker_id))[0]
    await failed_worker.process_message(*failed_delivery)
    failed_job = repository.get_job(failed_creation.job.id)
    assert failed_job.status == "failed"
    assert "sensitive implementation detail" not in failed_job.error_message
    dlq_entries = await redis.xrange(f"{prefix}:dlq")
    assert len(dlq_entries) == 1
    assert dlq_entries[0][1]["job_id"] == str(failed_contract.job_id)
    assert "sensitive implementation detail" not in dlq_entries[0][1]["message"]

    with factory.begin() as session:
        session.execute(
            delete(Conversation).where(Conversation.tenant_id == failed_contract.tenant_id)
        )
        session.execute(delete(User).where(User.tenant_id == failed_contract.tenant_id))
        session.execute(delete(Tenant).where(Tenant.id == failed_contract.tenant_id))
    async for key in redis.scan_iter(f"{prefix}:*"):
        await redis.delete(key)
    await redis.aclose()
    engine.dispose()
