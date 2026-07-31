from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.state.models import (
    IngestionRun,
    KnowledgeBase,
    KnowledgeBaseSource,
    KnowledgeSource,
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

    def list_knowledge_bases(self, tenant_id: uuid.UUID) -> list[KnowledgeBase]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(KnowledgeBase)
                    .where(KnowledgeBase.tenant_id == tenant_id)
                    .order_by(KnowledgeBase.created_at)
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

    def transition_version(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        version_id: uuid.UUID,
        target_status: str,
        *,
        failure_code: str | None = None,
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
    ) -> SourceVersion:
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
    ) -> None:
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
            run = session.scalar(
                select(IngestionRun)
                .where(
                    IngestionRun.id == run_id,
                    IngestionRun.source_version_id == version_id,
                    IngestionRun.tenant_id == tenant_id,
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
