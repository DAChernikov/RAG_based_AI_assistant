from __future__ import annotations

import json

from redis.asyncio import Redis

from app.api.config import settings
from app.indexing.contracts import IndexingJobContract


class RedisIndexingQueue:
    def __init__(self, redis: Redis | None = None):
        self.redis = redis or Redis.from_url(settings.redis_url, decode_responses=True)

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(
                settings.indexing_jobs_stream,
                settings.indexing_consumer_group,
                id="0-0",
                mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, contract: IndexingJobContract) -> str:
        return await self.redis.xadd(
            settings.indexing_jobs_stream,
            {"contract": contract.model_dump_json()},
            maxlen=settings.indexing_stream_maxlen,
            approximate=True,
        )

    async def read(self, consumer: str):
        rows = await self.redis.xreadgroup(
            settings.indexing_consumer_group,
            consumer,
            {settings.indexing_jobs_stream: ">"},
            count=1,
            block=settings.indexing_worker_block_ms,
        )
        return rows[0][1] if rows else []

    async def claim_stale(self, consumer: str):
        result = await self.redis.xautoclaim(
            settings.indexing_jobs_stream,
            settings.indexing_consumer_group,
            consumer,
            min_idle_time=settings.indexing_claim_idle_ms,
            start_id="0-0",
            count=1,
        )
        return result[1] if result else []

    async def ack(self, message_id: str) -> None:
        await self.redis.xack(
            settings.indexing_jobs_stream, settings.indexing_consumer_group, message_id
        )

    async def dlq(self, message_id: str, contract: IndexingJobContract | None, code: str) -> None:
        await self.redis.xadd(
            settings.indexing_dlq_stream,
            {
                "message_id": message_id,
                "run_id": str(contract.run_id) if contract else "",
                "correlation_id": str(contract.correlation_id) if contract else "",
                "error_code": code,
            },
            maxlen=settings.indexing_stream_maxlen,
            approximate=True,
        )

    async def heartbeat(self, worker_id: str) -> None:
        import time

        key = f"{settings.indexing_heartbeat_prefix}:{worker_id}"
        await self.redis.set(key, json.dumps({"last_seen_unix": time.time()}), ex=30)

    async def close(self) -> None:
        await self.redis.aclose()
