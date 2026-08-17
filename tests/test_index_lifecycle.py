import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.indexing.repository import IndexingConflictError, IndexRepository
from app.state.models import (
    Base,
    ChunkEmbedding,
    ContentBlob,
    DocumentChunk,
    EmbeddingModelVersion,
    KnowledgeBase,
    KnowledgeBaseSource,
    KnowledgeSource,
    ModelDefinition,
    NormalizedDocument,
    SourceVersion,
    Tenant,
)


@pytest.fixture
def index_context():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="tenant", name="Tenant")
        session.add(tenant)
        session.flush()
        kb = KnowledgeBase(tenant_id=tenant.id, name="KB")
        source = KnowledgeSource(
            tenant_id=tenant.id,
            name="docs",
            source_type="website",
            config_version="1.0",
            config={},
        )
        model = EmbeddingModelVersion(
            model_id="BAAI/bge-m3",
            version="bge-m3/1",
            dimensions=1024,
            checksum="0" * 64,
        )
        model_definition = ModelDefinition(
            role="embedding",
            model_id="BAAI/bge-m3",
            version="bge-m3/1",
            endpoint_ref="endpoint:embedding",
            capabilities={"dimensions": 1024},
            config_checksum="d" * 64,
            is_active=True,
        )
        session.add_all([kb, source, model, model_definition])
        session.flush()
        session.add(
            KnowledgeBaseSource(
                tenant_id=tenant.id,
                knowledge_base_id=kb.id,
                source_id=source.id,
            )
        )
        version = SourceVersion(
            tenant_id=tenant.id,
            source_id=source.id,
            version_number=1,
            status="active",
            config_snapshot={},
        )
        blob = ContentBlob(
            tenant_id=tenant.id,
            checksum="a" * 64,
            content="Apache Spark documentation",
            byte_count=26,
        )
        session.add_all([version, blob])
        session.flush()
        document = NormalizedDocument(
            tenant_id=tenant.id,
            source_version_id=version.id,
            object_key="guide",
            canonical_uri="https://docs.example.test/guide",
            title="Guide",
            checksum="b" * 64,
            content_blob_id=blob.id,
        )
        session.add(document)
        session.flush()
        chunk = DocumentChunk(
            tenant_id=tenant.id,
            document_id=document.id,
            chunk_index=0,
            checksum="c" * 64,
            content_blob_id=blob.id,
        )
        session.add(chunk)
        session.flush()
        ids = tenant.id, kb.id
    return IndexRepository(factory), factory, ids


def build_ready(repository, tenant_id, kb_id, key):
    run, created = repository.create_run(tenant_id, kb_id, key, key.ljust(64, "0")[:64], 3)
    assert created is True
    claimed, token = repository.claim(run.id, "worker", 60)
    rows = repository.chunks_for_run(run.id, None, 10)
    repository.persist_batch(run.id, token, rows, [[0.0] * 1024])
    assert repository.complete(run.id, token) is True
    return run


def test_index_build_reuses_embeddings_and_supports_activation_rollback(index_context):
    repository, factory, (tenant_id, kb_id) = index_context
    first = build_ready(repository, tenant_id, kb_id, "first")
    active_first = repository.activate(tenant_id, first.index_version_id)
    assert active_first.status == "active"

    second = build_ready(repository, tenant_id, kb_id, "second")
    active_second = repository.activate(tenant_id, second.index_version_id)
    assert active_second.status == "active"
    assert repository.activate(tenant_id, first.index_version_id).status == "active"
    with factory() as session:
        assert len(list(session.scalars(select(ChunkEmbedding)))) == 1


def test_index_idempotency_lease_fencing_and_cancellation(index_context):
    repository, factory, (tenant_id, kb_id) = index_context
    run, _ = repository.create_run(tenant_id, kb_id, "same", "a" * 64, 2)
    same, created = repository.create_run(tenant_id, kb_id, "same", "a" * 64, 2)
    assert created is False and same.id == run.id
    with pytest.raises(IndexingConflictError):
        repository.create_run(tenant_id, kb_id, "same", "b" * 64, 2)
    _, old_token = repository.claim(run.id, "old", 60)
    with factory.begin() as session:
        stored = session.get(type(run), run.id)
        stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    _, new_token = repository.claim(run.id, "new", 60)
    rows = repository.chunks_for_run(run.id, None, 10)
    with pytest.raises(RuntimeError, match="lease"):
        repository.persist_batch(run.id, old_token, rows, [[0.0] * 1024])
    repository.persist_batch(run.id, new_token, rows, [[0.0] * 1024])
    assert repository.cancel(tenant_id, run.id) is True
    assert repository.complete(run.id, new_token) is True
    assert repository.get_run(tenant_id, run.id).status == "cancelled"


def test_index_validation_rejects_empty_and_wrong_dimension(index_context):
    repository, _, (tenant_id, kb_id) = index_context
    empty, _ = repository.create_run(tenant_id, kb_id, "empty", "e" * 64, 2)
    _, token = repository.claim(empty.id, "worker", 60)
    assert repository.complete(empty.id, token) is False
    assert repository.get_run(tenant_id, empty.id).error_code == "empty_index"
    with pytest.raises(IndexingConflictError):
        repository.activate(tenant_id, empty.index_version_id)

    wrong, _ = repository.create_run(tenant_id, kb_id, "wrong", "f" * 64, 2)
    _, token = repository.claim(wrong.id, "worker", 60)
    rows = repository.chunks_for_run(wrong.id, None, 10)
    with pytest.raises(RuntimeError, match="dimension"):
        repository.persist_batch(wrong.id, token, rows, [[0.0] * 3])


def test_index_retry_failure_events_pagination_and_tenant_boundaries(index_context):
    repository, factory, (tenant_id, kb_id) = index_context
    run, _ = repository.create_run(tenant_id, kb_id, "retry", "1" * 64, 2)
    _, token = repository.claim(run.id, "worker", 1)
    assert repository.renew(run.id, token, 60)
    assert repository.retry(run.id, token, "embedding_unavailable")
    _, second_token = repository.claim(run.id, "worker", 1)
    assert repository.fail(run.id, second_token, "broken")
    assert repository.cancel(tenant_id, run.id) is False
    assert repository.get_run(uuid.uuid4(), run.id) is None
    assert repository.list_runs(tenant_id, 0, 10)[0].id == run.id
    assert repository.list_versions(tenant_id, kb_id)
    events = repository.list_events(tenant_id, run.id, 0, 10)
    assert isinstance(events, list)
    with pytest.raises(LookupError):
        repository.list_events(uuid.uuid4(), run.id, 0, 10)
    with factory.begin() as session:
        stored = session.get(type(run), run.id)
        stored.status = "running"
        stored.attempt_count = stored.max_attempts
        stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert repository.fail_exhausted(run.id)
