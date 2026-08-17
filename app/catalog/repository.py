from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.base import ConnectorDocument, DiscoveryResult, PreviousObject
from app.state.models import (
    ContentBlob,
    DocumentChunk,
    IngestionRun,
    IngestionRunStatus,
    KnowledgeBase,
    KnowledgeBaseSource,
    KnowledgeSource,
    NormalizedDocument,
    SourceObject,
    SourceVersion,
    SourceVersionStatus,
)


class CatalogNotFoundError(LookupError):
    pass


class InvalidVersionTransitionError(RuntimeError):
    pass


class ImmutableVersionError(RuntimeError):
    pass


class SourceHistoryExistsError(RuntimeError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


class LeaseLostError(RuntimeError):
    pass


ALLOWED_TRANSITIONS = {
    SourceVersionStatus.DISCOVERED.value: {SourceVersionStatus.INGESTING.value},
    SourceVersionStatus.INGESTING.value: {
        SourceVersionStatus.STAGED.value,
        SourceVersionStatus.FAILED.value,
    },
    SourceVersionStatus.STAGED.value: {
        SourceVersionStatus.VALIDATING.value,
        SourceVersionStatus.FAILED.value,
    },
    SourceVersionStatus.VALIDATING.value: {
        SourceVersionStatus.READY.value,
        SourceVersionStatus.FAILED.value,
    },
    SourceVersionStatus.READY.value: {SourceVersionStatus.ACTIVE.value},
    SourceVersionStatus.ACTIVE.value: {SourceVersionStatus.SUPERSEDED.value},
    SourceVersionStatus.SUPERSEDED.value: {SourceVersionStatus.ACTIVE.value},
    SourceVersionStatus.FAILED.value: set(),
}


class CatalogRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    @staticmethod
    def _has_valid_lease(
        run: IngestionRun | None,
        lease_token: uuid.UUID,
        version_id: uuid.UUID | None = None,
    ) -> bool:
        if (
            run is None
            or run.status != IngestionRunStatus.RUNNING.value
            or run.lease_token != lease_token
            or run.lease_expires_at is None
        ):
            return False
        expires_at = (
            run.lease_expires_at.replace(tzinfo=UTC)
            if run.lease_expires_at.tzinfo is None
            else run.lease_expires_at
        )
        return expires_at > datetime.now(UTC) and (
            version_id is None or run.source_version_id == version_id
        )

    @classmethod
    def _assert_lease(
        cls,
        session: Session,
        run_id: uuid.UUID | None,
        lease_token: uuid.UUID | None,
        version_id: uuid.UUID,
    ) -> None:
        if run_id is None and lease_token is None:
            return
        if run_id is None or lease_token is None:
            raise LeaseLostError("A complete ingestion lease fence is required.")
        run = session.scalar(
            select(IngestionRun).where(IngestionRun.id == run_id).with_for_update()
        )
        if not cls._has_valid_lease(run, lease_token, version_id):
            raise LeaseLostError("Ingestion lease is no longer valid.")

    def list_knowledge_bases(self, tenant_id: uuid.UUID) -> list[KnowledgeBase]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(KnowledgeBase)
                    .where(KnowledgeBase.tenant_id == tenant_id)
                    .order_by(KnowledgeBase.created_at)
                )
            )

    def knowledge_base_ids_for_source(
        self, tenant_id: uuid.UUID, source_id: uuid.UUID
    ) -> list[uuid.UUID]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(KnowledgeBaseSource.knowledge_base_id).where(
                        KnowledgeBaseSource.tenant_id == tenant_id,
                        KnowledgeBaseSource.source_id == source_id,
                    )
                )
            )

    def get_knowledge_base(
        self, tenant_id: uuid.UUID, knowledge_base_id: uuid.UUID
    ) -> KnowledgeBase | None:
        with self.session_factory() as session:
            return session.scalar(
                select(KnowledgeBase).where(
                    KnowledgeBase.id == knowledge_base_id,
                    KnowledgeBase.tenant_id == tenant_id,
                )
            )

    def create_knowledge_base(
        self, tenant_id: uuid.UUID, name: str, description: str | None, is_enabled: bool
    ) -> KnowledgeBase:
        with self.session_factory.begin() as session:
            item = KnowledgeBase(
                tenant_id=tenant_id,
                name=name,
                description=description,
                is_enabled=is_enabled,
            )
            session.add(item)
            session.flush()
            return item

    def update_knowledge_base(
        self,
        tenant_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        changes: dict,
    ) -> KnowledgeBase | None:
        with self.session_factory.begin() as session:
            item = session.scalar(
                select(KnowledgeBase)
                .where(
                    KnowledgeBase.id == knowledge_base_id,
                    KnowledgeBase.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if item is None:
                return None
            for key, value in changes.items():
                setattr(item, key, value)
            return item

    def delete_knowledge_base(self, tenant_id: uuid.UUID, knowledge_base_id: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            item = session.scalar(
                select(KnowledgeBase)
                .where(
                    KnowledgeBase.id == knowledge_base_id,
                    KnowledgeBase.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if item is None:
                return False
            session.delete(item)
            return True

    def list_sources(self, tenant_id: uuid.UUID) -> list[KnowledgeSource]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(KnowledgeSource)
                    .where(KnowledgeSource.tenant_id == tenant_id)
                    .order_by(KnowledgeSource.created_at)
                )
            )

    def get_source(self, tenant_id: uuid.UUID, source_id: uuid.UUID) -> KnowledgeSource | None:
        with self.session_factory() as session:
            return session.scalar(
                select(KnowledgeSource).where(
                    KnowledgeSource.id == source_id,
                    KnowledgeSource.tenant_id == tenant_id,
                )
            )

    def create_source(
        self,
        tenant_id: uuid.UUID,
        name: str,
        source_type: str,
        config_version: str,
        config: dict,
        is_enabled: bool,
    ) -> KnowledgeSource:
        with self.session_factory.begin() as session:
            item = KnowledgeSource(
                tenant_id=tenant_id,
                name=name,
                source_type=source_type,
                config_version=config_version,
                config=config,
                is_enabled=is_enabled,
            )
            session.add(item)
            session.flush()
            return item

    def update_source(
        self, tenant_id: uuid.UUID, source_id: uuid.UUID, changes: dict
    ) -> KnowledgeSource | None:
        with self.session_factory.begin() as session:
            item = session.scalar(
                select(KnowledgeSource)
                .where(
                    KnowledgeSource.id == source_id,
                    KnowledgeSource.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if item is None:
                return None
            for key, value in changes.items():
                setattr(item, key, value)
            return item

    def delete_source(self, tenant_id: uuid.UUID, source_id: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            item = session.scalar(
                select(KnowledgeSource)
                .where(
                    KnowledgeSource.id == source_id,
                    KnowledgeSource.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if item is None:
                return False
            has_versions = session.scalar(
                select(SourceVersion.id).where(SourceVersion.source_id == source_id).limit(1)
            )
            if has_versions is not None:
                raise SourceHistoryExistsError(
                    "Source with ingestion history cannot be deleted; disable it instead."
                )
            session.delete(item)
            return True

    def link_source(
        self, tenant_id: uuid.UUID, knowledge_base_id: uuid.UUID, source_id: uuid.UUID
    ) -> KnowledgeBaseSource:
        with self.session_factory.begin() as session:
            knowledge_base = session.scalar(
                select(KnowledgeBase).where(
                    KnowledgeBase.id == knowledge_base_id,
                    KnowledgeBase.tenant_id == tenant_id,
                )
            )
            source = session.scalar(
                select(KnowledgeSource).where(
                    KnowledgeSource.id == source_id,
                    KnowledgeSource.tenant_id == tenant_id,
                )
            )
            if knowledge_base is None or source is None:
                raise CatalogNotFoundError("Knowledge base or source was not found.")
            existing = session.scalar(
                select(KnowledgeBaseSource).where(
                    KnowledgeBaseSource.knowledge_base_id == knowledge_base_id,
                    KnowledgeBaseSource.source_id == source_id,
                )
            )
            if existing is not None:
                return existing
            link = KnowledgeBaseSource(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                source_id=source_id,
            )
            session.add(link)
            session.flush()
            return link

    def unlink_source(
        self, tenant_id: uuid.UUID, knowledge_base_id: uuid.UUID, source_id: uuid.UUID
    ) -> bool:
        with self.session_factory.begin() as session:
            result = session.execute(
                delete(KnowledgeBaseSource).where(
                    KnowledgeBaseSource.tenant_id == tenant_id,
                    KnowledgeBaseSource.knowledge_base_id == knowledge_base_id,
                    KnowledgeBaseSource.source_id == source_id,
                )
            )
            return bool(result.rowcount)

    def list_linked_sources(
        self, tenant_id: uuid.UUID, knowledge_base_id: uuid.UUID
    ) -> list[KnowledgeSource]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(KnowledgeSource)
                    .join(
                        KnowledgeBaseSource,
                        KnowledgeBaseSource.source_id == KnowledgeSource.id,
                    )
                    .where(
                        KnowledgeBaseSource.tenant_id == tenant_id,
                        KnowledgeBaseSource.knowledge_base_id == knowledge_base_id,
                    )
                    .order_by(KnowledgeSource.created_at)
                )
            )

    def create_version(self, tenant_id: uuid.UUID, source_id: uuid.UUID) -> SourceVersion:
        with self.session_factory.begin() as session:
            source = session.scalar(
                select(KnowledgeSource)
                .where(
                    KnowledgeSource.id == source_id,
                    KnowledgeSource.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if source is None:
                raise CatalogNotFoundError("Knowledge source was not found.")
            latest = session.scalar(
                select(func.max(SourceVersion.version_number)).where(
                    SourceVersion.source_id == source_id
                )
            )
            version = SourceVersion(
                tenant_id=tenant_id,
                source_id=source_id,
                version_number=(latest or 0) + 1,
                status=SourceVersionStatus.DISCOVERED.value,
                config_snapshot=dict(source.config),
            )
            session.add(version)
            session.flush()
            return version

    def get_version(
        self, tenant_id: uuid.UUID, source_id: uuid.UUID, version_id: uuid.UUID
    ) -> SourceVersion | None:
        with self.session_factory() as session:
            return session.scalar(
                select(SourceVersion).where(
                    SourceVersion.id == version_id,
                    SourceVersion.source_id == source_id,
                    SourceVersion.tenant_id == tenant_id,
                )
            )

    def list_versions(self, tenant_id: uuid.UUID, source_id: uuid.UUID) -> list[SourceVersion]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(SourceVersion)
                    .where(
                        SourceVersion.tenant_id == tenant_id,
                        SourceVersion.source_id == source_id,
                    )
                    .order_by(SourceVersion.version_number.desc())
                )
            )

    def set_version_pinned(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        version_id: uuid.UUID,
        pinned: bool,
    ) -> SourceVersion:
        with self.session_factory.begin() as session:
            version = session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.id == version_id,
                    SourceVersion.source_id == source_id,
                    SourceVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if version is None:
                raise CatalogNotFoundError("Source version was not found.")
            version.pinned = pinned
            return version

    def transition_version(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        version_id: uuid.UUID,
        target_status: str,
        *,
        failure_code: str | None = None,
        run_id: uuid.UUID | None = None,
        lease_token: uuid.UUID | None = None,
    ) -> SourceVersion:
        with self.session_factory.begin() as session:
            self._assert_lease(session, run_id, lease_token, version_id)
            version = session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.id == version_id,
                    SourceVersion.source_id == source_id,
                    SourceVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if version is None:
                raise CatalogNotFoundError("Source version was not found.")
            if target_status not in ALLOWED_TRANSITIONS.get(version.status, set()):
                raise InvalidVersionTransitionError(
                    f"Transition {version.status} -> {target_status} is not allowed."
                )
            version.status = target_status
            if target_status == SourceVersionStatus.FAILED.value:
                version.failed_at = datetime.now(UTC)
                version.failure_code = failure_code
            return version

    def write_version_content(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        objects: list[dict],
        manifest: dict,
        content_checksum: str,
    ) -> SourceVersion:
        with self.session_factory.begin() as session:
            version = session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.id == version_id,
                    SourceVersion.source_id == source_id,
                    SourceVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if version is None:
                raise CatalogNotFoundError("Source version was not found.")
            if version.status != SourceVersionStatus.INGESTING.value:
                raise ImmutableVersionError("Version contents can change only while ingesting.")
            session.execute(
                delete(SourceObject).where(SourceObject.source_version_id == version_id)
            )
            for item in objects:
                session.add(
                    SourceObject(
                        tenant_id=tenant_id,
                        source_version_id=version.id,
                        **item,
                    )
                )
            version.manifest = manifest
            version.content_checksum = content_checksum
            return version

    def activate_version(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        run_id: uuid.UUID | None = None,
        lease_token: uuid.UUID | None = None,
    ) -> SourceVersion:
        with self.session_factory.begin() as session:
            self._assert_lease(session, run_id, lease_token, version_id)
            source = session.scalar(
                select(KnowledgeSource)
                .where(
                    KnowledgeSource.id == source_id,
                    KnowledgeSource.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if source is None:
                raise CatalogNotFoundError("Knowledge source was not found.")
            target = session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.id == version_id,
                    SourceVersion.source_id == source_id,
                    SourceVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if target is None:
                raise CatalogNotFoundError("Source version was not found.")
            if target.status == SourceVersionStatus.ACTIVE.value:
                return target
            if target.status not in {
                SourceVersionStatus.READY.value,
                SourceVersionStatus.SUPERSEDED.value,
            }:
                raise InvalidVersionTransitionError(
                    f"Version in {target.status} state cannot be activated."
                )
            now = datetime.now(UTC)
            current = session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.source_id == source_id,
                    SourceVersion.status == SourceVersionStatus.ACTIVE.value,
                )
                .with_for_update()
            )
            if current is not None:
                current.status = SourceVersionStatus.SUPERSEDED.value
                current.superseded_at = now
                # Release the partial unique active-version slot before promoting the target.
                session.flush()
            target.status = SourceVersionStatus.ACTIVE.value
            target.activated_at = now
            target.superseded_at = None
            return target

    def list_ingestion_runs(self, tenant_id: uuid.UUID, source_id: uuid.UUID) -> list[IngestionRun]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(IngestionRun)
                    .where(
                        IngestionRun.tenant_id == tenant_id,
                        IngestionRun.source_id == source_id,
                    )
                    .order_by(IngestionRun.created_at.desc())
                )
            )

    def get_ingestion_run(self, tenant_id: uuid.UUID, run_id: uuid.UUID) -> IngestionRun | None:
        with self.session_factory() as session:
            return session.scalar(
                select(IngestionRun).where(
                    IngestionRun.id == run_id,
                    IngestionRun.tenant_id == tenant_id,
                )
            )

    def create_refresh_job(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        *,
        idempotency_key: str,
        request_hash: str,
        correlation_id: uuid.UUID,
        auto_activate: bool,
        max_attempts: int,
    ) -> tuple[IngestionRun, SourceVersion, bool]:
        try:
            with self.session_factory.begin() as session:
                source = session.scalar(
                    select(KnowledgeSource)
                    .where(
                        KnowledgeSource.id == source_id,
                        KnowledgeSource.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                if source is None:
                    raise CatalogNotFoundError("Knowledge source was not found.")
                if not source.is_enabled:
                    raise InvalidVersionTransitionError("Disabled source cannot be refreshed.")
                existing = session.scalar(
                    select(IngestionRun).where(
                        IngestionRun.tenant_id == tenant_id,
                        IngestionRun.operation == "refresh_source",
                        IngestionRun.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_hash != request_hash or existing.source_id != source_id:
                        raise IdempotencyConflictError(
                            "Idempotency-Key was already used for a different request."
                        )
                    version = session.get(SourceVersion, existing.source_version_id)
                    return existing, version, False
                latest = session.scalar(
                    select(func.max(SourceVersion.version_number)).where(
                        SourceVersion.source_id == source_id
                    )
                )
                version = SourceVersion(
                    tenant_id=tenant_id,
                    source_id=source_id,
                    version_number=(latest or 0) + 1,
                    status=SourceVersionStatus.DISCOVERED.value,
                    config_snapshot=dict(source.config),
                )
                session.add(version)
                session.flush()
                connector_version = f"{source.source_type}/1.0"
                run = IngestionRun(
                    tenant_id=tenant_id,
                    source_id=source_id,
                    source_version_id=version.id,
                    status=IngestionRunStatus.QUEUED.value,
                    connector_version=connector_version,
                    operation="refresh_source",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    correlation_id=correlation_id,
                    auto_activate=auto_activate,
                    max_attempts=max_attempts,
                )
                session.add(run)
                session.flush()
                return run, version, True
        except IntegrityError:
            with self.session_factory() as session:
                existing = session.scalar(
                    select(IngestionRun).where(
                        IngestionRun.tenant_id == tenant_id,
                        IngestionRun.operation == "refresh_source",
                        IngestionRun.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise
                if existing.request_hash != request_hash or existing.source_id != source_id:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was already used for a different request."
                    )
                return existing, session.get(SourceVersion, existing.source_version_id), False

    def claim_ingestion_run(
        self, run_id: uuid.UUID, worker_id: str, lease_seconds: int
    ) -> tuple[IngestionRun, uuid.UUID] | None:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IngestionRun).where(IngestionRun.id == run_id).with_for_update()
            )
            if run is None or run.status in {
                IngestionRunStatus.COMPLETED.value,
                IngestionRunStatus.FAILED.value,
                IngestionRunStatus.CANCELLED.value,
            }:
                return None
            if (
                run.status == IngestionRunStatus.RUNNING.value
                and run.lease_expires_at is not None
                and (
                    run.lease_expires_at.replace(tzinfo=UTC)
                    if run.lease_expires_at.tzinfo is None
                    else run.lease_expires_at
                )
                > now
            ):
                return None
            if run.attempt_count >= run.max_attempts:
                return None
            lease_token = uuid.uuid4()
            run.status = IngestionRunStatus.RUNNING.value
            run.attempt_count += 1
            run.started_at = run.started_at or now
            run.lease_owner = worker_id
            run.lease_token = lease_token
            run.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return run, lease_token

    def fail_exhausted_ingestion(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        version_id: uuid.UUID,
        run_id: uuid.UUID,
        error_code: str = "retry_exhausted",
        error_message: str = "Ingestion failed. See logs using the correlation ID.",
    ) -> bool:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IngestionRun)
                .where(
                    IngestionRun.id == run_id,
                    IngestionRun.tenant_id == tenant_id,
                    IngestionRun.source_id == source_id,
                    IngestionRun.source_version_id == version_id,
                )
                .with_for_update()
            )
            if (
                run is None
                or run.status
                in {
                    IngestionRunStatus.COMPLETED.value,
                    IngestionRunStatus.FAILED.value,
                    IngestionRunStatus.CANCELLED.value,
                }
                or run.attempt_count < run.max_attempts
            ):
                return False
            if run.status == IngestionRunStatus.RUNNING.value:
                if run.lease_expires_at is None:
                    return False
                expires_at = (
                    run.lease_expires_at.replace(tzinfo=UTC)
                    if run.lease_expires_at.tzinfo is None
                    else run.lease_expires_at
                )
                if expires_at > now:
                    return False
            version = session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.id == version_id,
                    SourceVersion.source_id == source_id,
                    SourceVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            run.status = IngestionRunStatus.FAILED.value
            run.completed_at = now
            run.error_code = error_code[:100]
            run.error_message = error_message[:500]
            run.lease_owner = None
            run.lease_token = None
            run.lease_expires_at = None
            if version is not None and version.status not in {
                SourceVersionStatus.ACTIVE.value,
                SourceVersionStatus.SUPERSEDED.value,
            }:
                version.status = SourceVersionStatus.FAILED.value
                version.failed_at = now
                version.failure_code = error_code[:100]
                version.failure_message = error_message[:500]
            return True

    def renew_ingestion_lease(
        self, run_id: uuid.UUID, lease_token: uuid.UUID, lease_seconds: int
    ) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IngestionRun).where(IngestionRun.id == run_id).with_for_update()
            )
            if not self._has_valid_lease(run, lease_token):
                return False
            run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
            return True

    def retry_ingestion_job(
        self, run_id: uuid.UUID, lease_token: uuid.UUID, error_code: str
    ) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IngestionRun).where(IngestionRun.id == run_id).with_for_update()
            )
            if not self._has_valid_lease(run, lease_token) or run.attempt_count >= run.max_attempts:
                return False
            run.status = IngestionRunStatus.QUEUED.value
            run.error_code = error_code[:100]
            run.error_message = "Temporary ingestion failure; retry scheduled."
            run.lease_owner = None
            run.lease_token = None
            run.lease_expires_at = None
            return True

    def request_ingestion_cancel(self, tenant_id: uuid.UUID, run_id: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IngestionRun)
                .where(IngestionRun.id == run_id, IngestionRun.tenant_id == tenant_id)
                .with_for_update()
            )
            if run is None:
                raise CatalogNotFoundError("Ingestion run was not found.")
            if run.status in {
                IngestionRunStatus.COMPLETED.value,
                IngestionRunStatus.FAILED.value,
                IngestionRunStatus.CANCELLED.value,
            }:
                return False
            run.cancel_requested = True
            if run.status == IngestionRunStatus.QUEUED.value:
                run.status = IngestionRunStatus.CANCELLED.value
                run.completed_at = datetime.now(UTC)
                version = session.get(SourceVersion, run.source_version_id)
                if version is not None:
                    version.status = SourceVersionStatus.FAILED.value
                    version.failed_at = datetime.now(UTC)
                    version.failure_code = "ingestion_cancelled"
                    version.failure_message = "Ingestion was cancelled."
            return True

    def ingestion_cancel_requested(self, run_id: uuid.UUID) -> bool:
        with self.session_factory() as session:
            return bool(
                session.scalar(
                    select(IngestionRun.cancel_requested).where(IngestionRun.id == run_id)
                )
            )

    def complete_ingestion_job(self, run_id: uuid.UUID, lease_token: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IngestionRun).where(IngestionRun.id == run_id).with_for_update()
            )
            if not self._has_valid_lease(run, lease_token):
                return False
            run.status = IngestionRunStatus.COMPLETED.value
            run.completed_at = datetime.now(UTC)
            run.lease_owner = None
            run.lease_token = None
            run.lease_expires_at = None
            return True

    def cancel_ingestion_job(self, run_id: uuid.UUID, lease_token: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IngestionRun).where(IngestionRun.id == run_id).with_for_update()
            )
            if not self._has_valid_lease(run, lease_token):
                return False
            run.status = IngestionRunStatus.CANCELLED.value
            run.completed_at = datetime.now(UTC)
            run.lease_owner = None
            run.lease_token = None
            run.lease_expires_at = None
            version = session.get(SourceVersion, run.source_version_id)
            if version is not None and version.status not in {
                SourceVersionStatus.ACTIVE.value,
                SourceVersionStatus.SUPERSEDED.value,
            }:
                version.status = SourceVersionStatus.FAILED.value
                version.failed_at = datetime.now(UTC)
                version.failure_code = "ingestion_cancelled"
                version.failure_message = "Ingestion was cancelled."
            return True

    def load_previous_objects(
        self, tenant_id: uuid.UUID, source_id: uuid.UUID
    ) -> tuple[dict[str, PreviousObject], str | None, uuid.UUID | None]:
        with self.session_factory() as session:
            version = session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.tenant_id == tenant_id,
                    SourceVersion.source_id == source_id,
                    SourceVersion.status.in_(
                        [
                            SourceVersionStatus.ACTIVE.value,
                            SourceVersionStatus.READY.value,
                            SourceVersionStatus.SUPERSEDED.value,
                        ]
                    ),
                )
                .order_by(
                    (SourceVersion.status == SourceVersionStatus.ACTIVE.value).desc(),
                    SourceVersion.version_number.desc(),
                )
            )
            if version is None:
                return {}, None, None
            rows = session.scalars(
                select(SourceObject).where(SourceObject.source_version_id == version.id)
            )
            objects = {
                item.object_key: PreviousObject(
                    object_key=item.object_key,
                    checksum=item.checksum,
                    metadata={
                        **dict(item.metadata_json),
                        "_byte_count": item.byte_count,
                        "_chunk_count": item.chunk_count,
                    },
                )
                for item in rows
            }
            revision = (version.manifest or {}).get("source_revision")
            return objects, revision, version.id

    @staticmethod
    def _content_blob(session: Session, tenant_id: uuid.UUID, content: str) -> ContentBlob:
        checksum = __import__("hashlib").sha256(content.encode("utf-8")).hexdigest()
        blob = session.scalar(
            select(ContentBlob).where(
                ContentBlob.tenant_id == tenant_id,
                ContentBlob.checksum == checksum,
            )
        )
        if blob is None:
            blob = ContentBlob(
                tenant_id=tenant_id,
                checksum=checksum,
                content=content,
                byte_count=len(content.encode("utf-8")),
            )
            session.add(blob)
            session.flush()
        return blob

    def persist_discovery(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        version_id: uuid.UUID,
        discovery: DiscoveryResult,
        previous_version_id: uuid.UUID | None,
        manifest: dict,
        content_checksum: str,
        *,
        run_id: uuid.UUID | None = None,
        lease_token: uuid.UUID | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            self._assert_lease(session, run_id, lease_token, version_id)
            version = session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.id == version_id,
                    SourceVersion.source_id == source_id,
                    SourceVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if version is None:
                raise CatalogNotFoundError("Source version was not found.")
            if version.status != SourceVersionStatus.INGESTING.value:
                raise ImmutableVersionError("Version contents can change only while ingesting.")
            document_ids = select(NormalizedDocument.id).where(
                NormalizedDocument.source_version_id == version_id
            )
            session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id.in_(document_ids))
            )
            session.execute(
                delete(NormalizedDocument).where(NormalizedDocument.source_version_id == version_id)
            )
            session.execute(
                delete(SourceObject).where(SourceObject.source_version_id == version_id)
            )
            previous_documents = {}
            if previous_version_id is not None:
                previous_documents = {
                    item.object_key: item
                    for item in session.scalars(
                        select(NormalizedDocument).where(
                            NormalizedDocument.source_version_id == previous_version_id
                        )
                    )
                }
            by_key = {document.object_key: document for document in discovery.documents}
            for key in discovery.unchanged:
                if key in by_key:
                    continue
                previous = previous_documents.get(key)
                if previous is None:
                    continue
                source_object = session.scalar(
                    select(SourceObject).where(
                        SourceObject.source_version_id == previous_version_id,
                        SourceObject.object_key == key,
                    )
                )
                if source_object is None:
                    continue
                session.add(
                    SourceObject(
                        tenant_id=tenant_id,
                        source_version_id=version_id,
                        object_key=key,
                        checksum=source_object.checksum,
                        byte_count=source_object.byte_count,
                        chunk_count=source_object.chunk_count,
                        metadata_json=dict(source_object.metadata_json),
                    )
                )
                clone = NormalizedDocument(
                    tenant_id=tenant_id,
                    source_version_id=version_id,
                    object_key=key,
                    canonical_uri=previous.canonical_uri,
                    title=previous.title,
                    checksum=previous.checksum,
                    content_blob_id=previous.content_blob_id,
                    metadata_json=dict(previous.metadata_json),
                )
                session.add(clone)
                session.flush()
                old_chunks = session.scalars(
                    select(DocumentChunk).where(DocumentChunk.document_id == previous.id)
                )
                for chunk in old_chunks:
                    session.add(
                        DocumentChunk(
                            tenant_id=tenant_id,
                            document_id=clone.id,
                            chunk_index=chunk.chunk_index,
                            checksum=chunk.checksum,
                            content_blob_id=chunk.content_blob_id,
                            metadata_json=dict(chunk.metadata_json),
                        )
                    )
            for document in discovery.documents:
                self._persist_document(session, tenant_id, version_id, document)
            version.manifest = manifest
            version.content_checksum = content_checksum

    def _persist_document(
        self,
        session: Session,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
        document: ConnectorDocument,
    ) -> None:
        session.add(
            SourceObject(
                tenant_id=tenant_id,
                source_version_id=version_id,
                object_key=document.object_key,
                checksum=document.checksum,
                byte_count=document.byte_count,
                chunk_count=len(document.chunks),
                metadata_json=dict(document.metadata),
            )
        )
        blob = self._content_blob(session, tenant_id, document.text)
        normalized = NormalizedDocument(
            tenant_id=tenant_id,
            source_version_id=version_id,
            object_key=document.object_key,
            canonical_uri=document.canonical_uri,
            title=document.title,
            checksum=document.checksum,
            content_blob_id=blob.id,
            metadata_json=dict(document.metadata),
        )
        session.add(normalized)
        session.flush()
        for chunk in document.chunks:
            chunk_blob = self._content_blob(session, tenant_id, chunk.text)
            session.add(
                DocumentChunk(
                    tenant_id=tenant_id,
                    document_id=normalized.id,
                    chunk_index=chunk.chunk_index,
                    checksum=chunk.checksum,
                    content_blob_id=chunk_blob.id,
                    metadata_json=dict(chunk.metadata),
                )
            )

    def create_ingestion_run(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        version_id: uuid.UUID,
        connector_version: str,
    ) -> IngestionRun:
        with self.session_factory.begin() as session:
            run = IngestionRun(
                tenant_id=tenant_id,
                source_id=source_id,
                source_version_id=version_id,
                status="running",
                connector_version=connector_version,
            )
            session.add(run)
            session.flush()
            return run

    def complete_ingestion_run(
        self, run_id: uuid.UUID, status: str, error_code: str | None = None
    ) -> None:
        with self.session_factory.begin() as session:
            run = session.get(IngestionRun, run_id)
            if run is not None:
                run.status = status
                run.completed_at = datetime.now(UTC)
                run.error_code = error_code

    def fail_ingestion(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        version_id: uuid.UUID,
        run_id: uuid.UUID,
        error_code: str,
        error_message: str,
        lease_token: uuid.UUID | None = None,
    ) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IngestionRun)
                .where(
                    IngestionRun.id == run_id,
                    IngestionRun.source_version_id == version_id,
                    IngestionRun.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if lease_token is not None and not self._has_valid_lease(run, lease_token, version_id):
                return False
            version = session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.id == version_id,
                    SourceVersion.source_id == source_id,
                    SourceVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if version is not None and version.status not in {
                SourceVersionStatus.ACTIVE.value,
                SourceVersionStatus.SUPERSEDED.value,
            }:
                version.status = SourceVersionStatus.FAILED.value
                version.failed_at = datetime.now(UTC)
                version.failure_code = error_code[:100]
                version.failure_message = error_message[:500]
            if run is not None:
                run.status = "failed"
                run.completed_at = datetime.now(UTC)
                run.error_code = error_code[:100]
                run.error_message = error_message[:500]
                run.lease_owner = None
                run.lease_token = None
                run.lease_expires_at = None
            return version is not None or run is not None
