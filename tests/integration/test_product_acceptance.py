from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from app.auth.security import PasswordManager
from app.auth.service import AuthService
from app.catalog.repository import CatalogRepository
from app.connectors.base import ConnectorDocument, DiscoveryResult, ParsedChunk
from app.indexing.repository import IndexRepository
from app.ingestion.contracts import IngestionJobContract
from app.ingestion.pipeline import IngestionPipeline
from app.operations.repository import OperationsRepository
from app.retrieval.contracts import RetrievalFilters
from app.retrieval.hybrid import HybridRetrievalRepository, KnowledgeBaseAccessError
from app.state.auth_repository import AuthRepository
from app.state.models import ChunkEmbedding, Tenant, User

pytestmark = pytest.mark.integration


class Limiter:
    async def check(self, _key):
        return None

    async def reset(self, _key):
        return None


class Connector:
    connector_version = "deterministic-local/1"

    def __init__(self, results):
        self.results = list(results)

    async def discover(self, _config, _previous, **_kwargs):
        return self.results.pop(0)


def doc(key: str, uri: str, text: str, source_type: str):
    checksum = hashlib.sha256(text.encode()).hexdigest()
    return ConnectorDocument(
        object_key=key,
        canonical_uri=uri,
        title=key,
        text=text,
        checksum=checksum,
        byte_count=len(text.encode()),
        metadata={"source_type": source_type, "path": key},
        chunks=(ParsedChunk(text=text, checksum=checksum, chunk_index=0),),
    )


def refresh(repository, tenant_id, source_id, key):
    run, version, _ = repository.create_refresh_job(
        tenant_id,
        source_id,
        idempotency_key=key,
        request_hash=hashlib.sha256(key.encode()).hexdigest(),
        correlation_id=uuid.uuid4(),
        auto_activate=True,
        max_attempts=3,
    )
    _run, lease = repository.claim_ingestion_run(run.id, "acceptance", 60)
    return (
        run,
        version,
        lease,
        IngestionJobContract(
            run_id=run.id,
            tenant_id=tenant_id,
            source_id=source_id,
            source_version_id=version.id,
            correlation_id=run.correlation_id,
        ),
    )


