from __future__ import annotations

import asyncio
import inspect
import uuid
from typing import Any

from app.api.config import settings
from app.inference.contracts import InferenceJobContract, QueuedEvent, QueuedPayload
from app.inference.redis_queue import RedisInferenceQueue
from app.observability import metric
from app.state.models import JobStatus
from app.state.repositories import JobCreationResult


async def _resolve(value):
    return await value if inspect.isawaitable(value) else value


def serialize_job(job) -> dict[str, Any]:
    answer = None
    sources: list[dict] = []
    if job.answer is not None:
        answer = {
            "answer": job.answer.answer_text,
            "mode": job.answer.mode,
            "confidence": job.answer.confidence,
            "model_name": job.answer.model_name,
        }
        sources = [
            {
                "doc_id": source.source_id,
                "source": source.source_type,
                "title": source.title,
                "uri": source.uri,
                "score": source.score,
                "rank": source.rank,
            }
            for source in job.answer.sources
        ]
        answer["retrieved"] = sources
    return {
        "job_id": str(job.id),
        "status": job.status,
        "conversation_id": str(job.conversation_id),
        "contract_version": job.contract_version,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "queued_at": job.queued_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "failed_at": job.failed_at,
        "error": (
            {"code": job.error_code, "message": job.error_message} if job.error_code else None
        ),
        "answer": answer,
        "sources": sources,
    }


class QueuedInferenceApplication:
    def __init__(self, repository, queue: RedisInferenceQueue):
        self.repository = repository
        self.queue = queue

    async def submit(
        self,
        payload: dict[str, Any],
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        idempotency_key: str | None,
        conversation_id: uuid.UUID | None,
    ) -> tuple[JobCreationResult, InferenceJobContract]:
        request_payload = {
            "question": payload["question"],
            "mode": payload.get("mode"),
            "top_k": payload.get("top_k"),
            "max_new_tokens": payload.get("max_new_tokens"),
            "conversation_id": str(conversation_id) if conversation_id else None,
        }
        creation = await _resolve(
            self.repository.create_job(
                tenant_id=tenant_id,
                user_id=user_id,
                question=payload["question"],
                request_payload=request_payload,
                contract_version="1.0",
                max_attempts=settings.inference_max_attempts,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
            )
        )
        contract = InferenceJobContract(
            job_id=creation.job.id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=creation.job.conversation_id,
            message_id=creation.job.user_message_id,
            question=payload["question"],
            requested_mode=payload.get("mode"),
            top_k=payload.get("top_k"),
            max_new_tokens=payload.get("max_new_tokens"),
        )
        if creation.created or creation.job.status == JobStatus.QUEUED.value:
            await self.queue.enqueue(contract)
            metric("jobs_queued")
        if creation.created:
            await self.queue.publish_event(
                QueuedEvent(
                    event_id=str(uuid.uuid4()),
                    sequence=await self.queue.next_event_sequence(contract.job_id),
                    job_id=contract.job_id,
                    correlation_id=contract.correlation_id,
                    event_type="queued",
                    payload=QueuedPayload(),
                )
            )
        return creation, contract

    async def wait_for_terminal(self, job_id: uuid.UUID):
        deadline = asyncio.get_running_loop().time() + settings.inference_wait_timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            job = await _resolve(self.repository.get_job(job_id))
            if job is not None and job.status in {
                JobStatus.COMPLETED.value,
                JobStatus.FAILED.value,
            }:
                return job
            await asyncio.sleep(settings.inference_poll_interval_sec)
        return None
