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
from app.concurrency import BoundedThreadAdapter
from app.ingestion.contracts import IngestionEvent, IngestionJobContract
from app.ingestion.redis_queue import RedisIngestionQueue
from app.observability import log_event
from app.operations.repository import OperationsRepository
from app.state.auth_repository import AuthRepository
from app.state.database import create_database_engine, create_session_factory
from app.worker_healthcheck import record_local_heartbeat


class Scheduler:
    _RENEW_LEADER = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('EXPIRE', KEYS[1], ARGV[2])
    end
    return 0
    """
    _RELEASE_LEADER = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    def __init__(self, operations, catalog, audit, queue, redis, blocking_io=None):
        self.operations = operations
        self.catalog = catalog
        self.audit = audit
        self.queue = queue
        self.redis = redis
        self.stop_event = asyncio.Event()
        self.leader_key = "rag:scheduler:leader"
        self.leader_token = f"{settings.scheduler_id}:{uuid.uuid4()}"
        self.blocking_io = blocking_io or BoundedThreadAdapter(max_workers=4, max_pending=8)
        self._owns_blocking_io = blocking_io is None

    async def _leader(self) -> bool:
        acquired = await self.redis.set(
            self.leader_key,
            self.leader_token,
            nx=True,
            ex=settings.scheduler_leader_ttl_sec,
        )
        if acquired:
            return True
        renewed = await self.redis.eval(
            self._RENEW_LEADER,
            1,
            self.leader_key,
            self.leader_token,
            str(settings.scheduler_leader_ttl_sec),
        )
        return bool(renewed)

    async def tick(self) -> None:
        if not await self._leader():
            return
        due = await self.blocking_io.call(
            self.operations.claim_due_schedules,
            settings.scheduler_max_catchup,
            lease_seconds=settings.scheduler_leader_ttl_sec,
            max_attempts=settings.ingestion_max_attempts,
        )
        for attempt_id, attempt_token, schedule_id, tenant_id, source_id, scheduled_for in due:
            correlation_id = uuid.uuid4()
            request = {"source_id": str(source_id), "auto_activate": True}
            request_hash = hashlib.sha256(
                json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            idempotency_key = f"schedule:{schedule_id}:{scheduled_for.isoformat()}"
            try:
                if not await self._leader():
                    await self.blocking_io.call(
                        self.operations.fail_schedule_attempt,
                        attempt_id,
                        attempt_token,
                        "leader_lease_lost",
                    )
                    return
                run, version, _created = await self.blocking_io.call(
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
                completed = await self.blocking_io.call(
                    self.operations.complete_schedule_attempt,
                    attempt_id,
                    attempt_token,
                    run.id,
                )
                if not completed:
                    raise RuntimeError("Schedule attempt lease was lost.")
                await self.blocking_io.call(
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
                await self.blocking_io.call(
                    self.operations.fail_schedule_attempt,
                    attempt_id,
                    attempt_token,
                    "enqueue_failed",
                )
                log_event(
                    "scheduler_trigger_failed",
                    correlation_id=correlation_id,
                    error_type=type(exc).__name__,
                )

    async def run(self) -> None:
        await self.queue.ensure_group()
        while not self.stop_event.is_set():
            record_local_heartbeat("scheduler")
            await self.tick()
            try:
                await asyncio.wait_for(self.stop_event.wait(), settings.scheduler_poll_sec)
            except TimeoutError:
                pass
        await self.redis.eval(self._RELEASE_LEADER, 1, self.leader_key, self.leader_token)
        if self._owns_blocking_io:
            self.blocking_io.close()


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
