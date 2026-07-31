from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.catalog.repository import CatalogRepository, IdempotencyConflictError
from app.connectors.base import ConnectorDocument, DiscoveryResult, ParsedChunk
from app.ingestion.contracts import IngestionJobContract
from app.ingestion.pipeline import IngestionPipeline
from app.state.models import (
    Base,
    ContentBlob,
    DocumentChunk,
    IngestionRun,
    NormalizedDocument,
    SourceVersion,
    Tenant,
)


def document(key: str, text: str) -> ConnectorDocument:
    checksum = hashlib.sha256(text.encode()).hexdigest()
    return ConnectorDocument(
        object_key=key,
        canonical_uri=f"https://docs.example.test/{key}",
        title=key,
        text=text,
        checksum=checksum,
        byte_count=len(text.encode()),
        metadata={"etag": f'"{checksum[:8]}"'},
        chunks=(ParsedChunk(text=text, checksum=checksum, chunk_index=0),),
    )


class SequentialConnector:
    connector_version = "website/1.0"

    def __init__(self, *results):
        self.results = list(results)

    async def discover(self, _config, _previous):
        return self.results.pop(0)


@pytest.fixture
def ingestion_context():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="ingestion", name="Ingestion")
        other = Tenant(slug="other", name="Other")
        session.add_all([tenant, other])
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
    return repository, factory, tenant, other, source


def create_job(repository, tenant, source, key: str, auto_activate=True):
    run, version, created = repository.create_refresh_job(
        tenant.id,
        source.id,
        idempotency_key=key,
        request_hash=hashlib.sha256(f"{source.id}:{auto_activate}".encode()).hexdigest(),
        correlation_id=uuid.uuid4(),
        auto_activate=auto_activate,
        max_attempts=3,
    )
    contract = IngestionJobContract(
        run_id=run.id,
        tenant_id=tenant.id,
        source_id=source.id,
        source_version_id=version.id,
        correlation_id=run.correlation_id,
    )
    return run, version, contract, created


@pytest.mark.asyncio
async def test_pipeline_incremental_content_reuse_activation_and_tenant_isolation(
    ingestion_context,
):
    repository, factory, tenant, other, source = ingestion_context
    first_document = document("index", "# Docs\nHello")
    connector = SequentialConnector(
        DiscoveryResult(
            documents=(first_document,), added=("index",), modified=(), unchanged=(), deleted=()
        ),
        DiscoveryResult(documents=(), added=(), modified=(), unchanged=("index",), deleted=()),
    )
    pipeline = IngestionPipeline(repository, connector, connector)

    run, version, contract, _ = create_job(repository, tenant, source, "refresh-1")
    _running, lease = repository.claim_ingestion_run(run.id, "worker", 60)
    events = []
    await pipeline.execute(contract, lambda event, stage: _record(events, event, stage))
    assert repository.complete_ingestion_job(run.id, lease)
    assert repository.get_version(tenant.id, source.id, version.id).status == "active"

    second_run, second_version, second_contract, _ = create_job(
        repository, tenant, source, "refresh-2"
    )
    _running, second_lease = repository.claim_ingestion_run(second_run.id, "worker", 60)
    await pipeline.execute(second_contract, lambda event, stage: _record(events, event, stage))
    assert repository.complete_ingestion_job(second_run.id, second_lease)
    assert repository.get_version(tenant.id, source.id, version.id).status == "superseded"
    assert repository.get_version(tenant.id, source.id, second_version.id).status == "active"
    assert repository.get_ingestion_run(other.id, second_run.id) is None

    with factory() as session:
        assert session.scalar(select(func.count(ContentBlob.id))) == 1
        documents = list(
            session.scalars(select(NormalizedDocument).order_by(NormalizedDocument.created_at))
        )
        assert len(documents) == 2
        assert documents[0].content_blob_id == documents[1].content_blob_id
        assert session.scalar(select(func.count(DocumentChunk.id))) == 2


async def _record(events, event, stage):
    events.append((event, stage))


def test_refresh_idempotency_cancel_and_atomic_lease(ingestion_context):
    repository, factory, tenant, _other, source = ingestion_context
    run, version, _contract, created = create_job(repository, tenant, source, "same")
    duplicate, duplicate_version, _contract, duplicate_created = create_job(
        repository, tenant, source, "same"
    )
    assert created is True and duplicate_created is False
    assert duplicate.id == run.id and duplicate_version.id == version.id
    with pytest.raises(IdempotencyConflictError):
        repository.create_refresh_job(
            tenant.id,
            source.id,
            idempotency_key="same",
            request_hash="f" * 64,
            correlation_id=uuid.uuid4(),
            auto_activate=False,
            max_attempts=3,
        )

    claimed = repository.claim_ingestion_run(run.id, "first", 60)
    assert claimed is not None
    assert repository.claim_ingestion_run(run.id, "second", 60) is None
    assert repository.request_ingestion_cancel(tenant.id, run.id) is True
    assert repository.cancel_ingestion_job(run.id, claimed[1]) is True
    with factory() as session:
        assert session.get(IngestionRun, run.id).status == "cancelled"
        assert session.get(SourceVersion, version.id).status == "failed"
