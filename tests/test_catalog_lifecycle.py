from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.catalog.repository import (
    CatalogRepository,
    ImmutableVersionError,
    InvalidVersionTransitionError,
)
from app.catalog.service import CatalogService
from app.state.models import Base, IngestionRun, SourceObject, SourceVersion, Tenant


@pytest.fixture
def catalog_context():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug=f"catalog-{uuid.uuid4().hex}", name="Catalog")
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
            "root_url": "https://docs.example.com/",
            "allowed_domains": ["example.com"],
        },
        True,
    )
    return repository, CatalogService(repository), factory, tenant, source


@pytest.mark.asyncio
async def test_version_lifecycle_immutability_activation_and_rollback(catalog_context):
    repository, service, factory, tenant, source = catalog_context
    objects = [
        {
            "object_key": "index.html",
            "checksum": "a" * 64,
            "byte_count": 100,
            "chunk_count": 2,
        }
    ]
    first = await service.record_fixture_ingestion(tenant.id, source.id, objects=objects)
    second = await service.record_fixture_ingestion(
        tenant.id,
        source.id,
        objects=[{**objects[0], "object_key": "next.html"}],
    )
    assert first.status == second.status == "ready"

    active_first = repository.activate_version(tenant.id, source.id, first.id)
    active_second = repository.activate_version(tenant.id, source.id, second.id)
    assert active_first.status == "active"
    assert active_second.status == "active"
    assert repository.get_version(tenant.id, source.id, first.id).status == "superseded"

    rolled_back = repository.activate_version(tenant.id, source.id, first.id)
    assert rolled_back.status == "active"
    assert repository.get_version(tenant.id, source.id, second.id).status == "superseded"

    with pytest.raises(ImmutableVersionError):
        repository.write_version_content(
            tenant.id,
            source.id,
            first.id,
            objects=[],
            manifest={},
            content_checksum="b" * 64,
        )

    with factory() as session:
        assert session.scalar(select(func.count(SourceObject.id))) == 2
        assert session.scalar(select(func.count(IngestionRun.id))) == 2
        assert (
            session.scalar(
                select(func.count(SourceVersion.id)).where(SourceVersion.status == "active")
            )
            == 1
        )


def test_allowed_and_forbidden_transitions(catalog_context):
    repository, _, _, tenant, source = catalog_context
    version = repository.create_version(tenant.id, source.id)
    version = repository.transition_version(tenant.id, source.id, version.id, "ingesting")
    version = repository.transition_version(tenant.id, source.id, version.id, "failed")
    assert version.status == "failed"
    with pytest.raises(InvalidVersionTransitionError):
        repository.transition_version(tenant.id, source.id, version.id, "active")
    with pytest.raises(InvalidVersionTransitionError):
        repository.activate_version(tenant.id, source.id, version.id)