@pytest.mark.asyncio
async def test_product_lifecycle_acceptance_with_real_postgres():
    database_url = os.getenv("INTEGRATION_DATABASE_URL")
    if not database_url:
        pytest.skip("Integration PostgreSQL URL is not configured.")
    engine = create_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    passwords = PasswordManager()
    with factory.begin() as session:
        tenant = Tenant(slug=f"acceptance-{suffix}", name="Acceptance")
        other = Tenant(slug=f"acceptance-other-{suffix}", name="Other")
        session.add_all([tenant, other])
        session.flush()
        admin = User(
            tenant_id=tenant.id,
            username="owner",
            display_name="Owner",
            role="admin",
            password_hash=passwords.hash("owner-password-123"),
        )
        session.add(admin)
        session.flush()
        ids = tenant.id, other.id, admin.id

    auth = AuthService(AuthRepository(factory), Limiter())
    login = await auth.login(f"acceptance-{suffix}", "owner", "owner-password-123", uuid.uuid4())
    principal = await auth.authenticate_access_token(login["access_token"])
    await auth._call(
        auth.repository.create_user,
        ids[0],
        "reader",
        "Reader",
        passwords.hash("reader-password-123"),
        "user",
    )
    key_row, key_value = await auth.create_api_key(
        principal, "telegram", ["inference:read", "inference:write"], None
    )
    assert (await auth.authenticate_api_key(key_value)).tenant_id == ids[0]
    assert key_row.key_hash != key_value and key_value not in key_row.prefix

    catalog = CatalogRepository(factory)
    kb = catalog.create_knowledge_base(ids[0], "Product", "Acceptance KB", True)
    website = catalog.create_source(
        ids[0],
        "Local website",
        "website",
        "1.0",
        {
            "source_type": "website",
            "config_version": "1.0",
            "root_url": "https://local.test/",
            "allowed_domains": ["local.test"],
        },
        True,
    )
    git = catalog.create_source(
        ids[0],
        "Local git fixture",
        "git",
        "1.0",
        {
            "source_type": "git",
            "config_version": "1.0",
            "repository_url": "https://git.local.test/product.git",
            "ref_kind": "commit",
            "ref": "a" * 40,
        },
        True,
    )
    catalog.link_source(ids[0], kb.id, website.id)
    catalog.link_source(ids[0], kb.id, git.id)
    website_doc = doc(
        "guide", "https://local.test/guide", "Настройка очереди и scheduler reliability", "website"
    )
    git_doc = doc(
        "src/worker.py",
        "git://product/src/worker.py",
        "def process_event(job_id): return grounded_answer",
        "git",
    )
    website_connector = Connector(
        [
            DiscoveryResult(
                documents=(website_doc,), added=("guide",), modified=(), unchanged=(), deleted=()
            ),
            DiscoveryResult(documents=(), added=(), modified=(), unchanged=("guide",), deleted=()),
        ]
    )
    git_connector = Connector(
        [
            DiscoveryResult(
                documents=(git_doc,),
                added=("src/worker.py",),
                modified=(),
                unchanged=(),
                deleted=(),
                source_revision="a" * 40,
            )
        ]
    )
    pipeline = IngestionPipeline(catalog, website_connector, git_connector)
    for source, key in ((website, "website-1"), (git, "git-1")):
        run, _version, lease, contract = refresh(catalog, ids[0], source.id, key)
        await pipeline.execute(contract, lambda *_args: _noop(), lease_token=lease)
        assert catalog.complete_ingestion_job(run.id, lease)

    indexes = IndexRepository(factory)
    first, created = indexes.create_run(ids[0], kb.id, "index-1", "1" * 64, 3)
    assert created
    claimed, token = indexes.claim(first.id, "indexer", 60)
    rows = indexes.chunks_for_run(claimed.id, None, 100)
    vectors = [[1.0] + [0.0] * 1023 for _ in rows]
    indexes.persist_batch(first.id, token, rows, vectors)
    assert indexes.complete(first.id, token)
    assert indexes.activate(ids[0], first.index_version_id).status == "active"
    retrieval = HybridRetrievalRepository(factory)
    results = retrieval.search(
        ids[0], kb.id, "scheduler process_event", vectors[0], 5, RetrievalFilters()
    )
    assert {row.source_type for row in results} == {"website", "git"}
    with pytest.raises(KnowledgeBaseAccessError):
        retrieval.resolve_knowledge_base(ids[1], kb.id)

    run, second_source_version, lease, contract = refresh(catalog, ids[0], website.id, "website-2")
    await pipeline.execute(contract, lambda *_args: _noop(), lease_token=lease)
    assert catalog.complete_ingestion_job(run.id, lease)
    second, _ = indexes.create_run(ids[0], kb.id, "index-2", "2" * 64, 3)
    claimed, token = indexes.claim(second.id, "indexer", 60)
    rows = indexes.chunks_for_run(claimed.id, None, 100)
    indexes.persist_batch(second.id, token, rows, [[1.0] + [0.0] * 1023 for _ in rows])
    assert indexes.complete(second.id, token)
    indexes.activate(ids[0], second.index_version_id)
    assert indexes.activate(ids[0], first.index_version_id).status == "active"
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(ChunkEmbedding.id)).where(ChunkEmbedding.tenant_id == ids[0])
            )
            == 2
        )
        assert catalog.get_version(ids[0], website.id, second_source_version.id).status == "active"

    operations = OperationsRepository(factory)
    schedule = operations.upsert_schedule(ids[0], website.id, 300, False)
    assert schedule.is_enabled is False
    operations.upsert_schedule(ids[0], website.id, 300, True)
    dry_run = operations.retention_candidates(ids[0], 100)
    assert first.index_version_id not in dry_run["index_version_ids"]
    executed = operations.execute_retention(ids[0], 100)
    assert first.index_version_id not in executed["index_version_ids"]

    with factory.begin() as session:
        session.execute(delete(Tenant).where(Tenant.id.in_([ids[0], ids[1]])))
    engine.dispose()


async def _noop():
    return None
