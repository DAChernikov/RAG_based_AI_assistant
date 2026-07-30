from __future__ import annotations

import os
import time
import uuid

import pytest
from redis.asyncio import Redis

from app.inference.contracts import InferenceJobContract
from app.inference.redis_queue import RedisInferenceQueue

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
