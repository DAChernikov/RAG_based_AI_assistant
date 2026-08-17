from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.operations.repository import OperationsRepository
from app.state.models import (
    Base,
    EmbeddingModelVersion,
    KnowledgeBase,
    KnowledgeIndexVersion,
    KnowledgeSource,
    SourceVersion,
    Tenant,
)


def context():
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
        source = KnowledgeSource(
            tenant_id=tenant.id,
            name="docs",
            source_type="website",
            config_version="1.0",
            config={},
        )
        kb = KnowledgeBase(tenant_id=tenant.id, name="KB")
        model = EmbeddingModelVersion(
            model_id="BAAI/bge-m3",
            version="bge-m3/1",
            dimensions=1024,
            checksum="0" * 64,
        )
        session.add_all([source, kb, model])
        session.flush()
        ids = tenant.id, source.id, kb.id, model.id
    return OperationsRepository(factory), factory, ids


def test_schedule_upsert_claim_pause_and_delete():
    repository, factory, (tenant_id, source_id, _, _) = context()
    row = repository.upsert_schedule(tenant_id, source_id, 300, True)
    with factory.begin() as session:
        session.get(type(row), row.id).next_run_at = datetime.now(UTC) - timedelta(seconds=1)
    claimed = repository.claim_due_schedules()
    assert claimed[0][2] == source_id
    assert repository.claim_due_schedules() == []
    paused = repository.upsert_schedule(tenant_id, source_id, 600, False)
    assert paused.is_enabled is False
    assert repository.delete_schedule(tenant_id, paused.id) is True


def test_retention_never_selects_active_or_pinned_versions():
    repository, factory, (tenant_id, source_id, kb_id, model_id) = context()
    old = datetime.now(UTC) - timedelta(days=500)
    with factory.begin() as session:
        source = session.get(KnowledgeSource, source_id)
        failed = SourceVersion(
            tenant_id=tenant_id,
            source_id=source_id,
            version_number=1,
            status="failed",
            config_snapshot=source.config,
            updated_at=old,
        )
        active = SourceVersion(
            tenant_id=tenant_id,
            source_id=source_id,
            version_number=2,
            status="active",
            config_snapshot=source.config,
            updated_at=old,
        )
        pinned = KnowledgeIndexVersion(
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            embedding_model_version_id=model_id,
            version_number=1,
            status="superseded",
            pinned=True,
            manifest={},
            created_at=old,
        )
        session.add_all([failed, active, pinned])
        session.flush()
        expected = failed.id
    candidates = repository.retention_candidates(tenant_id)
    assert candidates["source_version_ids"] == [expected]
    assert candidates["index_version_ids"] == []
    repository.execute_retention(tenant_id)
    with factory() as session:
        assert session.get(SourceVersion, expected) is None


def test_model_registry_activation_is_atomic_per_role():
    repository, _, (tenant_id, _, _, _) = context()
    first = repository.create_model(
        tenant_id,
        {
            "role": "generation",
            "model_id": "qwen-a",
            "version": "1",
            "endpoint_ref": "endpoint:generation",
            "credential_ref": None,
            "capabilities": {"stream": True},
        },
    )
    second = repository.create_model(
        tenant_id,
        {
            "role": "generation",
            "model_id": "qwen-b",
            "version": "1",
            "endpoint_ref": "endpoint:generation",
            "credential_ref": None,
            "capabilities": {"stream": True},
        },
    )
    repository.activate_model(tenant_id, first.id)
    repository.activate_model(tenant_id, second.id)
    active = [row for row in repository.list_models(tenant_id) if row.is_active]
    assert [row.id for row in active] == [second.id]
