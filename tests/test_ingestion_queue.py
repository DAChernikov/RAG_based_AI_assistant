from __future__ import annotations

import uuid

import pytest

from app.ingestion.contracts import IngestionEvent, IngestionJobContract
from app.ingestion.redis_queue import RedisIngestionQueue


class Redis:
    def __init__(self):
        self.closed = False
        self.events = []

    async def xgroup_create(self, *_args, **_kwargs):
        return True

    async def xadd(self, key, fields, **_kwargs):
        self.events.append((key, fields))
        return "1-0"

    async def xreadgroup(self, *_args, **_kwargs):
        return [("stream", [("1-0", {"contract": "payload"})])]

    async def xautoclaim(self, *_args, **_kwargs):
        return ("0-0", [("2-0", {"contract": "payload"})])

    async def xack(self, *_args):
        return 1

    async def expire(self, *_args):
        return True

    async def xrange(self, *_args, **_kwargs):
        return [("1-0", self.events[-1][1])]

    async def set(self, *_args, **_kwargs):
        return True

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_ingestion_queue_transport_events_dlq_and_ownership():
    redis = Redis()
    queue = RedisIngestionQueue(redis)
    contract = IngestionJobContract(
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        source_version_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
    )
    await queue.ensure_group()
    assert await queue.enqueue(contract) == "1-0"
    assert (await queue.read_jobs("worker"))[0][0] == "1-0"
    assert (await queue.claim_stale("worker"))[0][0] == "2-0"
    await queue.ack("1-0")
    event = IngestionEvent(run_id=contract.run_id, event_type="queued")
    assert await queue.publish_event(event) == "1-0"
    assert (await queue.list_events(contract.run_id))[0]["event_type"] == "queued"
    assert await queue.send_to_dlq("1-0", contract, "failure", "sanitized") == "1-0"
    await queue.heartbeat("worker")
    await queue.close()
    assert redis.closed is False


@pytest.mark.asyncio
async def test_ingestion_queue_closes_owned_client(monkeypatch):
    redis = Redis()
    monkeypatch.setattr("app.ingestion.redis_queue.Redis.from_url", lambda *_args, **_kwargs: redis)
    queue = RedisIngestionQueue()
    await queue.close()
    assert redis.closed is True
