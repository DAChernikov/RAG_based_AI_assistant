from __future__ import annotations

import asyncio
import signal
import time
import uuid
from datetime import UTC, datetime

from pydantic import ValidationError

from app.api.config import settings
from app.api.services.llm_service import LLMRateLimitError, LLMTemporaryUnavailableError
from app.inference.contracts import (
    CompletedEvent,
    CompletedPayload,
    FailedEvent,
    FailedPayload,
    MetaEvent,
    MetaPayload,
    RetryingEvent,
    RetryingPayload,
    StartedEvent,
    StartedPayload,
    TokenEvent,
    TokenPayload,
    UnsupportedContractVersion,
    parse_job_contract,
)
from app.inference.redis_queue import RedisInferenceQueue
from app.observability import log_event, metric
from app.state.database import create_database_engine, create_session_factory
from app.state.models import JobStatus
from app.state.repositories import ApplicationRepository
from app.worker.processor import InferenceProcessor


class InferenceWorker:
    def __init__(self, repository, queue, processor, worker_id: str | None = None):
        self.repository = repository
        self.queue = queue
        self.processor = processor
        self.worker_id = worker_id or settings.worker_id
        self.stop_event = asyncio.Event()

    async def _event(self, contract, event_type: str, payload: dict) -> None:
        common = {
            "event_id": str(uuid.uuid4()),
            "sequence": await self.queue.next_event_sequence(contract.job_id),
            "job_id": contract.job_id,
            "correlation_id": contract.correlation_id,
        }
        event_classes = {
            "started": (StartedEvent, StartedPayload),
            "meta": (MetaEvent, MetaPayload),
            "token": (TokenEvent, TokenPayload),
            "completed": (CompletedEvent, CompletedPayload),
            "failed": (FailedEvent, FailedPayload),
            "retrying": (RetryingEvent, RetryingPayload),
        }
        event_class, payload_class = event_classes[event_type]
        await self.queue.publish_event(
            event_class(event_type=event_type, payload=payload_class(**payload), **common)
        )

    async def process_message(self, message_id: str, fields: dict) -> None:
        try:
            contract = parse_job_contract(fields.get("contract", ""))
        except (UnsupportedContractVersion, ValidationError) as exc:
            await self.queue.send_to_dlq(
                message_id=message_id,
                job_id=None,
                correlation_id=None,
                error_code="invalid_contract",
                message=str(exc),
            )
            await self.queue.ack(message_id)
            metric("dlq_count")
            return

        job = self.repository.get_job(contract.job_id)
        if job is None:
            await self.queue.send_to_dlq(
                message_id=message_id,
                job_id=str(contract.job_id),
                correlation_id=str(contract.correlation_id),
                error_code="job_not_found",
                message="Inference job does not exist in application state.",
            )
            await self.queue.ack(message_id)
            metric("dlq_count")
            return
        if job.status == JobStatus.COMPLETED.value:
            await self._event(
                contract,
                "completed",
                {"answer": job.answer.answer_text, "mode": job.answer.mode},
            )
            await self.queue.ack(message_id)
            return
        if job.status == JobStatus.FAILED.value:
            await self._event(
                contract,
                "failed",
                {
                    "error_code": job.error_code or "inference_failed",
                    "message": job.error_message or "Inference job failed.",
                },
            )
            await self.queue.send_to_dlq(
                message_id=message_id,
                job_id=str(contract.job_id),
                correlation_id=str(contract.correlation_id),
                error_code=job.error_code or "inference_failed",
                message=job.error_message or "Inference job failed.",
            )
            await self.queue.ack(message_id)
            return

        running = self.repository.mark_running(contract.job_id)
        await self._event(
            contract,
            "started",
            {"attempt": running.attempt_count, "worker_id": self.worker_id},
        )
        metric("jobs_running")
        started = time.monotonic()

        async def emit(event_type, payload):
            await self._event(contract, event_type, payload)

        try:
            result = await self.processor.execute(contract, emit)
            latency_ms = (time.monotonic() - started) * 1000
            self.repository.complete_job(
                contract.job_id,
                result=result,
                model_name=settings.generation_model,
                latency_ms=latency_ms,
            )
            await self._event(
                contract,
                "completed",
                {"answer": result.get("answer", ""), "mode": result.get("mode", "rag_docs")},
            )
            await self.queue.ack(message_id)
            metric("jobs_completed")
            log_event(
                "job_completed",
                job_id=contract.job_id,
                conversation_id=contract.conversation_id,
                correlation_id=contract.correlation_id,
                worker_id=self.worker_id,
                duration_ms=latency_ms,
                final_status="completed",
            )
        except (LLMTemporaryUnavailableError, LLMRateLimitError, TimeoutError):
            current = self.repository.get_job(contract.job_id)
            if current.attempt_count < current.max_attempts:
                code = "temporary_inference_error"
                self.repository.mark_retry(contract.job_id, code, "Temporary inference failure.")
                await self._event(
                    contract,
                    "retrying",
                    {
                        "attempt": current.attempt_count,
                        "max_attempts": current.max_attempts,
                        "error_code": code,
                    },
                )
                await asyncio.sleep(settings.inference_retry_backoff_sec * current.attempt_count)
                await self.queue.enqueue(contract)
                await self.queue.ack(message_id)
                metric("retries")
            else:
                await self._terminal_failure(message_id, contract, "retry_exhausted")
        except Exception:
            await self._terminal_failure(message_id, contract, "inference_failed")

    async def _terminal_failure(self, message_id, contract, code: str) -> None:
        message = "Inference job failed. See worker logs using the correlation ID."
        self.repository.mark_failed(contract.job_id, code, message)
        await self._event(
            contract,
            "failed",
            {"error_code": code, "message": message},
        )
        await self.queue.send_to_dlq(
            message_id=message_id,
            job_id=str(contract.job_id),
            correlation_id=str(contract.correlation_id),
            error_code=code,
            message=message,
        )
        await self.queue.ack(message_id)
        metric("jobs_failed")
        metric("dlq_count")

    async def _heartbeat_loop(self) -> None:
        started_at = datetime.now(UTC).isoformat()
        while not self.stop_event.is_set():
            try:
                readiness = await self.processor.readiness()
                await self.queue.heartbeat(
                    {
                        "worker_id": self.worker_id,
                        "started_at": started_at,
                        "last_seen_at": datetime.now(UTC).isoformat(),
                        "last_seen_unix": time.time(),
                        "job_contract_versions": ["1.0"],
                        "event_contract_versions": ["1.0"],
                        "retriever_ready": readiness["retriever_ready"],
                        "model_ready": readiness["model"]["ready"],
                        "model_status": readiness["model"]["status"],
                        "process_mode": "queued",
                        "model_name": settings.generation_model,
                    }
                )
            except Exception:
                log_event("heartbeat_failed", worker_id=self.worker_id)
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=max(1, settings.worker_heartbeat_ttl_sec // 3)
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
            await self.processor.close()
            await self.queue.close()


async def async_main() -> None:
    engine = create_database_engine()
    repository = ApplicationRepository(create_session_factory(engine))
    worker = InferenceWorker(repository, RedisInferenceQueue(), InferenceProcessor())
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, worker.stop_event.set)
    await worker.run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
