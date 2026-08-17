from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest

from app.inference.application import QueuedInferenceApplication, serialize_job
from app.inference.contracts import (
    CompletedEvent,
    CompletedPayload,
    InferenceJobContract,
    QueuedEvent,
    QueuedPayload,
)
from app.inference.redis_queue import RedisInferenceQueue
from app.state.models import JobStatus
from app.state.repositories import JobCreationResult


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.streams = {}
        self.closed = False
        self.read_rows = []

    async def xgroup_create(self, *_args, **_kwargs):
        return True

    async def ping(self):
        return True

    async def xadd(self, key, fields, **_kwargs):
        event_id = f"{len(self.streams.get(key, [])) + 1}-0"
        self.streams.setdefault(key, []).append((event_id, fields))
        return event_id

    async def xreadgroup(self, *_args, **_kwargs):
        return [("jobs", [("1-0", {"contract": "payload"})])]

    async def xautoclaim(self, *_args, **_kwargs):
        return ("0-0", [("2-0", {"contract": "payload"})])

    async def xack(self, *_args):
        return 1

    async def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def expire(self, *_args):
        return True

    async def xread(self, *_args, **_kwargs):
        if self.read_rows:
            return [("events", self.read_rows.pop(0))]
        return []

    async def set(self, key, value, **_kwargs):
        self.values[key] = value

    async def scan_iter(self, _pattern):
        for key in list(self.values):
            if "heartbeat" in key:
                yield key

    async def get(self, key):
        return self.values.get(key)

    async def aclose(self):
        self.closed = True


def inference_contract():
    return InferenceJobContract(
        job_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        question="question",
    )


@pytest.mark.asyncio
async def test_inference_queue_transport_events_heartbeat_and_dlq(monkeypatch):
    redis = FakeRedis()
    queue = RedisInferenceQueue(redis)
    contract = inference_contract()
    await queue.ensure_group()
    assert await queue.ping()
    assert await queue.enqueue(contract) == "1-0"
    assert (await queue.read_jobs("worker"))[0][0] == "1-0"
    assert (await queue.claim_stale("worker"))[0][0] == "2-0"
    await queue.ack("1-0")
    assert await queue.next_event_sequence(contract.job_id) == 1
    queued = QueuedEvent(
        event_id=str(uuid.uuid4()),
        sequence=1,
        job_id=contract.job_id,
        correlation_id=contract.correlation_id,
        event_type="queued",
        payload=QueuedPayload(),
    )
    await queue.publish_event(queued)
    completed = CompletedEvent(
        event_id=str(uuid.uuid4()),
        sequence=2,
        job_id=contract.job_id,
        correlation_id=contract.correlation_id,
        event_type="completed",
        payload=CompletedPayload(answer="ok", mode="rag_docs"),
    )
    redis.read_rows = [
        [("1-0", {"contract": queued.model_dump_json()})],
        [("2-0", {"contract": completed.model_dump_json()})],
    ]
    observed = [item async for item in queue.iter_events(contract.job_id)]
    assert [item[1].event_type for item in observed] == ["queued", "completed"]
    await queue.send_to_dlq(
        message_id="1-0",
        job_id=str(contract.job_id),
        correlation_id=str(contract.correlation_id),
        error_code="x" * 120,
        message="m" * 600,
    )
    await queue.heartbeat({"worker_id": "a", "last_seen_unix": time.time()})
    await queue.heartbeat({"worker_id": "b", "last_seen_unix": time.time() - 5})
    assert (await queue.latest_heartbeat())["worker_id"] == "a"
    await queue.close()
    assert redis.closed is False


class Repository:
    def __init__(self, job):
        self.job = job
        self.created = True

    async def create_job(self, **_kwargs):
        return JobCreationResult(self.job, self.created)

    async def get_job(self, _job_id):
        return self.job


class Queue:
    def __init__(self):
        self.jobs = []
        self.events = []

    async def enqueue(self, contract):
        self.jobs.append(contract)

    async def next_event_sequence(self, _job_id):
        return 1

    async def publish_event(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_queued_application_submit_idempotency_wait_and_serialization(monkeypatch):
    job = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_message_id=uuid.uuid4(),
        status=JobStatus.QUEUED.value,
        contract_version="1.0",
        attempt_count=0,
        max_attempts=3,
        cancel_requested=False,
        queued_at=None,
        started_at=None,
        completed_at=None,
        failed_at=None,
        error_code=None,
        error_message=None,
        answer=None,
    )
    repository = Repository(job)
    queue = Queue()
    application = QueuedInferenceApplication(repository, queue)
    payload = {"question": "q", "knowledge_base_id": uuid.uuid4()}
    creation, contract = await application.submit(
        payload,
        tenant_id=job.tenant_id,
        user_id=job.user_id,
        idempotency_key="key",
        conversation_id=None,
    )
    assert creation.created and queue.jobs == [contract]
    assert queue.events[0].event_type == "queued"
    job.status = JobStatus.COMPLETED.value
    assert await application.wait_for_terminal(job.id) is job
    assert serialize_job(job)["answer"] is None

    source = SimpleNamespace(
        source_id="chunk", source_type="git", title="Code", uri="git://x", score=0.8, rank=1
    )
    job.answer = SimpleNamespace(
        id=uuid.uuid4(),
        answer_text="answer",
        mode="rag_code",
        confidence={"router": 1},
        model_name="qwen@1",
        sources=[source],
    )
    serialized = serialize_job(job)
    assert serialized["answer"]["answer"] == "answer"
    assert serialized["sources"][0]["uri"] == "git://x"
