from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.state.models import (
    AuditEvent,
    IndexVersionStatus,
    KnowledgeIndexVersion,
    KnowledgeSource,
    ModelDefinition,
    PromptTemplate,
    RetentionPolicy,
    SourceSchedule,
    SourceVersion,
    SourceVersionStatus,
)


def checksum(value: dict | str) -> str:
    payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class OperationsRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

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

    def claim_due_schedules(self, limit: int = 20):
        with self.session_factory.begin() as session:
            now = datetime.now(UTC)
            rows = list(
                session.scalars(
                    select(SourceSchedule)
                    .where(SourceSchedule.is_enabled.is_(True), SourceSchedule.next_run_at <= now)
                    .order_by(SourceSchedule.next_run_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            claimed = []
            for row in rows:
                scheduled_for = (
                    row.next_run_at.replace(tzinfo=UTC)
                    if row.next_run_at.tzinfo is None
                    else row.next_run_at
                )
                row.last_run_at = now
                # Bounded catch-up: one trigger per scheduler pass.
                row.next_run_at = max(scheduled_for, now) + timedelta(seconds=row.interval_seconds)
                claimed.append((row.id, row.tenant_id, row.source_id, scheduled_for))
            return claimed

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
        return {"source_version_ids": source_ids, "index_version_ids": index_ids}

    def execute_retention(self, tenant_id: uuid.UUID, limit: int = 500):
        candidates = self.retention_candidates(tenant_id, limit)
        with self.session_factory.begin() as session:
            if candidates["source_version_ids"]:
                session.execute(
                    delete(SourceVersion).where(
                        SourceVersion.id.in_(candidates["source_version_ids"]),
                        SourceVersion.tenant_id == tenant_id,
                        SourceVersion.status.in_(
                            [SourceVersionStatus.SUPERSEDED.value, SourceVersionStatus.FAILED.value]
                        ),
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
        with self.session_factory.begin() as session:
            row = ModelDefinition(
                tenant_id=tenant_id,
                config_checksum=checksum(values),
                **values,
            )
            session.add(row)
            session.flush()
            return row

    def activate_model(self, tenant_id: uuid.UUID, model_id: uuid.UUID):
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(ModelDefinition)
                .where(ModelDefinition.id == model_id, ModelDefinition.tenant_id == tenant_id)
                .with_for_update()
            )
            if row is None:
                raise LookupError("Model definition was not found.")
            for current in session.scalars(
                select(ModelDefinition).where(
                    ModelDefinition.tenant_id == tenant_id,
                    ModelDefinition.role == row.role,
                    ModelDefinition.is_active.is_(True),
                )
            ):
                current.is_active = False
            row.is_active = True
            return row

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
            row = session.scalar(
                select(PromptTemplate)
                .where(PromptTemplate.id == prompt_id, PromptTemplate.tenant_id == tenant_id)
                .with_for_update()
            )
            if row is None:
                raise LookupError("Prompt template was not found.")
            for current in session.scalars(
                select(PromptTemplate).where(
                    PromptTemplate.tenant_id == tenant_id,
                    PromptTemplate.name == row.name,
                    PromptTemplate.is_active.is_(True),
                )
            ):
                current.is_active = False
            row.is_active = True
            return row

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
