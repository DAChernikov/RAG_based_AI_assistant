from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.state.models import (
    AuditEvent,
    ChunkEmbedding,
    ContentBlob,
    DocumentChunk,
    IndexingEvent,
    IndexingRun,
    IndexingRunStatus,
    IndexVersionStatus,
    InferenceJob,
    IngestionRun,
    IngestionRunStatus,
    JobStatus,
    KnowledgeIndexEntry,
    KnowledgeIndexVersion,
    KnowledgeSource,
    ModelDefinition,
    NormalizedDocument,
    PromptTemplate,
    RetentionPolicy,
    ScheduleAttempt,
    ScheduleAttemptStatus,
    SourceSchedule,
    SourceVersion,
    SourceVersionStatus,
    TelegramConfiguration,
)


def checksum(value: dict | str) -> str:
    payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class OperationsRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def get_telegram_configuration(self, tenant_id: uuid.UUID):
        with self.session_factory() as session:
            return session.get(TelegramConfiguration, tenant_id)

    def list_enabled_telegram_configurations(self):
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(TelegramConfiguration).where(TelegramConfiguration.is_enabled.is_(True))
                )
            )

    def upsert_telegram_configuration(
        self,
        tenant_id: uuid.UUID,
        *,
        is_enabled: bool,
        token_credential_ref: str | None,
        api_key_credential_ref: str | None,
    ):
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(TelegramConfiguration)
                .where(TelegramConfiguration.tenant_id == tenant_id)
                .with_for_update()
            )
            if row is None:
                row = TelegramConfiguration(tenant_id=tenant_id, config_version=1)
                session.add(row)
            else:
                row.config_version += 1
            row.is_enabled = is_enabled
            row.token_credential_ref = token_credential_ref
            row.api_key_credential_ref = api_key_credential_ref
            session.flush()
            return row

    def record_telegram_test(self, tenant_id: uuid.UUID, status: str):
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(TelegramConfiguration)
                .where(TelegramConfiguration.tenant_id == tenant_id)
                .with_for_update()
            )
            if row is None:
                raise LookupError("Telegram is not configured.")
            row.last_test_status = status
            row.last_tested_at = datetime.now(UTC)
            return row

    def list_schedules(self, tenant_id: uuid.UUID, offset: int, limit: int):
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(SourceSchedule)
                    .where(SourceSchedule.tenant_id == tenant_id)
                    .order_by(SourceSchedule.created_at)
                    .offset(offset)
                    .limit(limit)
                )
            )

    def upsert_schedule(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        interval_seconds: int,
        is_enabled: bool,
    ):
        with self.session_factory.begin() as session:
            source = session.scalar(
                select(KnowledgeSource).where(
                    KnowledgeSource.id == source_id, KnowledgeSource.tenant_id == tenant_id
                )
            )
            if source is None:
                raise LookupError("Knowledge source was not found.")
            row = session.scalar(
                select(SourceSchedule)
                .where(
                    SourceSchedule.tenant_id == tenant_id,
                    SourceSchedule.source_id == source_id,
                )
                .with_for_update()
            )
            if row is None:
                row = SourceSchedule(
                    tenant_id=tenant_id,
                    source_id=source_id,
                    interval_seconds=interval_seconds,
                    is_enabled=is_enabled,
                    next_run_at=datetime.now(UTC) + timedelta(seconds=interval_seconds),
                )
                session.add(row)
            else:
                row.interval_seconds = interval_seconds
                row.is_enabled = is_enabled
                row.next_run_at = datetime.now(UTC) + timedelta(seconds=interval_seconds)
            session.flush()
            return row

    def delete_schedule(self, tenant_id: uuid.UUID, schedule_id: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(SourceSchedule).where(
                    SourceSchedule.id == schedule_id,
                    SourceSchedule.tenant_id == tenant_id,
                )
            )
            if row is None:
                return False
            session.delete(row)
            return True

    def claim_due_schedules(
        self, limit: int = 20, *, lease_seconds: int = 30, max_attempts: int = 3
    ):
        with self.session_factory.begin() as session:
            now = datetime.now(UTC)
            attempts = list(
                session.scalars(
                    select(ScheduleAttempt)
                    .where(
                        ScheduleAttempt.status == ScheduleAttemptStatus.PENDING.value,
                        ScheduleAttempt.next_attempt_at <= now,
                        or_(
                            ScheduleAttempt.lease_expires_at.is_(None),
                            ScheduleAttempt.lease_expires_at < now,
                        ),
                    )
                    .order_by(ScheduleAttempt.next_attempt_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            remaining = max(0, limit - len(attempts))
            rows = (
                list(
                    session.scalars(
                        select(SourceSchedule)
                        .where(
                            SourceSchedule.is_enabled.is_(True), SourceSchedule.next_run_at <= now
                        )
                        .order_by(SourceSchedule.next_run_at)
                        .limit(remaining)
                        .with_for_update(skip_locked=True)
                    )
                )
                if remaining
                else []
            )
            for row in rows:
                scheduled_for = (
                    row.next_run_at.replace(tzinfo=UTC)
                    if row.next_run_at.tzinfo is None
                    else row.next_run_at
                )
                existing = session.scalar(
                    select(ScheduleAttempt).where(
                        ScheduleAttempt.schedule_id == row.id,
                        ScheduleAttempt.scheduled_for == scheduled_for,
                    )
                )
                if existing is None:
                    existing = ScheduleAttempt(
                        tenant_id=row.tenant_id,
                        schedule_id=row.id,
                        source_id=row.source_id,
                        scheduled_for=scheduled_for,
                        next_attempt_at=now,
                        max_attempts=max_attempts,
                    )
                    session.add(existing)
                    session.flush()
                if existing.status == ScheduleAttemptStatus.PENDING.value and all(
                    item.id != existing.id for item in attempts
                ):
                    attempts.append(existing)
            claimed = []
            for attempt in attempts[:limit]:
                token = uuid.uuid4()
                attempt.attempt_count += 1
                attempt.lease_token = token
                attempt.lease_expires_at = now + timedelta(seconds=lease_seconds)
                claimed.append(
                    (
                        attempt.id,
                        token,
                        attempt.schedule_id,
                        attempt.tenant_id,
                        attempt.source_id,
                        attempt.scheduled_for,
                    )
                )
            return claimed

    def complete_schedule_attempt(
        self, attempt_id: uuid.UUID, token: uuid.UUID, ingestion_run_id: uuid.UUID
    ) -> bool:
        with self.session_factory.begin() as session:
            attempt = session.scalar(
                select(ScheduleAttempt)
                .where(
                    ScheduleAttempt.id == attempt_id,
                    ScheduleAttempt.lease_token == token,
                    ScheduleAttempt.status == ScheduleAttemptStatus.PENDING.value,
                )
                .with_for_update()
            )
            if attempt is None:
                return False
            schedule = session.scalar(
                select(SourceSchedule)
                .where(SourceSchedule.id == attempt.schedule_id)
                .with_for_update()
            )
            if schedule is None:
                return False
            now = datetime.now(UTC)
            scheduled_for = attempt.scheduled_for
            if scheduled_for.tzinfo is None:
                scheduled_for = scheduled_for.replace(tzinfo=UTC)
            schedule.last_run_at = now
            schedule.next_run_at = max(scheduled_for, now) + timedelta(
                seconds=schedule.interval_seconds
            )
            attempt.status = ScheduleAttemptStatus.ENQUEUED.value
            attempt.ingestion_run_id = ingestion_run_id
            attempt.completed_at = now
            attempt.lease_token = None
            attempt.lease_expires_at = None
            return True

    def fail_schedule_attempt(
        self, attempt_id: uuid.UUID, token: uuid.UUID, error_code: str
    ) -> bool:
        with self.session_factory.begin() as session:
            attempt = session.scalar(
                select(ScheduleAttempt)
                .where(
                    ScheduleAttempt.id == attempt_id,
                    ScheduleAttempt.lease_token == token,
                    ScheduleAttempt.status == ScheduleAttemptStatus.PENDING.value,
                )
                .with_for_update()
            )
            if attempt is None:
                return False
            now = datetime.now(UTC)
            attempt.error_code = error_code[:100]
            attempt.lease_token = None
            attempt.lease_expires_at = None
            if attempt.attempt_count >= attempt.max_attempts:
                attempt.status = ScheduleAttemptStatus.FAILED.value
                attempt.completed_at = now
                schedule = session.scalar(
                    select(SourceSchedule)
                    .where(SourceSchedule.id == attempt.schedule_id)
                    .with_for_update()
                )
                if schedule is not None:
                    scheduled_for = attempt.scheduled_for
                    if scheduled_for.tzinfo is None:
                        scheduled_for = scheduled_for.replace(tzinfo=UTC)
                    schedule.next_run_at = max(scheduled_for, now) + timedelta(
                        seconds=schedule.interval_seconds
                    )
            else:
                attempt.next_attempt_at = now + timedelta(
                    seconds=min(300, 2**attempt.attempt_count)
                )
            return True

    def get_retention_policy(self, tenant_id: uuid.UUID):
        with self.session_factory.begin() as session:
            row = session.get(RetentionPolicy, tenant_id)
            if row is None:
                row = RetentionPolicy(tenant_id=tenant_id)
                session.add(row)
                session.flush()
            return row

    def update_retention_policy(self, tenant_id: uuid.UUID, values: dict):
        with self.session_factory.begin() as session:
            row = session.get(RetentionPolicy, tenant_id)
            if row is None:
                row = RetentionPolicy(tenant_id=tenant_id)
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            session.flush()
            return row

    def retention_candidates(self, tenant_id: uuid.UUID, limit: int = 500):
        policy = self.get_retention_policy(tenant_id)
        now = datetime.now(UTC)
        with self.session_factory() as session:
            source_ids = list(
                session.scalars(
                    select(SourceVersion.id)
                    .where(
                        SourceVersion.tenant_id == tenant_id,
                        SourceVersion.pinned.is_(False),
                        SourceVersion.status.in_(
                            [SourceVersionStatus.SUPERSEDED.value, SourceVersionStatus.FAILED.value]
                        ),
                        SourceVersion.updated_at
                        < now - timedelta(days=policy.source_versions_days),
                    )
                    .limit(limit)
                )
            )
            index_ids = list(
                session.scalars(
                    select(KnowledgeIndexVersion.id)
                    .where(
                        KnowledgeIndexVersion.tenant_id == tenant_id,
                        KnowledgeIndexVersion.pinned.is_(False),
                        KnowledgeIndexVersion.status.in_(
                            [IndexVersionStatus.SUPERSEDED.value, IndexVersionStatus.FAILED.value]
                        ),
                        KnowledgeIndexVersion.created_at
                        < now - timedelta(days=policy.index_versions_days),
                    )
                    .limit(limit)
                )
            )
            run_cutoff = now - timedelta(days=policy.run_history_days)
            ingestion_run_ids = list(
                session.scalars(
                    select(IngestionRun.id)
                    .where(
                        IngestionRun.tenant_id == tenant_id,
                        IngestionRun.status.in_(
                            [
                                IngestionRunStatus.COMPLETED.value,
                                IngestionRunStatus.FAILED.value,
                                IngestionRunStatus.CANCELLED.value,
                            ]
                        ),
                        IngestionRun.created_at < run_cutoff,
                    )
                    .limit(limit)
                )
            )
            indexing_run_ids = list(
                session.scalars(
                    select(IndexingRun.id)
                    .where(
                        IndexingRun.tenant_id == tenant_id,
                        IndexingRun.status.in_(
                            [
                                IndexingRunStatus.COMPLETED.value,
                                IndexingRunStatus.FAILED.value,
                                IndexingRunStatus.CANCELLED.value,
                            ]
                        ),
                        IndexingRun.created_at < run_cutoff,
                    )
                    .limit(limit)
                )
            )
            inference_job_ids = list(
                session.scalars(
                    select(InferenceJob.id)
                    .where(
                        InferenceJob.tenant_id == tenant_id,
                        InferenceJob.status.in_(
                            [
                                JobStatus.COMPLETED.value,
                                JobStatus.FAILED.value,
                                JobStatus.CANCELLED.value,
                            ]
                        ),
                        InferenceJob.created_at < run_cutoff,
                    )
                    .limit(limit)
                )
            )
            indexing_event_ids = list(
                session.scalars(
                    select(IndexingEvent.id)
                    .join(IndexingRun, IndexingRun.id == IndexingEvent.run_id)
                    .where(
                        IndexingEvent.tenant_id == tenant_id,
                        IndexingEvent.created_at < run_cutoff,
                        IndexingRun.status.in_(
                            [
                                IndexingRunStatus.COMPLETED.value,
                                IndexingRunStatus.FAILED.value,
                                IndexingRunStatus.CANCELLED.value,
                            ]
                        ),
                    )
                    .limit(limit)
                )
            )
            audit_ids = list(
                session.scalars(
                    select(AuditEvent.id)
                    .where(
                        AuditEvent.tenant_id == tenant_id,
                        AuditEvent.created_at < now - timedelta(days=policy.audit_days),
                    )
                    .limit(limit)
                )
            )
            orphan_embedding_ids = list(
                session.scalars(
                    select(ChunkEmbedding.id)
                    .where(
                        ChunkEmbedding.tenant_id == tenant_id,
                        ~exists().where(
                            KnowledgeIndexEntry.chunk_embedding_id == ChunkEmbedding.id
                        ),
                    )
                    .limit(limit)
                )
            )
            orphan_blob_ids = list(
                session.scalars(
                    select(ContentBlob.id)
                    .where(
                        ContentBlob.tenant_id == tenant_id,
                        ~exists().where(DocumentChunk.content_blob_id == ContentBlob.id),
                        ~exists().where(NormalizedDocument.content_blob_id == ContentBlob.id),
                    )
                    .limit(limit)
                )
            )
        return {
            "source_version_ids": source_ids,
            "index_version_ids": index_ids,
            "ingestion_run_ids": ingestion_run_ids,
            "indexing_run_ids": indexing_run_ids,
            "inference_job_ids": inference_job_ids,
            "indexing_event_ids": indexing_event_ids,
            "audit_event_ids": audit_ids,
            "orphan_embedding_ids": orphan_embedding_ids,
            "orphan_content_blob_ids": orphan_blob_ids,
        }

    def execute_retention(self, tenant_id: uuid.UUID, limit: int = 500):
        candidates = self.retention_candidates(tenant_id, limit)
        with self.session_factory.begin() as session:
            for key, model in (
                ("indexing_event_ids", IndexingEvent),
                ("indexing_run_ids", IndexingRun),
                ("ingestion_run_ids", IngestionRun),
                ("inference_job_ids", InferenceJob),
            ):
                if candidates[key]:
                    session.execute(
                        delete(model).where(
                            model.id.in_(candidates[key]), model.tenant_id == tenant_id
                        )
                    )
            if candidates["index_version_ids"]:
                session.execute(
                    delete(KnowledgeIndexVersion).where(
                        KnowledgeIndexVersion.id.in_(candidates["index_version_ids"]),
                        KnowledgeIndexVersion.tenant_id == tenant_id,
                        KnowledgeIndexVersion.pinned.is_(False),
                        KnowledgeIndexVersion.status.in_(
                            [IndexVersionStatus.SUPERSEDED.value, IndexVersionStatus.FAILED.value]
                        ),
                    )
                )
            if candidates["source_version_ids"]:
                session.execute(
                    delete(SourceVersion).where(
                        SourceVersion.id.in_(candidates["source_version_ids"]),
                        SourceVersion.tenant_id == tenant_id,
                        SourceVersion.pinned.is_(False),
                        SourceVersion.status.in_(
                            [SourceVersionStatus.SUPERSEDED.value, SourceVersionStatus.FAILED.value]
                        ),
                    )
                )
            if candidates["audit_event_ids"]:
                session.execute(
                    delete(AuditEvent).where(
                        AuditEvent.id.in_(candidates["audit_event_ids"]),
                        AuditEvent.tenant_id == tenant_id,
                    )
                )
        # Version removal can create new derived orphans; discover them after FK cascades.
        post = self.retention_candidates(tenant_id, limit)
        with self.session_factory.begin() as session:
            for orphan_key, orphan_model in (
                ("orphan_embedding_ids", ChunkEmbedding),
                ("orphan_content_blob_ids", ContentBlob),
            ):
                ids = post[orphan_key]
                if ids:
                    session.execute(
                        delete(orphan_model).where(
                            orphan_model.id.in_(ids), orphan_model.tenant_id == tenant_id
                        )
                    )
                    candidates[orphan_key] = list(dict.fromkeys([*candidates[orphan_key], *ids]))
        return candidates

    def list_models(self, tenant_id: uuid.UUID, offset: int = 0, limit: int = 100):
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(ModelDefinition)
                    .where(
                        or_(
                            ModelDefinition.tenant_id == tenant_id,
                            ModelDefinition.tenant_id.is_(None),
                        )
                    )
                    .order_by(ModelDefinition.role, ModelDefinition.created_at)
                    .offset(offset)
                    .limit(limit)
                )
            )

    def create_model(self, tenant_id: uuid.UUID, values: dict):
        expected_checksum = checksum(values)

        def existing(session: Session):
            return session.scalar(
                select(ModelDefinition).where(
                    ModelDefinition.tenant_id == tenant_id,
                    ModelDefinition.role == values["role"],
                    ModelDefinition.model_id == values["model_id"],
                    ModelDefinition.version == values["version"],
                )
            )

        try:
            with self.session_factory.begin() as session:
                row = existing(session)
                if row is not None:
                    if row.config_checksum != expected_checksum:
                        raise ValueError(
                            "This model version already exists with different configuration."
                        )
                    return row
                row = ModelDefinition(
                    tenant_id=tenant_id,
                    config_checksum=expected_checksum,
                    **values,
                )
                session.add(row)
                session.flush()
                return row
        except IntegrityError as exc:
            # A concurrent first-run tab may have inserted the same immutable definition.
            with self.session_factory() as session:
                row = existing(session)
                if row is not None and row.config_checksum == expected_checksum:
                    return row
            raise ValueError("Model version was created concurrently with other settings.") from exc

    def activate_model(self, tenant_id: uuid.UUID, model_id: uuid.UUID):
        with self.session_factory.begin() as session:
            target = session.scalar(
                select(ModelDefinition).where(
                    ModelDefinition.id == model_id, ModelDefinition.tenant_id == tenant_id
                )
            )
            if target is None:
                raise LookupError("Model definition was not found.")
            rows = list(
                session.scalars(
                    select(ModelDefinition)
                    .where(
                        ModelDefinition.tenant_id == tenant_id,
                        ModelDefinition.role == target.role,
                    )
                    .with_for_update()
                )
            )
            row = next(item for item in rows if item.id == model_id)
            for current in rows:
                current.is_active = current.id == row.id
            return row

    def resolve_active_model(self, tenant_id: uuid.UUID, role: str):
        with self.session_factory() as session:
            tenant = session.scalar(
                select(ModelDefinition).where(
                    ModelDefinition.tenant_id == tenant_id,
                    ModelDefinition.role == role,
                    ModelDefinition.is_active.is_(True),
                )
            )
            if tenant is not None:
                return tenant
            return session.scalar(
                select(ModelDefinition).where(
                    ModelDefinition.tenant_id.is_(None),
                    ModelDefinition.role == role,
                    ModelDefinition.is_active.is_(True),
                )
            )

    def get_model(self, model_id: uuid.UUID):
        with self.session_factory() as session:
            return session.get(ModelDefinition, model_id)

    def resolve_active_index_model(self, tenant_id: uuid.UUID, knowledge_base_id: uuid.UUID):
        with self.session_factory() as session:
            return session.scalar(
                select(ModelDefinition)
                .join(IndexingRun, IndexingRun.model_definition_id == ModelDefinition.id)
                .join(
                    KnowledgeIndexVersion,
                    KnowledgeIndexVersion.id == IndexingRun.index_version_id,
                )
                .where(
                    KnowledgeIndexVersion.tenant_id == tenant_id,
                    KnowledgeIndexVersion.knowledge_base_id == knowledge_base_id,
                    KnowledgeIndexVersion.status == IndexVersionStatus.ACTIVE.value,
                    IndexingRun.status == "completed",
                )
                .order_by(IndexingRun.completed_at.desc())
                .limit(1)
            )

    def list_prompts(self, tenant_id: uuid.UUID, offset: int = 0, limit: int = 100):
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(PromptTemplate)
                    .where(
                        or_(
                            PromptTemplate.tenant_id == tenant_id,
                            PromptTemplate.tenant_id.is_(None),
                        )
                    )
                    .order_by(PromptTemplate.name, PromptTemplate.created_at)
                    .offset(offset)
                    .limit(limit)
                )
            )

    def create_prompt(self, tenant_id: uuid.UUID, values: dict):
        with self.session_factory.begin() as session:
            row = PromptTemplate(
                tenant_id=tenant_id, checksum=checksum(values["template"]), **values
            )
            session.add(row)
            session.flush()
            return row

    def activate_prompt(self, tenant_id: uuid.UUID, prompt_id: uuid.UUID):
        with self.session_factory.begin() as session:
            target = session.scalar(
                select(PromptTemplate).where(
                    PromptTemplate.id == prompt_id, PromptTemplate.tenant_id == tenant_id
                )
            )
            if target is None:
                raise LookupError("Prompt template was not found.")
            rows = list(
                session.scalars(
                    select(PromptTemplate)
                    .where(
                        PromptTemplate.tenant_id == tenant_id,
                        PromptTemplate.name == target.name,
                    )
                    .with_for_update()
                )
            )
            row = next(item for item in rows if item.id == prompt_id)
            for current in rows:
                current.is_active = current.id == row.id
            return row

    def resolve_active_prompt(self, tenant_id: uuid.UUID, name: str):
        with self.session_factory() as session:
            tenant = session.scalar(
                select(PromptTemplate).where(
                    PromptTemplate.tenant_id == tenant_id,
                    PromptTemplate.name == name,
                    PromptTemplate.is_active.is_(True),
                )
            )
            if tenant is not None:
                return tenant
            return session.scalar(
                select(PromptTemplate).where(
                    PromptTemplate.tenant_id.is_(None),
                    PromptTemplate.name == name,
                    PromptTemplate.is_active.is_(True),
                )
            )

    def list_audit(self, tenant_id: uuid.UUID, offset: int, limit: int):
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.tenant_id == tenant_id)
                    .order_by(AuditEvent.created_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
