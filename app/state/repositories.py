from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
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
            tenant = session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
            if tenant is None:
                raise LookupError("Tenant was not found.")
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
                conversation = Conversation(
                    tenant_id=tenant_id, user_id=user_id, next_message_sequence=1
                )
                session.add(conversation)
                session.flush()
            else:
                conversation = session.scalar(
                    select(Conversation).where(Conversation.id == conversation_id).with_for_update()
                )
                if (
                    conversation is None
                    or conversation.tenant_id != tenant_id
                    or conversation.user_id != user_id
                ):
                    raise ConversationAccessError("Conversation was not found.")

            sequence_number = conversation.next_message_sequence
            conversation.next_message_sequence += 1
            user_message = Message(
                conversation_id=conversation.id,
                role=MessageRole.USER.value,
                content=question,
                sequence_number=sequence_number,
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

    def get_job_for_owner(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> InferenceJob | None:
        with self.session_factory() as session:
            return session.scalar(
                select(InferenceJob)
                .options(selectinload(InferenceJob.answer).selectinload(Answer.sources))
                .where(
                    InferenceJob.id == job_id,
                    InferenceJob.tenant_id == tenant_id,
                    InferenceJob.user_id == user_id,
                )
            )

    def mark_running(self, job_id: uuid.UUID) -> InferenceJob | None:
        claimed = self.claim_job(job_id, "legacy-worker", 300)
        return claimed[0] if claimed else self.get_job(job_id)

    def claim_job(
        self, job_id: uuid.UUID, worker_id: str, lease_seconds: int
    ) -> tuple[InferenceJob, uuid.UUID] | None:
        with self.session_factory.begin() as session:
            now = datetime.now(UTC)
            job = session.scalar(
                select(InferenceJob)
                .where(
                    InferenceJob.id == job_id,
                    or_(
                        InferenceJob.status == JobStatus.QUEUED.value,
                        (
                            (InferenceJob.status == JobStatus.RUNNING.value)
                            & (InferenceJob.lease_expires_at < now)
                        ),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            lease_token = uuid.uuid4()
            job.status = JobStatus.RUNNING.value
            job.attempt_count += 1
            job.started_at = now
            job.lease_owner = worker_id
            job.lease_token = lease_token
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.error_code = None
            job.error_message = None
            session.flush()
            return job, lease_token

    def renew_job_lease(
        self, job_id: uuid.UUID, lease_token: uuid.UUID, lease_seconds: int
    ) -> bool:
        with self.session_factory.begin() as session:
            job = session.scalar(
                select(InferenceJob)
                .where(
                    InferenceJob.id == job_id,
                    InferenceJob.status == JobStatus.RUNNING.value,
                    InferenceJob.lease_token == lease_token,
                )
                .with_for_update()
            )
            if job is None:
                return False
            job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
            return True

    def mark_retry(
        self,
        job_id: uuid.UUID,
        code: str,
        message: str,
        lease_token: uuid.UUID | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            job = session.get(InferenceJob, job_id)
            if (
                job is not None
                and job.status != JobStatus.COMPLETED.value
                and (lease_token is None or job.lease_token == lease_token)
            ):
                job.status = JobStatus.QUEUED.value
                job.error_code = code[:100]
                job.error_message = message[:500]
                job.lease_owner = None
                job.lease_token = None
                job.lease_expires_at = None

    def mark_failed(
        self,
        job_id: uuid.UUID,
        code: str,
        message: str,
        lease_token: uuid.UUID | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            job = session.get(InferenceJob, job_id)
            if (
                job is not None
                and job.status != JobStatus.COMPLETED.value
                and (lease_token is None or job.lease_token == lease_token)
            ):
                job.status = JobStatus.FAILED.value
                job.failed_at = datetime.now(UTC)
                job.error_code = code[:100]
                job.error_message = message[:500]
                job.lease_owner = None
                job.lease_token = None
                job.lease_expires_at = None

    def complete_job(
        self,
        job_id: uuid.UUID,
        *,
        result: dict[str, Any],
        model_name: str,
        latency_ms: float | None = None,
        lease_token: uuid.UUID | None = None,
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
            if lease_token is not None and job.lease_token != lease_token:
                raise RuntimeError("Inference job lease is no longer owned by this worker.")

            conversation = session.scalar(
                select(Conversation).where(Conversation.id == job.conversation_id).with_for_update()
            )
            sequence_number = conversation.next_message_sequence
            conversation.next_message_sequence += 1
            assistant_message = Message(
                conversation_id=job.conversation_id,
                role=MessageRole.ASSISTANT.value,
                content=result.get("answer", ""),
                sequence_number=sequence_number,
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
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
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


class AsyncApplicationRepository:
    """Non-blocking API adapter around the transactional sync repository."""

    def __init__(self, repository: ApplicationRepository):
        self.repository = repository

    async def get_compatibility_identity(self, tenant_slug: str, user_external_id: str):
        return await asyncio.to_thread(
            self.repository.get_compatibility_identity, tenant_slug, user_external_id
        )

    async def create_job(self, **kwargs):
        return await asyncio.to_thread(self.repository.create_job, **kwargs)

    async def get_job(self, job_id: uuid.UUID):
        return await asyncio.to_thread(self.repository.get_job, job_id)

    async def get_job_for_owner(self, job_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID):
        return await asyncio.to_thread(
            self.repository.get_job_for_owner, job_id, tenant_id, user_id
        )

    async def conversation_history(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID
    ):
        return await asyncio.to_thread(
            self.repository.conversation_history, conversation_id, tenant_id, user_id
        )
