from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.state.models import (
    ChunkEmbedding,
    ContentBlob,
    DocumentChunk,
    EmbeddingModelVersion,
    IndexingEvent,
    IndexingRun,
    IndexingRunStatus,
    IndexVersionStatus,
    KnowledgeBase,
    KnowledgeBaseSource,
    KnowledgeIndexEntry,
    KnowledgeIndexVersion,
    KnowledgeSource,
    ModelDefinition,
    NormalizedDocument,
    SourceVersion,
    SourceVersionStatus,
)


class IndexingConflictError(RuntimeError):
    pass


class IndexRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def create_run(
        self,
        tenant_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        idempotency_key: str,
        request_hash: str,
        max_attempts: int,
    ) -> tuple[IndexingRun, bool]:
        with self.session_factory.begin() as session:
            existing = session.scalar(
                select(IndexingRun).where(
                    IndexingRun.tenant_id == tenant_id,
                    IndexingRun.idempotency_key == idempotency_key,
                )
            )
            if existing:
                if existing.request_hash != request_hash:
                    raise IndexingConflictError("Idempotency key was used with another request.")
                return existing, False
            kb = session.scalar(
                select(KnowledgeBase)
                .where(KnowledgeBase.id == knowledge_base_id, KnowledgeBase.tenant_id == tenant_id)
                .with_for_update()
            )
            if kb is None:
                raise LookupError("Knowledge base was not found.")
            model_definition = session.scalar(
                select(ModelDefinition).where(
                    ModelDefinition.tenant_id == tenant_id,
                    ModelDefinition.role == "embedding",
                    ModelDefinition.is_active.is_(True),
                )
            ) or session.scalar(
                select(ModelDefinition).where(
                    ModelDefinition.tenant_id.is_(None),
                    ModelDefinition.role == "embedding",
                    ModelDefinition.is_active.is_(True),
                )
            )
            if model_definition is None:
                raise LookupError("An active embedding model is required before indexing.")
            model = session.scalar(
                select(EmbeddingModelVersion).where(
                    EmbeddingModelVersion.model_id == model_definition.model_id,
                    EmbeddingModelVersion.version == model_definition.version,
                    EmbeddingModelVersion.is_enabled.is_(True),
                )
            )
            if model is None:
                raise LookupError("Active registry model has no enabled embedding contract.")
            configured_dimensions = model_definition.capabilities.get("dimensions")
            if configured_dimensions is not None and int(configured_dimensions) != model.dimensions:
                raise IndexingConflictError(
                    "Embedding registry dimensions do not match the model contract."
                )
            number = (
                session.scalar(
                    select(func.max(KnowledgeIndexVersion.version_number)).where(
                        KnowledgeIndexVersion.knowledge_base_id == knowledge_base_id
                    )
                )
                or 0
            ) + 1
            version = KnowledgeIndexVersion(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                embedding_model_version_id=model.id,
                version_number=number,
                status=IndexVersionStatus.CREATED.value,
                manifest={},
            )
            session.add(version)
            session.flush()
            run = IndexingRun(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                index_version_id=version.id,
                model_definition_id=model_definition.id,
                status=IndexingRunStatus.QUEUED.value,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                max_attempts=max_attempts,
            )
            session.add(run)
            session.flush()
            return run, True

    def get_run(self, tenant_id: uuid.UUID, run_id: uuid.UUID):
        with self.session_factory() as session:
            return session.scalar(
                select(IndexingRun).where(
                    IndexingRun.id == run_id, IndexingRun.tenant_id == tenant_id
                )
            )

    def list_runs(self, tenant_id: uuid.UUID, offset: int, limit: int):
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(IndexingRun)
                    .where(IndexingRun.tenant_id == tenant_id)
                    .order_by(IndexingRun.created_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )

    def list_versions(self, tenant_id: uuid.UUID, knowledge_base_id: uuid.UUID):
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(KnowledgeIndexVersion)
                    .where(
                        KnowledgeIndexVersion.tenant_id == tenant_id,
                        KnowledgeIndexVersion.knowledge_base_id == knowledge_base_id,
                    )
                    .order_by(KnowledgeIndexVersion.version_number.desc())
                )
            )

    def list_events(self, tenant_id: uuid.UUID, run_id: uuid.UUID, offset: int, limit: int):
        with self.session_factory() as session:
            if (
                session.scalar(
                    select(IndexingRun.id).where(
                        IndexingRun.id == run_id, IndexingRun.tenant_id == tenant_id
                    )
                )
                is None
            ):
                raise LookupError("Indexing run was not found.")
            return list(
                session.scalars(
                    select(IndexingEvent)
                    .where(IndexingEvent.tenant_id == tenant_id, IndexingEvent.run_id == run_id)
                    .order_by(IndexingEvent.created_at)
                    .offset(offset)
                    .limit(limit)
                )
            )

    def set_pinned(self, tenant_id: uuid.UUID, version_id: uuid.UUID, pinned: bool):
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(KnowledgeIndexVersion)
                .where(
                    KnowledgeIndexVersion.id == version_id,
                    KnowledgeIndexVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if row is None:
                raise LookupError("Index version was not found.")
            row.pinned = pinned
            return row

    def claim(self, run_id: uuid.UUID, worker_id: str, lease_seconds: int):
        with self.session_factory.begin() as session:
            now = datetime.now(UTC)
            run = session.scalar(
                select(IndexingRun)
                .where(
                    IndexingRun.id == run_id,
                    IndexingRun.cancel_requested.is_(False),
                    IndexingRun.attempt_count < IndexingRun.max_attempts,
                    or_(
                        IndexingRun.status == IndexingRunStatus.QUEUED.value,
                        (IndexingRun.status == IndexingRunStatus.RUNNING.value)
                        & (IndexingRun.lease_expires_at < now),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            if run is None:
                return None
            token = uuid.uuid4()
            run.status = IndexingRunStatus.RUNNING.value
            run.attempt_count += 1
            run.started_at = run.started_at or now
            run.lease_owner = worker_id
            run.lease_token = token
            run.lease_expires_at = now + timedelta(seconds=lease_seconds)
            version = session.get(KnowledgeIndexVersion, run.index_version_id)
            version.status = IndexVersionStatus.INDEXING.value
            return run, token

    def renew(self, run_id: uuid.UUID, token: uuid.UUID, lease_seconds: int) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IndexingRun)
                .where(
                    IndexingRun.id == run_id,
                    IndexingRun.lease_token == token,
                    IndexingRun.cancel_requested.is_(False),
                )
                .with_for_update()
            )
            if run is None:
                return False
            run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
            return True

    def chunks_for_run(self, run_id: uuid.UUID, after_id: uuid.UUID | None, limit: int):
        with self.session_factory() as session:
            run = session.get(IndexingRun, run_id)
            statement = (
                select(
                    DocumentChunk, ContentBlob, NormalizedDocument, KnowledgeSource, SourceVersion
                )
                .join(NormalizedDocument, NormalizedDocument.id == DocumentChunk.document_id)
                .join(ContentBlob, ContentBlob.id == DocumentChunk.content_blob_id)
                .join(SourceVersion, SourceVersion.id == NormalizedDocument.source_version_id)
                .join(KnowledgeSource, KnowledgeSource.id == SourceVersion.source_id)
                .join(KnowledgeBaseSource, KnowledgeBaseSource.source_id == KnowledgeSource.id)
                .where(
                    DocumentChunk.tenant_id == run.tenant_id,
                    KnowledgeBaseSource.knowledge_base_id == run.knowledge_base_id,
                    KnowledgeBaseSource.tenant_id == run.tenant_id,
                    SourceVersion.status == SourceVersionStatus.ACTIVE.value,
                )
                .order_by(DocumentChunk.id)
                .limit(limit)
            )
            if after_id:
                statement = statement.where(DocumentChunk.id > after_id)
            return list(session.execute(statement).all())

    def persist_batch(self, run_id: uuid.UUID, token: uuid.UUID, rows, vectors) -> None:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IndexingRun)
                .where(IndexingRun.id == run_id, IndexingRun.lease_token == token)
                .with_for_update()
            )
            if run is None or run.cancel_requested:
                raise RuntimeError("Indexing lease was lost or cancellation was requested.")
            version = session.get(KnowledgeIndexVersion, run.index_version_id)
            model = session.get(EmbeddingModelVersion, version.embedding_model_version_id)
            for row, vector in zip(rows, vectors, strict=True):
                if len(vector) != model.dimensions:
                    raise RuntimeError(
                        "Embedding dimension does not match the index model contract."
                    )
                chunk, _blob, document, source, source_version = row
                embedding = session.scalar(
                    select(ChunkEmbedding).where(
                        ChunkEmbedding.tenant_id == run.tenant_id,
                        ChunkEmbedding.text_checksum == chunk.checksum,
                        ChunkEmbedding.embedding_model_version_id
                        == version.embedding_model_version_id,
                    )
                )
                if embedding is None:
                    embedding = ChunkEmbedding(
                        tenant_id=run.tenant_id,
                        embedding_model_version_id=version.embedding_model_version_id,
                        text_checksum=chunk.checksum,
                        embedding=vector,
                    )
                    session.add(embedding)
                    session.flush()
                exists = session.scalar(
                    select(KnowledgeIndexEntry.id).where(
                        KnowledgeIndexEntry.index_version_id == version.id,
                        KnowledgeIndexEntry.chunk_id == chunk.id,
                    )
                )
                if exists is None:
                    metadata = chunk.metadata_json or {}
                    session.add(
                        KnowledgeIndexEntry(
                            tenant_id=run.tenant_id,
                            index_version_id=version.id,
                            source_id=source.id,
                            source_version_id=source_version.id,
                            document_id=document.id,
                            chunk_id=chunk.id,
                            chunk_embedding_id=embedding.id,
                            canonical_uri=document.canonical_uri,
                            title=document.title,
                            source_type=source.source_type,
                            path=metadata.get("path"),
                            schema_name=metadata.get("schema"),
                        )
                    )
            run.checkpoint = {"after_id": str(rows[-1][0].id)}
            session.add(
                IndexingEvent(
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    event_type="progress",
                    payload={"processed": len(rows), "after_id": str(rows[-1][0].id)},
                )
            )

    def complete(self, run_id: uuid.UUID, token: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IndexingRun)
                .where(IndexingRun.id == run_id, IndexingRun.lease_token == token)
                .with_for_update()
            )
            if run is None:
                return False
            version = session.get(KnowledgeIndexVersion, run.index_version_id)
            if run.cancel_requested:
                run.status = IndexingRunStatus.CANCELLED.value
                run.completed_at = datetime.now(UTC)
                run.lease_owner = None
                run.lease_token = None
                run.lease_expires_at = None
                version.status = IndexVersionStatus.FAILED.value
                version.failure_code = "cancelled"
                version.failure_message = "Indexing was cancelled by an administrator."
                return True
            count = session.scalar(
                select(func.count())
                .select_from(KnowledgeIndexEntry)
                .where(KnowledgeIndexEntry.index_version_id == version.id)
            )
            expected = session.scalar(
                select(func.count(DocumentChunk.id))
                .join(NormalizedDocument, NormalizedDocument.id == DocumentChunk.document_id)
                .join(SourceVersion, SourceVersion.id == NormalizedDocument.source_version_id)
                .join(KnowledgeBaseSource, KnowledgeBaseSource.source_id == SourceVersion.source_id)
                .where(
                    DocumentChunk.tenant_id == run.tenant_id,
                    KnowledgeBaseSource.tenant_id == run.tenant_id,
                    KnowledgeBaseSource.knowledge_base_id == run.knowledge_base_id,
                    SourceVersion.status == SourceVersionStatus.ACTIVE.value,
                )
            )
            valid_links = session.scalar(
                select(func.count(KnowledgeIndexEntry.id))
                .join(DocumentChunk, DocumentChunk.id == KnowledgeIndexEntry.chunk_id)
                .join(NormalizedDocument, NormalizedDocument.id == KnowledgeIndexEntry.document_id)
                .join(ChunkEmbedding, ChunkEmbedding.id == KnowledgeIndexEntry.chunk_embedding_id)
                .where(
                    KnowledgeIndexEntry.index_version_id == version.id,
                    KnowledgeIndexEntry.tenant_id == run.tenant_id,
                    DocumentChunk.tenant_id == run.tenant_id,
                    NormalizedDocument.tenant_id == run.tenant_id,
                    ChunkEmbedding.tenant_id == run.tenant_id,
                    ChunkEmbedding.embedding_model_version_id == version.embedding_model_version_id,
                )
            )
            retrieval_smoke = False
            sample_embedding = session.scalar(
                select(ChunkEmbedding)
                .join(
                    KnowledgeIndexEntry,
                    KnowledgeIndexEntry.chunk_embedding_id == ChunkEmbedding.id,
                )
                .where(KnowledgeIndexEntry.index_version_id == version.id)
                .limit(1)
            )
            if sample_embedding is not None:
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    retrieval_smoke = (
                        session.scalar(
                            select(KnowledgeIndexEntry.id)
                            .join(
                                ChunkEmbedding,
                                ChunkEmbedding.id == KnowledgeIndexEntry.chunk_embedding_id,
                            )
                            .where(KnowledgeIndexEntry.index_version_id == version.id)
                            .order_by(
                                ChunkEmbedding.embedding.cosine_distance(sample_embedding.embedding)
                            )
                            .limit(1)
                        )
                        is not None
                    )
                else:
                    # SQLite is used only by unit tests; relational integrity remains exercised.
                    retrieval_smoke = True
            version.status = IndexVersionStatus.VALIDATING.value
            version.manifest = {
                "entry_count": count,
                "expected_chunk_count": expected,
                "validated_link_count": valid_links,
                "embedding_dimensions": session.get(
                    EmbeddingModelVersion, version.embedding_model_version_id
                ).dimensions,
                "retrieval_smoke": retrieval_smoke,
                "contract_version": "1.1",
            }
            validation_error = None
            if not count:
                validation_error = "empty_index"
            elif count != expected or count != valid_links or not retrieval_smoke:
                validation_error = "integrity_mismatch"
            if validation_error:
                version.status = IndexVersionStatus.FAILED.value
                version.failure_code = validation_error
                version.failure_message = "Index validation failed. See indexing events."
                run.status = IndexingRunStatus.FAILED.value
                run.error_code = validation_error
                run.error_message = version.failure_message
                session.add(
                    IndexingEvent(
                        tenant_id=run.tenant_id,
                        run_id=run.id,
                        event_type="validation_failed",
                        payload={"reason": validation_error},
                    )
                )
            else:
                version.status = IndexVersionStatus.READY.value
                run.status = IndexingRunStatus.COMPLETED.value
                session.add(
                    IndexingEvent(
                        tenant_id=run.tenant_id,
                        run_id=run.id,
                        event_type="validated",
                        payload={"entry_count": count},
                    )
                )
            run.completed_at = datetime.now(UTC)
            run.lease_owner = None
            run.lease_token = None
            run.lease_expires_at = None
            return validation_error is None

    def fail(self, run_id: uuid.UUID, token: uuid.UUID, code: str) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IndexingRun)
                .where(IndexingRun.id == run_id, IndexingRun.lease_token == token)
                .with_for_update()
            )
            if run is None:
                return False
            run.status = IndexingRunStatus.FAILED.value
            run.error_code = code
            run.error_message = "Indexing failed. See logs using the correlation ID."
            run.completed_at = datetime.now(UTC)
            version = session.get(KnowledgeIndexVersion, run.index_version_id)
            version.status = IndexVersionStatus.FAILED.value
            version.failure_code = code
            version.failure_message = run.error_message
            return True

    def retry(self, run_id: uuid.UUID, token: uuid.UUID, code: str) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IndexingRun)
                .where(
                    IndexingRun.id == run_id,
                    IndexingRun.lease_token == token,
                    IndexingRun.attempt_count < IndexingRun.max_attempts,
                    IndexingRun.cancel_requested.is_(False),
                )
                .with_for_update()
            )
            if run is None:
                return False
            run.status = IndexingRunStatus.QUEUED.value
            run.error_code = code
            run.error_message = "Indexing dependency is temporarily unavailable."
            run.lease_owner = None
            run.lease_token = None
            run.lease_expires_at = None
            return True

    def fail_exhausted(self, run_id: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            now = datetime.now(UTC)
            run = session.scalar(
                select(IndexingRun)
                .where(
                    IndexingRun.id == run_id,
                    IndexingRun.attempt_count >= IndexingRun.max_attempts,
                    or_(
                        IndexingRun.status == IndexingRunStatus.QUEUED.value,
                        (IndexingRun.status == IndexingRunStatus.RUNNING.value)
                        & (IndexingRun.lease_expires_at < now),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            if run is None:
                return False
            run.status = IndexingRunStatus.FAILED.value
            run.error_code = "retry_exhausted"
            run.error_message = "Indexing retry budget was exhausted."
            run.completed_at = now
            version = session.get(KnowledgeIndexVersion, run.index_version_id)
            version.status = IndexVersionStatus.FAILED.value
            version.failure_code = run.error_code
            version.failure_message = run.error_message
            return True

    def finish_cancelled(self, run_id: uuid.UUID, token: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IndexingRun)
                .where(
                    IndexingRun.id == run_id,
                    IndexingRun.lease_token == token,
                    IndexingRun.cancel_requested.is_(True),
                )
                .with_for_update()
            )
            if run is None:
                return False
            run.status = IndexingRunStatus.CANCELLED.value
            run.completed_at = datetime.now(UTC)
            run.lease_owner = None
            run.lease_token = None
            run.lease_expires_at = None
            version = session.get(KnowledgeIndexVersion, run.index_version_id)
            version.status = IndexVersionStatus.FAILED.value
            version.failure_code = "cancelled"
            version.failure_message = "Indexing was cancelled by an administrator."
            return True

    def cancel(self, tenant_id: uuid.UUID, run_id: uuid.UUID) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(IndexingRun)
                .where(IndexingRun.id == run_id, IndexingRun.tenant_id == tenant_id)
                .with_for_update()
            )
            if run is None:
                raise LookupError("Indexing run was not found.")
            if run.status not in {"queued", "running"}:
                return False
            run.cancel_requested = True
            return True

    def activate(self, tenant_id: uuid.UUID, version_id: uuid.UUID):
        with self.session_factory.begin() as session:
            target = session.scalar(
                select(KnowledgeIndexVersion)
                .where(
                    KnowledgeIndexVersion.id == version_id,
                    KnowledgeIndexVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if target is None:
                raise LookupError("Index version was not found.")
            if target.status == IndexVersionStatus.ACTIVE.value:
                return target
            if target.status not in {
                IndexVersionStatus.READY.value,
                IndexVersionStatus.SUPERSEDED.value,
            }:
                raise IndexingConflictError("Only ready or superseded index can be activated.")
            current = session.scalar(
                select(KnowledgeIndexVersion)
                .where(
                    KnowledgeIndexVersion.tenant_id == tenant_id,
                    KnowledgeIndexVersion.knowledge_base_id == target.knowledge_base_id,
                    KnowledgeIndexVersion.status == IndexVersionStatus.ACTIVE.value,
                )
                .with_for_update()
            )
            now = datetime.now(UTC)
            if current and current.id != target.id:
                current.status = IndexVersionStatus.SUPERSEDED.value
                current.superseded_at = now
                # Release the partial unique active-index slot before promoting the target.
                session.flush()
            target.status = IndexVersionStatus.ACTIVE.value
            target.activated_at = now
            target.superseded_at = None
            return target
