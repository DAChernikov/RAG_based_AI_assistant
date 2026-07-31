from __future__ import annotations

import os
import time
import uuid

import pytest
from redis.asyncio import Redis

from app.inference.contracts import InferenceJobContract
from app.inference.redis_queue import RedisInferenceQueue
from app.ingestion.contracts import IngestionEvent, IngestionJobContract
from app.ingestion.redis_queue import RedisIngestionQueue

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_pending_message_can_be_reclaimed(monkeypatch):
    redis_url = os.getenv("INTEGRATION_REDIS_URL")
    if not redis_url:
        pytest.skip("Integration Redis URL is not configured.")

    suffix = uuid.uuid4().hex
    prefix = f"rag:test:claim:{suffix}"
    monkeypatch.setattr("app.api.config.settings.inference_jobs_stream", f"{prefix}:jobs")
    monkeypatch.setattr("app.api.config.settings.inference_consumer_group", f"{prefix}:workers")
    monkeypatch.setattr("app.api.config.settings.inference_heartbeat_prefix", f"{prefix}:heartbeat")
    monkeypatch.setattr("app.api.config.settings.worker_claim_idle_ms", 0)

    redis = Redis.from_url(redis_url, decode_responses=True)
    queue = RedisInferenceQueue(redis)
    await queue.ensure_group()
    contract = InferenceJobContract(
        job_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        question="claim",
    )
    await queue.enqueue(contract)
    crashed_delivery = await queue.read_jobs("crashed-worker")
    assert len(crashed_delivery) == 1

    claimed = await queue.claim_stale("recovery-worker")

    assert claimed[0][0] == crashed_delivery[0][0]
    await queue.ack(claimed[0][0])
    await queue.heartbeat(
        {
            "worker_id": "recovery-worker",
            "last_seen_unix": time.time(),
            "retriever_ready": True,
            "model_ready": True,
        }
    )
    assert (await queue.latest_heartbeat())["worker_id"] == "recovery-worker"
    await redis.delete(f"{prefix}:jobs")
    async for key in redis.scan_iter(f"{prefix}:heartbeat:*"):
        await redis.delete(key)
    await redis.aclose()


@pytest.mark.asyncio
async def test_ingestion_stream_reclaim_events_and_dlq(monkeypatch):
    redis_url = os.getenv("INTEGRATION_REDIS_URL")
    if not redis_url:
        pytest.skip("Integration Redis URL is not configured.")
    suffix = uuid.uuid4().hex
    prefix = f"rag:test:ingestion:{suffix}"
    monkeypatch.setattr("app.api.config.settings.ingestion_jobs_stream", f"{prefix}:jobs")
    monkeypatch.setattr("app.api.config.settings.ingestion_consumer_group", f"{prefix}:workers")
    monkeypatch.setattr("app.api.config.settings.ingestion_events_prefix", f"{prefix}:events")
    monkeypatch.setattr("app.api.config.settings.ingestion_dlq_stream", f"{prefix}:dlq")
    monkeypatch.setattr("app.api.config.settings.ingestion_claim_idle_ms", 0)
    redis = Redis.from_url(redis_url, decode_responses=True)
    queue = RedisIngestionQueue(redis)
    await queue.ensure_group()
    contract = IngestionJobContract(
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        source_version_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
    )
    await queue.enqueue(contract)
    crashed = await queue.read_jobs("crashed")
    claimed = await queue.claim_stale("recovered")
    assert claimed[0][0] == crashed[0][0]
    await queue.publish_event(IngestionEvent(run_id=contract.run_id, event_type="started"))
    assert (await queue.list_events(contract.run_id))[0]["event_type"] == "started"
    await queue.send_to_dlq(claimed[0][0], contract, "test_failure", "sanitized")
    assert await redis.xlen(f"{prefix}:dlq") == 1
    await queue.ack(claimed[0][0])
    async for key in redis.scan_iter(f"{prefix}:*"):
        await redis.delete(key)
    await redis.aclose()
