from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.state.models import (
    Answer,
    AnswerSource,
    Conversation,
    InferenceJob,
    JobStatus,
    Message,
    MessageRole,
    Tenant,
    User,
)


class IdempotencyConflictError(RuntimeError):
    pass


class IdentityNotSeededError(RuntimeError):
    pass


class ConversationAccessError(RuntimeError):
    pass


@dataclass
class JobCreationResult:
    job: InferenceJob
    created: bool


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ApplicationRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def get_compatibility_identity(self, tenant_slug: str, user_external_id: str) -> tuple:
        with self.session_factory() as session:
            statement = (
                select(Tenant, User)
                .join(User, User.tenant_id == Tenant.id)
                .where(Tenant.slug == tenant_slug, User.external_id == user_external_id)
            )
            row = session.execute(statement).first()
            if row is None:
                raise IdentityNotSeededError(
                    "Development identity is not initialized. Run the seed-dev command."
                )
            return row[0].id, row[1].id

    def create_job(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        question: str,
        request_payload: dict[str, Any],
        contract_version: str,
        max_attempts: int,
        conversation_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
    ) -> JobCreationResult:
        with self.session_factory.begin() as session:
            if idempotency_key:
                existing = session.scalar(
                    select(InferenceJob).where(
                        InferenceJob.tenant_id == tenant_id,
                        InferenceJob.operation == "create_inference_job",
                        InferenceJob.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if _canonical(existing.request_payload) != _canonical(request_payload):
                        raise IdempotencyConflictError(
                            "Idempotency-Key was already used with a different payload."
                        )
                    return JobCreationResult(existing, created=False)

            if conversation_id is None:
                conversation = Conversation(tenant_id=tenant_id, user_id=user_id)
                session.add(conversation)
                session.flush()
            else:
                conversation = session.get(Conversation, conversation_id)
                if (
                    conversation is None
                    or conversation.tenant_id != tenant_id
                    or conversation.user_id != user_id
                ):
                    raise ConversationAccessError("Conversation was not found.")

            current_sequence = session.scalar(
                select(func.max(Message.sequence_number)).where(
                    Message.conversation_id == conversation.id
                )
            )
            user_message = Message(
                conversation_id=conversation.id,
                role=MessageRole.USER.value,
                content=question,
                sequence_number=(current_sequence or 0) + 1,
            )
            session.add(user_message)
            session.flush()

            job = InferenceJob(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation.id,
                user_message_id=user_message.id,
                request_payload=request_payload,
                contract_version=contract_version,
                idempotency_key=idempotency_key,
                max_attempts=max_attempts,
                status=JobStatus.QUEUED.value,
            )
            session.add(job)
            session.flush()
            return JobCreationResult(job, created=True)

    def get_job(self, job_id: uuid.UUID) -> InferenceJob | None:
        with self.session_factory() as session:
            return session.scalar(
                select(InferenceJob)
                .options(selectinload(InferenceJob.answer).selectinload(Answer.sources))
                .where(InferenceJob.id == job_id)
            )

    def mark_running(self, job_id: uuid.UUID) -> InferenceJob | None:
        with self.session_factory.begin() as session:
            job = session.scalar(
                select(InferenceJob).where(InferenceJob.id == job_id).with_for_update()
            )
            if job is None or job.status == JobStatus.COMPLETED.value:
                return job
            job.status = JobStatus.RUNNING.value
            job.attempt_count += 1
            job.started_at = datetime.now(UTC)
            job.error_code = None
            job.error_message = None
            session.flush()
            return job

    def mark_retry(self, job_id: uuid.UUID, code: str, message: str) -> None:
        with self.session_factory.begin() as session:
            job = session.get(InferenceJob, job_id)
            if job is not None and job.status != JobStatus.COMPLETED.value:
                job.status = JobStatus.QUEUED.value
                job.error_code = code[:100]
                job.error_message = message[:500]

    def mark_failed(self, job_id: uuid.UUID, code: str, message: str) -> None:
        with self.session_factory.begin() as session:
            job = session.get(InferenceJob, job_id)
            if job is not None and job.status != JobStatus.COMPLETED.value:
                job.status = JobStatus.FAILED.value
                job.failed_at = datetime.now(UTC)
                job.error_code = code[:100]
                job.error_message = message[:500]

    def complete_job(
        self,
        job_id: uuid.UUID,
        *,
        result: dict[str, Any],
        model_name: str,
        latency_ms: float | None = None,
    ) -> Answer:
        with self.session_factory.begin() as session:
            job = session.scalar(
                select(InferenceJob)
                .options(selectinload(InferenceJob.answer).selectinload(Answer.sources))
                .where(InferenceJob.id == job_id)
                .with_for_update()
            )
            if job is None:
                raise LookupError("Inference job was not found.")
            if job.answer is not None:
                return job.answer

            current_sequence = session.scalar(
                select(func.max(Message.sequence_number)).where(
                    Message.conversation_id == job.conversation_id
                )
            )
            assistant_message = Message(
                conversation_id=job.conversation_id,
                role=MessageRole.ASSISTANT.value,
                content=result.get("answer", ""),
                sequence_number=(current_sequence or 0) + 1,
            )
            session.add(assistant_message)
            session.flush()

            answer = Answer(
                job_id=job.id,
                assistant_message_id=assistant_message.id,
                mode=result.get("mode", "rag_docs"),
                answer_text=result.get("answer", ""),
                confidence=result.get("confidence"),
                route_plan=result.get("route_plan"),
                model_name=model_name,
                prompt_version=result.get("prompt_version"),
                latency_ms=latency_ms,
                usage_metadata=result.get("usage"),
            )
            session.add(answer)
            session.flush()
            for rank, source in enumerate(result.get("retrieved", []), start=1):
                session.add(
                    AnswerSource(
                        answer_id=answer.id,
                        source_type=source.get("source", "unknown"),
                        source_id=source.get("doc_id", "unknown"),
                        title=source.get("title") or source.get("doc_id", "unknown"),
                        uri=source.get("uri"),
                        score=float(source.get("score", 0.0)),
                        rank=rank,
                        metadata_json=source.get("metadata") or {},
                    )
                )
            job.status = JobStatus.COMPLETED.value
            job.completed_at = datetime.now(UTC)
            job.error_code = None
            job.error_message = None
            session.flush()
            return answer

    def conversation_history(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            conversation = session.get(Conversation, conversation_id)
            if (
                conversation is None
                or conversation.tenant_id != tenant_id
                or conversation.user_id != user_id
            ):
                raise ConversationAccessError("Conversation was not found.")
            messages = session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.sequence_number)
            ).all()
            return {
                "conversation_id": str(conversation.id),
                "title": conversation.title,
                "messages": [
                    {
                        "id": str(message.id),
                        "role": message.role,
                        "content": message.content,
                        "sequence_number": message.sequence_number,
                        "created_at": message.created_at,
                    }
                    for message in messages
                ],
            }
