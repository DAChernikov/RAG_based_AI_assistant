from __future__ import annotations

import asyncio
import contextlib
import signal

from pydantic import ValidationError

from app.api.config import settings
from app.catalog.repository import CatalogRepository, LeaseLostError
from app.connectors.base import EnvironmentCredentialResolver, TransientConnectorError
from app.connectors.git import GitConnector
from app.connectors.website import WebsiteConnector
from app.ingestion.contracts import IngestionEvent, IngestionJobContract
from app.ingestion.pipeline import IngestionCancelled, IngestionPipeline
from app.ingestion.redis_queue import RedisIngestionQueue
from app.state.database import create_database_engine, create_session_factory
from app.state.models import IngestionRunStatus


class IngestionWorker:
    def __init__(self, repository, queue, pipeline, worker_id: str | None = None):
        self.repository = repository
        self.queue = queue
        self.pipeline = pipeline
        self.worker_id = worker_id or settings.ingestion_worker_id
        self.stop_event = asyncio.Event()

    async def _event(self, contract, event_type: str, stage: str | None = None) -> None:
        await self.queue.publish_event(
            IngestionEvent(run_id=contract.run_id, event_type=event_type, stage=stage)
        )

    async def process_message(self, message_id: str, fields: dict) -> None:
        try:
            contract = IngestionJobContract.model_validate_json(fields.get("contract", ""))
        except ValidationError:
            await self.queue.send_to_dlq(
                message_id, None, "invalid_contract", "Invalid ingestion job contract."
            )
            await self.queue.ack(message_id)
            return
        run = await asyncio.to_thread(
            self.repository.get_ingestion_run, contract.tenant_id, contract.run_id
        )
        if run is None:
            await self.queue.send_to_dlq(
                message_id, contract, "run_not_found", "Ingestion run was not found."
            )
            await self.queue.ack(message_id)
            return
        if run.status in {
            IngestionRunStatus.COMPLETED.value,
            IngestionRunStatus.FAILED.value,
            IngestionRunStatus.CANCELLED.value,
        }:
            await self.queue.ack(message_id)
            return
        claimed = await asyncio.to_thread(
            self.repository.claim_ingestion_run,
            contract.run_id,
            self.worker_id,
            settings.ingestion_lease_sec,
        )
        if claimed is None:
            exhausted = await asyncio.to_thread(
                self.repository.fail_exhausted_ingestion,
                contract.tenant_id,
                contract.source_id,
                contract.source_version_id,
                contract.run_id,
            )
            if exhausted:
                await self._event(contract, "failed", "failed")
                await self.queue.send_to_dlq(
                    message_id,
                    contract,
                    "retry_exhausted",
                    "Ingestion failed. See logs using the correlation ID.",
                )
                await self.queue.ack(message_id)
            return
        running, lease_token = claimed
        await self._event(contract, "started", "discover")
        lease_task = asyncio.create_task(self._renew_lease(contract.run_id, lease_token))

        async def emit(event_type, stage):
            await self._event(contract, event_type, stage)

        try:
            await self.pipeline.execute(contract, emit, lease_token=lease_token)
            completed = await asyncio.to_thread(
                self.repository.complete_ingestion_job, contract.run_id, lease_token
            )
            if completed:
                await self._event(contract, "completed", "complete")
                await self.queue.ack(message_id)
        except IngestionCancelled:
            cancelled = await asyncio.to_thread(
                self.repository.cancel_ingestion_job, contract.run_id, lease_token
            )
            if cancelled:
                await self._event(contract, "cancelled", "cancelled")
                await self.queue.ack(message_id)
        except TransientConnectorError:
            retried = await asyncio.to_thread(
                self.repository.retry_ingestion_job,
                contract.run_id,
                lease_token,
                "temporary_connector_error",
            )
            if retried:
                await self._event(contract, "retrying", "discover")
                await self.queue.enqueue(contract)
                await self.queue.ack(message_id)
            else:
                await self._fail(message_id, contract, lease_token, "retry_exhausted")
        except LeaseLostError:
            return
        except Exception:
            await self._fail(message_id, contract, lease_token, "ingestion_failed")
        finally:
            lease_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lease_task

    async def _fail(self, message_id, contract, lease_token, code: str) -> None:
        message = "Ingestion failed. See logs using the correlation ID."
        failed = await asyncio.to_thread(
            self.repository.fail_ingestion,
            contract.tenant_id,
            contract.source_id,
            contract.source_version_id,
            contract.run_id,
            code,
            message,
            lease_token,
        )
        if not failed:
            return
        await self._event(contract, "failed", "failed")
        await self.queue.send_to_dlq(message_id, contract, code, message)
        await self.queue.ack(message_id)

    async def _renew_lease(self, run_id, lease_token) -> None:
        interval = max(1, settings.ingestion_lease_sec // 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                self.repository.renew_ingestion_lease,
                run_id,
                lease_token,
                settings.ingestion_lease_sec,
            )
            if not renewed:
                return

    async def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            with contextlib.suppress(Exception):
                await self.queue.heartbeat(self.worker_id)
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=max(1, settings.worker_heartbeat_ttl_sec // 3),
                )
            except TimeoutError:
                pass

    async def run(self) -> None:
        await self.queue.ensure_group()
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self.stop_event.is_set():
                messages = await self.queue.claim_stale(self.worker_id)
                if not messages:
                    messages = await self.queue.read_jobs(self.worker_id)
                for message_id, fields in messages:
                    await self.process_message(message_id, fields)
        finally:
            self.stop_event.set()
            await heartbeat


async def async_main() -> None:
    engine = create_database_engine()
    repository = CatalogRepository(create_session_factory(engine))
    resolver = EnvironmentCredentialResolver()
    pipeline = IngestionPipeline(
        repository,
        WebsiteConnector(resolver),
        GitConnector(resolver),
    )
    queue = RedisIngestionQueue()
    worker = IngestionWorker(repository, queue, pipeline)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, worker.stop_event.set)
    try:
        await worker.run()
    finally:
        await queue.close()
        engine.dispose()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
