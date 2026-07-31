from __future__ import annotations

import json
import time

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.api.config import settings
from app.ingestion.contracts import IngestionEvent, IngestionJobContract


class RedisIngestionQueue:
    def __init__(self, redis: Redis | None = None):
        self.redis = redis or Redis.from_url(settings.redis_url, decode_responses=True)
        self._owns_client = redis is None

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(
                settings.ingestion_jobs_stream,
                settings.ingestion_consumer_group,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, contract: IngestionJobContract) -> str:
        return await self.redis.xadd(
            settings.ingestion_jobs_stream, {"contract": contract.model_dump_json()}
        )

    async def read_jobs(self, worker_id: str) -> list[tuple[str, dict]]:
        rows = await self.redis.xreadgroup(
            settings.ingestion_consumer_group,
            worker_id,
            {settings.ingestion_jobs_stream: ">"},
            count=1,
            block=settings.ingestion_worker_block_ms,
        )
        return rows[0][1] if rows else []

    async def claim_stale(self, worker_id: str) -> list[tuple[str, dict]]:
        result = await self.redis.xautoclaim(
            settings.ingestion_jobs_stream,
            settings.ingestion_consumer_group,
            worker_id,
            min_idle_time=settings.ingestion_claim_idle_ms,
            start_id="0-0",
            count=10,
        )
        return result[1] if len(result) > 1 else []

    async def ack(self, message_id: str) -> None:
        await self.redis.xack(
            settings.ingestion_jobs_stream, settings.ingestion_consumer_group, message_id
        )

    def event_key(self, run_id) -> str:
        return f"{settings.ingestion_events_prefix}:{run_id}"

    async def publish_event(self, event: IngestionEvent) -> str:
        key = self.event_key(event.run_id)
        event_id = await self.redis.xadd(
            key,
            {"event": event.model_dump_json()},
            maxlen=settings.ingestion_event_maxlen,
            approximate=True,
        )
        await self.redis.expire(key, settings.ingestion_event_ttl_sec)
        return event_id

    async def list_events(self, run_id, start: str = "-") -> list[dict]:
        rows = await self.redis.xrange(self.event_key(run_id), min=start, max="+", count=1000)
        return [{"id": event_id, **json.loads(fields["event"])} for event_id, fields in rows]

    async def send_to_dlq(self, message_id: str, contract, code: str, message: str) -> str:
        return await self.redis.xadd(
            settings.ingestion_dlq_stream,
            {
                "source_message_id": message_id,
                "run_id": str(getattr(contract, "run_id", "")),
                "correlation_id": str(getattr(contract, "correlation_id", "")),
                "error_code": code[:100],
                "message": message[:500],
            },
        )

    async def heartbeat(self, worker_id: str) -> None:
        await self.redis.set(
            f"{settings.ingestion_heartbeat_prefix}:{worker_id}",
            json.dumps({"worker_id": worker_id, "last_seen_unix": time.time()}),
            ex=settings.worker_heartbeat_ttl_sec,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.redis.aclose()
