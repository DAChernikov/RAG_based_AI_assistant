from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import signal
import uuid

from redis.asyncio import Redis

from app.api.config import settings
from app.catalog.repository import CatalogRepository
from app.ingestion.contracts import IngestionEvent, IngestionJobContract
from app.ingestion.redis_queue import RedisIngestionQueue
from app.observability import log_event
from app.operations.repository import OperationsRepository
from app.state.auth_repository import AuthRepository
from app.state.database import create_database_engine, create_session_factory


class Scheduler:
    def __init__(self, operations, catalog, audit, queue, redis):
        self.operations = operations
        self.catalog = catalog
        self.audit = audit
        self.queue = queue
        self.redis = redis
        self.stop_event = asyncio.Event()
        self.leader_key = "rag:scheduler:leader"
        self.leader_token = f"{settings.scheduler_id}:{uuid.uuid4()}"

    async def _leader(self) -> bool:
        acquired = await self.redis.set(
            self.leader_key,
            self.leader_token,
            nx=True,
            ex=settings.scheduler_leader_ttl_sec,
        )
        if acquired:
            return True
        if await self.redis.get(self.leader_key) == self.leader_token:
            await self.redis.expire(self.leader_key, settings.scheduler_leader_ttl_sec)
            return True
        return False

    async def tick(self) -> None:
        if not await self._leader():
            return
        due = await asyncio.to_thread(
            self.operations.claim_due_schedules, settings.scheduler_max_catchup
        )
        for schedule_id, tenant_id, source_id, scheduled_for in due:
            correlation_id = uuid.uuid4()
            request = {"source_id": str(source_id), "auto_activate": True}
            request_hash = hashlib.sha256(
                json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            idempotency_key = f"schedule:{schedule_id}:{scheduled_for.isoformat()}"
            try:
                run, version, _created = await asyncio.to_thread(
                    self.catalog.create_refresh_job,
                    tenant_id,
                    source_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    correlation_id=correlation_id,
                    auto_activate=True,
                    max_attempts=settings.ingestion_max_attempts,
                )
                contract = IngestionJobContract(
                    run_id=run.id,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    source_version_id=version.id,
                    correlation_id=correlation_id,
                )
                await self.queue.enqueue(contract)
                await self.queue.publish_event(
                    IngestionEvent(run_id=run.id, event_type="queued", stage="scheduled")
                )
                await asyncio.to_thread(
                    self.audit.audit,
                    tenant_id=tenant_id,
                    actor_user_id=None,
                    action="schedule.trigger",
                    outcome="success",
                    correlation_id=correlation_id,
                    resource_type="source_schedule",
                    resource_id=str(schedule_id),
                )
            except Exception as exc:
                log_event(
                    "scheduler_trigger_failed",
                    correlation_id=correlation_id,
                    error_type=type(exc).__name__,
                )

    async def run(self) -> None:
        await self.queue.ensure_group()
        while not self.stop_event.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self.stop_event.wait(), settings.scheduler_poll_sec)
            except TimeoutError:
                pass
        if await self.redis.get(self.leader_key) == self.leader_token:
            await self.redis.delete(self.leader_key)


async def async_main() -> None:
    engine = create_database_engine()
    factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    queue = RedisIngestionQueue(redis)
    scheduler = Scheduler(
        OperationsRepository(factory),
        CatalogRepository(factory),
        AuthRepository(factory),
        queue,
        redis,
    )
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, scheduler.stop_event.set)
    try:
        await scheduler.run()
    finally:
        with contextlib.suppress(Exception):
            await redis.aclose()
        engine.dispose()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
