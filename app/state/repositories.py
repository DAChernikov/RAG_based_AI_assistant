from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.concurrency import api_blocking_io
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


class ConversationBusyError(RuntimeError):
    pass


class JobCancelledError(RuntimeError):
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
                    tenant_id=tenant_id,
                    user_id=user_id,
                    title=question[:255],
                    next_message_sequence=1,
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

    def claim_job(
        self, job_id: uuid.UUID, worker_id: str, lease_seconds: int
    ) -> tuple[InferenceJob, uuid.UUID] | None:
        with self.session_factory.begin() as session:
            now = datetime.now(UTC)
            job = session.scalar(
                select(InferenceJob)
                .where(
                    InferenceJob.id == job_id,
                    InferenceJob.cancel_requested.is_(False),
                    InferenceJob.attempt_count < InferenceJob.max_attempts,
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

    def fail_exhausted_job(self, job_id: uuid.UUID) -> bool:
        """Atomically terminalize a queued/stale job whose attempt budget is exhausted."""
        with self.session_factory.begin() as session:
            now = datetime.now(UTC)
            job = session.scalar(
                select(InferenceJob)
                .where(
                    InferenceJob.id == job_id,
                    InferenceJob.attempt_count >= InferenceJob.max_attempts,
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
                return False
            job.status = JobStatus.FAILED.value
            job.failed_at = now
            job.error_code = "retry_exhausted"
            job.error_message = "Inference retry budget was exhausted."
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            return True

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
                    InferenceJob.cancel_requested.is_(False),
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
    ) -> bool:
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
                return True
            return False

    def mark_failed(
        self,
        job_id: uuid.UUID,
        code: str,
        message: str,
        lease_token: uuid.UUID | None = None,
    ) -> bool:
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
                return True
            return False

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
            if lease_token is not None and job.lease_token != lease_token:
                raise RuntimeError("Inference job lease is no longer owned by this worker.")
            if job.cancel_requested:
                raise JobCancelledError("Inference job was cancelled.")
            if job.answer is not None:
                return job.answer

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
                model_definition_id=result.get("model_definition_id"),
                prompt_template_id=result.get("prompt_template_id"),
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
                "pinned": conversation.pinned,
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

    def list_conversations(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.user_id == user_id,
                )
                .order_by(Conversation.pinned.desc(), Conversation.updated_at.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            return [
                {
                    "conversation_id": str(row.id),
                    "title": row.title,
                    "pinned": row.pinned,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
                for row in rows
            ]

    def update_conversation(
        self,
        conversation_id: uuid.UUID,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        with self.session_factory.begin() as session:
            conversation = session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
            )
            if (
                conversation is None
                or conversation.tenant_id != tenant_id
                or conversation.user_id != user_id
            ):
                raise ConversationAccessError("Conversation was not found.")
            if title is not None:
                conversation.title = title
            if pinned is not None:
                conversation.pinned = pinned
            session.flush()
            return {
                "conversation_id": str(conversation.id),
                "title": conversation.title,
                "pinned": conversation.pinned,
                "created_at": conversation.created_at,
                "updated_at": conversation.updated_at,
            }

    def delete_conversation(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        with self.session_factory.begin() as session:
            conversation = session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
            )
            if (
                conversation is None
                or conversation.tenant_id != tenant_id
                or conversation.user_id != user_id
            ):
                raise ConversationAccessError("Conversation was not found.")
            active_job = session.scalar(
                select(InferenceJob.id).where(
                    InferenceJob.conversation_id == conversation_id,
                    InferenceJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
                )
            )
            if active_job is not None:
                raise ConversationBusyError(
                    "A conversation with an active request cannot be deleted."
                )
            session.delete(conversation)

    def request_cancel(self, job_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            job = session.scalar(
                select(InferenceJob)
                .where(
                    InferenceJob.id == job_id,
                    InferenceJob.tenant_id == tenant_id,
                    InferenceJob.user_id == user_id,
                )
                .with_for_update()
            )
            if job is None:
                raise ConversationAccessError("Inference job was not found.")
            if job.status not in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}:
                return False
            job.cancel_requested = True
            if job.status == JobStatus.QUEUED.value:
                job.status = JobStatus.CANCELLED.value
                job.completed_at = datetime.now(UTC)
            return True

    def mark_cancelled(self, job_id: uuid.UUID, lease_token: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            job = session.scalar(
                select(InferenceJob)
                .where(
                    InferenceJob.id == job_id,
                    InferenceJob.lease_token == lease_token,
                    InferenceJob.cancel_requested.is_(True),
                )
                .with_for_update()
            )
            if job is None:
                return False
            job.status = JobStatus.CANCELLED.value
            job.completed_at = datetime.now(UTC)
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            return True

    def create_feedback(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        answer_id: uuid.UUID,
        rating: int,
        comment: str | None,
    ):
        from app.state.models import AnswerFeedback

        with self.session_factory.begin() as session:
            owned = session.scalar(
                select(Answer.id)
                .join(InferenceJob, InferenceJob.id == Answer.job_id)
                .where(
                    Answer.id == answer_id,
                    InferenceJob.tenant_id == tenant_id,
                    InferenceJob.user_id == user_id,
                )
            )
            if owned is None:
                raise ConversationAccessError("Answer was not found.")
            row = session.scalar(
                select(AnswerFeedback).where(
                    AnswerFeedback.answer_id == answer_id,
                    AnswerFeedback.user_id == user_id,
                )
            )
            if row is None:
                row = AnswerFeedback(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    answer_id=answer_id,
                    rating=rating,
                    comment=comment,
                )
                session.add(row)
            else:
                row.rating = rating
                row.comment = comment
            session.flush()
            return row


class AsyncApplicationRepository:
    """Non-blocking API adapter around the transactional sync repository."""

    def __init__(self, repository: ApplicationRepository):
        self.repository = repository

    async def get_compatibility_identity(self, tenant_slug: str, user_external_id: str):
        return await api_blocking_io.call(
            self.repository.get_compatibility_identity, tenant_slug, user_external_id
        )

    async def create_job(self, **kwargs):
        return await api_blocking_io.call(self.repository.create_job, **kwargs)

    async def get_job(self, job_id: uuid.UUID):
        return await api_blocking_io.call(self.repository.get_job, job_id)

    async def get_job_for_owner(self, job_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID):
        return await api_blocking_io.call(
            self.repository.get_job_for_owner, job_id, tenant_id, user_id
        )

    async def conversation_history(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID
    ):
        return await api_blocking_io.call(
            self.repository.conversation_history, conversation_id, tenant_id, user_id
        )

    async def list_conversations(self, tenant_id, user_id, offset, limit):
        return await api_blocking_io.call(
            self.repository.list_conversations, tenant_id, user_id, offset, limit
        )

    async def update_conversation(self, conversation_id, tenant_id, user_id, **changes):
        return await api_blocking_io.call(
            self.repository.update_conversation,
            conversation_id,
            tenant_id,
            user_id,
            **changes,
        )

    async def delete_conversation(self, conversation_id, tenant_id, user_id):
        return await api_blocking_io.call(
            self.repository.delete_conversation, conversation_id, tenant_id, user_id
        )

    async def request_cancel(self, job_id, tenant_id, user_id):
        return await api_blocking_io.call(
            self.repository.request_cancel, job_id, tenant_id, user_id
        )

    async def create_feedback(self, tenant_id, user_id, answer_id, rating, comment):
        return await api_blocking_io.call(
            self.repository.create_feedback,
            tenant_id,
            user_id,
            answer_id,
            rating,
            comment,
        )
