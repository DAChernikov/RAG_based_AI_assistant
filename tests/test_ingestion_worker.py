from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.catalog.repository import CatalogRepository, LeaseLostError
from app.connectors.base import DiscoveryResult, TransientConnectorError
from app.ingestion.contracts import IngestionJobContract
from app.ingestion_worker.main import IngestionWorker
from app.state.models import Base, Tenant


class FakeQueue:
    def __init__(self):
        self.events = []
        self.acked = []
        self.enqueued = []
        self.dlq = []

    async def publish_event(self, event):
        self.events.append(event)

    async def ack(self, message_id):
        self.acked.append(message_id)

    async def enqueue(self, contract):
        self.enqueued.append(contract)

    async def send_to_dlq(self, message_id, contract, code, message):
        self.dlq.append((message_id, contract, code, message))


class FakePipeline:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0

    async def execute(self, _contract, emit, *, lease_token=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise TransientConnectorError("private upstream detail")
        await emit("progress", "validate")


def context(max_attempts=2):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="worker", name="Worker")
        session.add(tenant)
        session.flush()
    repository = CatalogRepository(factory)
    source = repository.create_source(
        tenant.id,
        "Docs",
        "website",
        "1.0",
        {
            "source_type": "website",
            "config_version": "1.0",
            "root_url": "https://docs.example.test/",
            "allowed_domains": ["docs.example.test"],
        },
        True,
    )
    run, version, _ = repository.create_refresh_job(
        tenant.id,
        source.id,
        idempotency_key="worker-test",
        request_hash=hashlib.sha256(b"worker-test").hexdigest(),
        correlation_id=uuid.uuid4(),
        auto_activate=True,
        max_attempts=max_attempts,
    )
    contract = IngestionJobContract(
        run_id=run.id,
        tenant_id=tenant.id,
        source_id=source.id,
        source_version_id=version.id,
        correlation_id=run.correlation_id,
    )
    return repository, run, version, contract


@pytest.mark.asyncio
async def test_ingestion_worker_retry_then_duplicate_delivery_is_idempotent():
    repository, run, _version, contract = context()
    queue = FakeQueue()
    pipeline = FakePipeline(failures=1)
    worker = IngestionWorker(repository, queue, pipeline, worker_id="test")

    await worker.process_message("1-0", {"contract": contract.model_dump_json()})
    assert repository.get_ingestion_run(contract.tenant_id, run.id).status == "queued"
    assert queue.enqueued == [contract]
    await worker.process_message("2-0", {"contract": contract.model_dump_json()})
    await worker.process_message("3-0", {"contract": contract.model_dump_json()})

    assert pipeline.calls == 2
    assert repository.get_ingestion_run(contract.tenant_id, run.id).status == "completed"
    assert queue.acked == ["1-0", "2-0", "3-0"]


@pytest.mark.asyncio
async def test_ingestion_worker_exhaustion_is_sanitized_and_dlq():
    repository, run, version, contract = context(max_attempts=1)
    queue = FakeQueue()
    worker = IngestionWorker(repository, queue, FakePipeline(failures=2), worker_id="test")

    await worker.process_message("1-0", {"contract": contract.model_dump_json()})

    failed = repository.get_ingestion_run(contract.tenant_id, run.id)
    failed_version = repository.get_version(contract.tenant_id, contract.source_id, version.id)
    assert failed.status == failed_version.status == "failed"
    assert failed.error_message == "Ingestion failed. See logs using the correlation ID."
    assert "private upstream detail" not in queue.dlq[0][3]


@pytest.mark.asyncio
async def test_stale_exhausted_delivery_is_atomically_failed_and_dlq():
    repository, run, version, contract = context(max_attempts=1)
    claimed = repository.claim_ingestion_run(run.id, "crashed", 60)
    assert claimed is not None
    with repository.session_factory.begin() as session:
        stored = session.get(type(run), run.id)
        stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    queue = FakeQueue()
    pipeline = FakePipeline()
    worker = IngestionWorker(repository, queue, pipeline, worker_id="recovery")

    await worker.process_message("stale-1", {"contract": contract.model_dump_json()})

    assert pipeline.calls == 0
    assert repository.get_ingestion_run(contract.tenant_id, run.id).status == "failed"
    assert (
        repository.get_version(contract.tenant_id, contract.source_id, version.id).status
        == "failed"
    )
    assert queue.acked == ["stale-1"]
    assert queue.dlq[0][2] == "retry_exhausted"


def test_stale_lease_cannot_mutate_version_or_documents():
    repository, run, version, contract = context(max_attempts=3)
    _running, stale_token = repository.claim_ingestion_run(run.id, "first", 60)
    with repository.session_factory.begin() as session:
        stored = session.get(type(run), run.id)
        stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert repository.claim_ingestion_run(run.id, "second", 60) is not None

    with pytest.raises(LeaseLostError):
        repository.transition_version(
            contract.tenant_id,
            contract.source_id,
            contract.source_version_id,
            "ingesting",
            run_id=run.id,
            lease_token=stale_token,
        )

    with pytest.raises(LeaseLostError):
        repository.persist_discovery(
            contract.tenant_id,
            contract.source_id,
            contract.source_version_id,
            DiscoveryResult(
                documents=(),
                added=(),
                modified=(),
                unchanged=(),
                deleted=(),
            ),
            None,
            {"checksum": "0" * 64},
            "0" * 64,
            run_id=run.id,
            lease_token=stale_token,
        )
