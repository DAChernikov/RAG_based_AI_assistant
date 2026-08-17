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
        session.add_all([kb, source, model])
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
