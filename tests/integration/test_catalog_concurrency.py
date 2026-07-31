from __future__ import annotations

import asyncio
import concurrent.futures
import os
import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from app.auth.rate_limit import RedisLoginRateLimiter
from app.auth.security import AuthenticationError
from app.catalog.repository import CatalogRepository
from app.catalog.service import CatalogService
from app.state.models import KnowledgeSource, SourceVersion, Tenant

pytestmark = pytest.mark.integration


def test_atomic_concurrent_activation_and_redis_rate_limit():
    database_url = os.getenv("INTEGRATION_DATABASE_URL")
    redis_url = os.getenv("INTEGRATION_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("Integration PostgreSQL and Redis URLs are not configured.")

    suffix = uuid.uuid4().hex
    engine = create_engine(database_url, pool_size=5)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug=f"catalog-{suffix}", name="Catalog concurrency")
        session.add(tenant)
        session.flush()
        source = KnowledgeSource(
            tenant_id=tenant.id,
            name="Concurrent source",
            source_type="website",
            config_version="1.0",
            config={
                "source_type": "website",
                "config_version": "1.0",
                "root_url": "https://example.com/",
                "allowed_domains": ["example.com"],
            },
            is_enabled=True,
        )
        session.add(source)
        session.flush()

    repository = CatalogRepository(factory)
    service = CatalogService(repository)
    objects = [
        {
            "object_key": "index",
            "checksum": "a" * 64,
            "byte_count": 1,
            "chunk_count": 1,
        }
    ]
    first = asyncio.run(service.record_fixture_ingestion(tenant.id, source.id, objects=objects))
    second = asyncio.run(
        service.record_fixture_ingestion(
            tenant.id,
            source.id,
            objects=[{**objects[0], "object_key": "next"}],
        )
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda version_id: repository.activate_version(tenant.id, source.id, version_id),
                [first.id, second.id],
            )
        )
    assert {item.id for item in results} == {first.id, second.id}
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(SourceVersion.id)).where(
                    SourceVersion.source_id == source.id,
                    SourceVersion.status == "active",
                )
            )
            == 1
        )

    async def rate_limit_scenario():
        redis = Redis.from_url(redis_url, decode_responses=True)
        prefix = f"test:catalog-login:{suffix}"
        first_limiter = RedisLoginRateLimiter(redis, 2, 60, prefix)
        second_limiter = RedisLoginRateLimiter(redis, 2, 60, prefix)
        await asyncio.gather(
            first_limiter.check("tenant:user"),
            second_limiter.check("tenant:user"),
        )
        with pytest.raises(AuthenticationError):
            await first_limiter.check("tenant:user")
        await first_limiter.reset("tenant:user")
        await second_limiter.check("tenant:user")
        await redis.aclose()

    asyncio.run(rate_limit_scenario())
    with factory.begin() as session:
        session.execute(delete(Tenant).where(Tenant.id == tenant.id))
    engine.dispose()
