from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.api.config import settings
from app.inference.contracts import InferenceEvent, InferenceJobContract, parse_event_contract


class RedisInferenceQueue:
    def __init__(self, redis: Redis | None = None):
        self.redis = redis or Redis.from_url(settings.redis_url, decode_responses=True)
        self._owns_client = redis is None

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(
                settings.inference_jobs_stream,
                settings.inference_consumer_group,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def ping(self) -> bool:
        try:
            return bool(await self.redis.ping())
        except Exception:
            return False

    async def enqueue(self, contract: InferenceJobContract) -> str:
        return await self.redis.xadd(
            settings.inference_jobs_stream,
            {"contract": contract.model_dump_json()},
            maxlen=settings.redis_stream_maxlen,
            approximate=True,
        )

    async def read_jobs(self, worker_id: str, count: int = 1) -> list[tuple[str, dict]]:
        rows = await self.redis.xreadgroup(
            settings.inference_consumer_group,
            worker_id,
            {settings.inference_jobs_stream: ">"},
            count=count,
            block=settings.worker_block_ms,
        )
        return rows[0][1] if rows else []

    async def claim_stale(self, worker_id: str) -> list[tuple[str, dict]]:
        result = await self.redis.xautoclaim(
            settings.inference_jobs_stream,
            settings.inference_consumer_group,
            worker_id,
            min_idle_time=settings.worker_claim_idle_ms,
            start_id="0-0",
            count=10,
        )
        return result[1] if len(result) > 1 else []

    async def ack(self, message_id: str) -> None:
        await self.redis.xack(
            settings.inference_jobs_stream,
            settings.inference_consumer_group,
            message_id,
        )

    def event_stream_key(self, job_id) -> str:
        return f"{settings.inference_events_prefix}:{job_id}"

    async def next_event_sequence(self, job_id) -> int:
        key = f"{self.event_stream_key(job_id)}:sequence"
        sequence = await self.redis.incr(key)
        await self.redis.expire(key, settings.inference_event_ttl_sec)
        return int(sequence)

    async def publish_event(self, event: InferenceEvent) -> str:
        key = self.event_stream_key(event.job_id)
        event_id = await self.redis.xadd(
            key,
            {"contract": event.model_dump_json()},
            maxlen=settings.inference_event_maxlen,
            approximate=True,
        )
        await self.redis.expire(key, settings.inference_event_ttl_sec)
        return event_id

    async def iter_events(
        self,
        job_id,
        *,
        last_event_id: str = "0-0",
        block_ms: int = 15000,
    ) -> AsyncIterator[tuple[str, InferenceEvent]]:
        key = self.event_stream_key(job_id)
        cursor = last_event_id
        while True:
            rows = await self.redis.xread({key: cursor}, count=100, block=block_ms)
            if not rows:
                continue
            for event_id, fields in rows[0][1]:
                cursor = event_id
                event = parse_event_contract(fields["contract"])
                yield event_id, event
                if event.event_type in {"completed", "failed"}:
                    return

    async def send_to_dlq(
        self,
        *,
        message_id: str,
        job_id: str | None,
        correlation_id: str | None,
        error_code: str,
        message: str,
    ) -> str:
        return await self.redis.xadd(
            settings.inference_dlq_stream,
            {
                "source_message_id": message_id,
                "job_id": job_id or "",
                "correlation_id": correlation_id or "",
                "error_code": error_code[:100],
                "message": message[:500],
            },
            maxlen=settings.redis_stream_maxlen,
            approximate=True,
        )

    async def heartbeat(self, payload: dict) -> None:
        key = f"{settings.inference_heartbeat_prefix}:{payload['worker_id']}"
        await self.redis.set(
            key,
            json.dumps(payload, separators=(",", ":")),
            ex=settings.worker_heartbeat_ttl_sec,
        )

    async def latest_heartbeat(self) -> dict | None:
        newest = None
        async for key in self.redis.scan_iter(f"{settings.inference_heartbeat_prefix}:*"):
            raw = await self.redis.get(key)
            if not raw:
                continue
            payload = json.loads(raw)
            if newest is None or payload.get("last_seen_unix", 0) > newest.get("last_seen_unix", 0):
                newest = payload
        if (
            newest
            and time.time() - newest.get("last_seen_unix", 0) <= settings.worker_stale_after_sec
        ):
            return newest
        return None

    async def close(self) -> None:
        if self._owns_client:
            await self.redis.aclose()
