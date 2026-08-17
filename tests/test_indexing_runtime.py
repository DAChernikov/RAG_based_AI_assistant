from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.embeddings.client import EmbeddingServiceError
from app.indexing.contracts import IndexingJobContract
from app.indexing.redis_queue import RedisIndexingQueue
from app.indexing_worker.main import IndexingWorker


class FakeRedis:
    def __init__(self):
        self.group_error = None
        self.calls = []

    async def xgroup_create(self, *args, **kwargs):
        self.calls.append(("group", args, kwargs))
        if self.group_error:
            raise RuntimeError(self.group_error)

    async def xadd(self, *args, **kwargs):
        self.calls.append(("xadd", args, kwargs))
        return "1-0"

    async def xreadgroup(self, *args, **kwargs):
        return [("stream", [("1-0", {"contract": "value"})])]

    async def xautoclaim(self, *args, **kwargs):
        return ("0-0", [("2-0", {"contract": "value"})])

    async def xack(self, *args):
        self.calls.append(("ack", args, {}))

    async def set(self, *args, **kwargs):
        self.calls.append(("set", args, kwargs))

    async def aclose(self):
        self.calls.append(("close", (), {}))


@pytest.mark.asyncio
async def test_indexing_redis_queue_contract_and_reclaim():
    redis = FakeRedis()
    queue = RedisIndexingQueue(redis)
    contract = IndexingJobContract(
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        index_version_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
    )
    await queue.ensure_group()
    assert await queue.enqueue(contract) == "1-0"
    assert (await queue.read("worker"))[0][0] == "1-0"
    assert (await queue.claim_stale("worker"))[0][0] == "2-0"
    await queue.ack("1-0")
    await queue.dlq("1-0", contract, "failed")
    await queue.heartbeat("worker")
    await queue.close()
    assert any(call[0] == "close" for call in redis.calls)


class DirectCalls:
    async def call(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class WorkerQueue:
    def __init__(self):
        self.acked = []
        self.dead = []
        self.enqueued = []

    async def ack(self, message_id):
        self.acked.append(message_id)

    async def dlq(self, message_id, contract, code):
        self.dead.append((message_id, contract, code))

    async def enqueue(self, contract):
        self.enqueued.append(contract)


class WorkerRepository:
    def __init__(self, *, exhausted=False, validation_failure=False):
        self.exhausted = exhausted
        self.validation_failure = validation_failure
        self.persisted = []
        self.claimed = True

    def claim(self, run_id, worker_id, lease):
        if not self.claimed:
            return None
        return (
            SimpleNamespace(
                id=run_id,
                checkpoint={},
                model_definition_id=uuid.uuid4(),
            ),
            uuid.uuid4(),
        )

    def chunks_for_run(self, *_args):
        if self.persisted:
            return []
        chunk = SimpleNamespace(id=uuid.uuid4())
        blob = SimpleNamespace(content="text")
        return [(chunk, blob, object(), object(), object())]

    def persist_batch(self, *args):
        self.persisted.append(args)

    def complete(self, *_args):
        return not self.validation_failure

    def activate(self, *_args):
        return True

    def get_run(self, tenant_id, run_id):
        return SimpleNamespace(status="failed", error_code="integrity_mismatch")

    def fail_exhausted(self, _run_id):
        return self.exhausted

    def retry(self, *_args):
        return True

    def finish_cancelled(self, *_args):
        return False

    def fail(self, *_args):
        return True

    def renew(self, *_args):
        return False


class Embeddings:
    def __init__(self, fail=False):
        self.fail = fail

    async def embed_for_model(self, _model, texts):
        if self.fail:
            raise EmbeddingServiceError()
        return [[0.0] * 1024 for _ in texts]


def contract():
    return IndexingJobContract(
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        index_version_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        auto_activate=True,
    )


@pytest.mark.asyncio
async def test_indexing_worker_success_validation_failure_and_retry():
    item = contract()
    queue = WorkerQueue()
    repository = WorkerRepository()
    worker = IndexingWorker(repository, queue, Embeddings(), blocking_io=DirectCalls())
    await worker.process("1-0", {"contract": item.model_dump_json()})
    assert queue.acked == ["1-0"]
    assert repository.persisted

    failed_queue = WorkerQueue()
    failed = IndexingWorker(
        WorkerRepository(validation_failure=True),
        failed_queue,
        Embeddings(),
        blocking_io=DirectCalls(),
    )
    await failed.process("2-0", {"contract": item.model_dump_json()})
    assert failed_queue.dead[0][2] == "integrity_mismatch"

    retry_queue = WorkerQueue()
    retry = IndexingWorker(
        WorkerRepository(), retry_queue, Embeddings(fail=True), blocking_io=DirectCalls()
    )
    await retry.process("3-0", {"contract": item.model_dump_json()})
    assert retry_queue.enqueued == [item]
    assert retry_queue.acked == ["3-0"]


@pytest.mark.asyncio
async def test_indexing_worker_invalid_and_exhausted_contracts():
    queue = WorkerQueue()
    repository = WorkerRepository(exhausted=True)
    repository.claimed = False
    worker = IndexingWorker(repository, queue, Embeddings(), blocking_io=DirectCalls())
    await worker.process("bad", {"contract": "{"})
    await worker.process("old", {"contract": contract().model_dump_json()})
    assert [item[2] for item in queue.dead] == ["invalid_contract", "retry_exhausted"]
    assert queue.acked == ["bad", "old"]
