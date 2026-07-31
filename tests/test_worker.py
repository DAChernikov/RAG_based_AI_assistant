from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.services.llm_service import LLMTemporaryUnavailableError
from app.inference.contracts import InferenceJobContract
from app.state.models import Answer, Base, Tenant, User
from app.state.repositories import ApplicationRepository
from app.worker.main import InferenceWorker


class FakeQueue:
    def __init__(self):
        self.events = []
        self.acked = []
        self.dlq = []
        self.enqueued = []
        self.sequences = {}

    async def next_event_sequence(self, job_id):
        self.sequences[job_id] = self.sequences.get(job_id, 1) + 1
        return self.sequences[job_id]

    async def publish_event(self, event):
        self.events.append(event)
        return str(len(self.events))

    async def ack(self, message_id):
        self.acked.append(message_id)

    async def send_to_dlq(self, **kwargs):
        self.dlq.append(kwargs)

    async def enqueue(self, contract):
        self.enqueued.append(contract)


class FakeProcessor:
    def __init__(self, failures=0, permanent=False):
        self.calls = 0
        self.failures = failures
        self.permanent = permanent

    async def execute(self, contract, emit):
        self.calls += 1
        if self.permanent:
            raise ValueError("unsafe internal detail")
        if self.calls <= self.failures:
            raise LLMTemporaryUnavailableError()
        await emit("meta", {"mode": "rag_docs", "confidence": None, "retrieved": []})
        await emit("token", {"text": "answer"})
        return {"answer": "answer", "mode": "rag_docs", "retrieved": []}


@pytest.fixture
def worker_context():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="development", name="Development")
        session.add(tenant)
        session.flush()
        user = User(
            tenant_id=tenant.id,
            external_id="development-user",
            username="development-user",
            display_name="Development",
        )
        session.add(user)
        session.flush()
        identity = tenant.id, user.id
    return ApplicationRepository(factory), factory, identity


def create_contract(repository, identity):
    tenant_id, user_id = identity
    creation = repository.create_job(
        tenant_id=tenant_id,
        user_id=user_id,
        question="question",
        request_payload={"question": "question"},
        contract_version="1.0",
        max_attempts=2,
    )
    return InferenceJobContract(
        job_id=creation.job.id,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=creation.job.conversation_id,
        message_id=creation.job.user_message_id,
        question="question",
    )


@pytest.mark.asyncio
async def test_worker_completes_and_duplicate_delivery_is_idempotent(worker_context):
    repository, factory, identity = worker_context
    contract = create_contract(repository, identity)
    queue = FakeQueue()
    worker = InferenceWorker(repository, queue, FakeProcessor(), worker_id="test-worker")

    await worker.process_message("1-0", {"contract": contract.model_dump_json()})
    await worker.process_message("2-0", {"contract": contract.model_dump_json()})

    assert repository.get_job(contract.job_id).status == "completed"
    assert [event.event_type for event in queue.events] == [
        "started",
        "meta",
        "token",
        "completed",
        "completed",
    ]
    assert queue.acked == ["1-0", "2-0"]
    with factory() as session:
        assert session.query(Answer).count() == 1


@pytest.mark.asyncio
async def test_worker_does_not_regenerate_job_with_active_lease(worker_context):
    repository, _, identity = worker_context
    contract = create_contract(repository, identity)
    processor = FakeProcessor()
    queue = FakeQueue()
    worker = InferenceWorker(repository, queue, processor, worker_id="second-worker")

    claimed = repository.claim_job(
        contract.job_id,
        worker_id="first-worker",
        lease_seconds=60,
    )
    assert claimed is not None

    await worker.process_message("2-0", {"contract": contract.model_dump_json()})

    assert processor.calls == 0
    assert queue.events == []
    assert queue.acked == []


@pytest.mark.asyncio
async def test_worker_retries_transient_failure_then_completes(worker_context, monkeypatch):
    repository, _, identity = worker_context
    contract = create_contract(repository, identity)
    queue = FakeQueue()
    processor = FakeProcessor(failures=1)
    worker = InferenceWorker(repository, queue, processor)
    monkeypatch.setattr("app.worker.main.settings.inference_retry_backoff_sec", 0)

    await worker.process_message("1-0", {"contract": contract.model_dump_json()})
    await worker.process_message("2-0", {"contract": queue.enqueued[0].model_dump_json()})

    assert repository.get_job(contract.job_id).status == "completed"
    assert any(event.event_type == "retrying" for event in queue.events)


@pytest.mark.asyncio
async def test_worker_terminal_failure_is_sanitized_and_sent_to_dlq(worker_context):
    repository, _, identity = worker_context
    contract = create_contract(repository, identity)
    queue = FakeQueue()
    worker = InferenceWorker(repository, queue, FakeProcessor(permanent=True))

    await worker.process_message("1-0", {"contract": contract.model_dump_json()})

    job = repository.get_job(contract.job_id)
    assert job.status == "failed"
    assert job.error_message == "Inference job failed. See worker logs using the correlation ID."
    assert queue.dlq[0]["error_code"] == "inference_failed"
    assert "unsafe internal detail" not in queue.dlq[0]["message"]


@pytest.mark.asyncio
async def test_unknown_contract_version_goes_to_dlq(worker_context):
    repository, _, _ = worker_context
    queue = FakeQueue()
    worker = InferenceWorker(repository, queue, FakeProcessor())

    await worker.process_message("1-0", {"contract": '{"contract_version":"99"}'})

    assert queue.acked == ["1-0"]
    assert queue.dlq[0]["error_code"] == "invalid_contract"
