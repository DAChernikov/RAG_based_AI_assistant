from __future__ import annotations

import asyncio
import contextlib
import signal
import uuid

from app.api.config import settings
from app.concurrency import BoundedThreadAdapter
from app.embeddings.client import EmbeddingServiceError
from app.indexing.contracts import IndexingJobContract
from app.indexing.redis_queue import RedisIndexingQueue
from app.indexing.repository import IndexRepository
from app.observability import log_event
from app.state.database import create_database_engine, create_session_factory
from app.worker_healthcheck import record_local_heartbeat


class IndexingWorker:
    def __init__(
        self, repository, queue, embeddings, worker_id: str | None = None, blocking_io=None
    ):
        self.repository = repository
        self.queue = queue
        self.embeddings = embeddings
        self.worker_id = worker_id or settings.indexing_worker_id
        self.stop_event = asyncio.Event()
        self.blocking_io = blocking_io or BoundedThreadAdapter(max_workers=4, max_pending=8)
        self._owns_blocking_io = blocking_io is None

    async def process(self, message_id: str, fields: dict) -> None:
        try:
            contract = IndexingJobContract.model_validate_json(fields.get("contract", ""))
        except Exception:
            await self.queue.dlq(message_id, None, "invalid_contract")
            await self.queue.ack(message_id)
            return
        claimed = await self.blocking_io.call(
            self.repository.claim,
            contract.run_id,
            self.worker_id,
            settings.indexing_lease_sec,
        )
        if claimed is None:
            if await self.blocking_io.call(self.repository.fail_exhausted, contract.run_id):
                await self.queue.dlq(message_id, contract, "retry_exhausted")
                await self.queue.ack(message_id)
            return
        run, token = claimed
        renew = asyncio.create_task(self._renew(run.id, token))
        try:
            after_id = (
                uuid.UUID(run.checkpoint["after_id"]) if run.checkpoint.get("after_id") else None
            )
            while not self.stop_event.is_set():
                rows = await self.blocking_io.call(
                    self.repository.chunks_for_run,
                    run.id,
                    after_id,
                    settings.embedding_batch_size,
                )
                if not rows:
                    break
                texts = [row[1].content for row in rows]
                if hasattr(self.embeddings, "embed_for_model"):
                    if run.model_definition_id is None:
                        raise RuntimeError("Indexing run has no pinned embedding model.")
                    vectors = await self.embeddings.embed_for_model(run.model_definition_id, texts)
                else:
                    vectors = await self.embeddings.embed(texts)
                await self.blocking_io.call(
                    self.repository.persist_batch, run.id, token, rows, vectors
                )
                after_id = rows[-1][0].id
            if self.stop_event.is_set():
                return
            if await self.blocking_io.call(self.repository.complete, run.id, token):
                if contract.auto_activate:
                    await self.blocking_io.call(
                        self.repository.activate,
                        contract.tenant_id,
                        contract.index_version_id,
                    )
                await self.queue.ack(message_id)
            else:
                current = await self.blocking_io.call(
                    self.repository.get_run, contract.tenant_id, run.id
                )
                if current is not None and current.status == "failed":
                    await self.queue.dlq(
                        message_id, contract, current.error_code or "validation_failed"
                    )
                    await self.queue.ack(message_id)
        except EmbeddingServiceError:
            if await self.blocking_io.call(
                self.repository.retry, run.id, token, "embedding_unavailable"
            ):
                await self.queue.enqueue(contract)
                await self.queue.ack(message_id)
            else:
                if await self.blocking_io.call(self.repository.fail_exhausted, run.id):
                    await self.queue.dlq(message_id, contract, "retry_exhausted")
                    await self.queue.ack(message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                "indexing_failed",
                run_id=run.id,
                correlation_id=contract.correlation_id,
                error_type=type(exc).__name__,
            )
            cancelled = await self.blocking_io.call(self.repository.finish_cancelled, run.id, token)
            if cancelled:
                await self.queue.ack(message_id)
            elif await self.blocking_io.call(
                self.repository.fail, run.id, token, "indexing_failed"
            ):
                await self.queue.dlq(message_id, contract, "indexing_failed")
                await self.queue.ack(message_id)
        finally:
            renew.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew

    async def _renew(self, run_id, token):
        while True:
            await asyncio.sleep(max(1, settings.indexing_lease_sec // 3))
            if not await self.blocking_io.call(
                self.repository.renew, run_id, token, settings.indexing_lease_sec
            ):
                return

    async def run(self) -> None:
        await self.queue.ensure_group()
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            while not self.stop_event.is_set():
                rows = await self.queue.claim_stale(self.worker_id)
                if not rows:
                    rows = await self.queue.read(self.worker_id)
                for message_id, fields in rows:
                    await self.process(message_id, fields)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            if self._owns_blocking_io:
                self.blocking_io.close()

    async def _heartbeat(self):
        while True:
            record_local_heartbeat("indexing")
            await self.queue.heartbeat(self.worker_id)
            await asyncio.sleep(10)


async def async_main() -> None:
    engine = create_database_engine()
    session_factory = create_session_factory(engine)
    repository = IndexRepository(session_factory)
    queue = RedisIndexingQueue()
    from app.operations.repository import OperationsRepository
    from app.operations.runtime_registry import RegistryEmbeddingGateway, RuntimeRegistry

    embeddings = RegistryEmbeddingGateway(RuntimeRegistry(OperationsRepository(session_factory)))
    worker = IndexingWorker(repository, queue, embeddings)
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, worker.stop_event.set)
    try:
        await worker.run()
    finally:
        await embeddings.close()
        await queue.close()
        engine.dispose()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
