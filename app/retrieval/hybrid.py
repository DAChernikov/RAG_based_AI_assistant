from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.concurrency import api_blocking_io
from app.embeddings.client import EmbeddingClient
from app.retrieval.contracts import RetrievalFilters, RetrievedChunk
from app.state.models import (
    ChunkEmbedding,
    ContentBlob,
    DocumentChunk,
    IndexVersionStatus,
    KnowledgeBase,
    KnowledgeIndexEntry,
    KnowledgeIndexVersion,
)


class KnowledgeBaseAccessError(RuntimeError):
    pass


class HybridRetrievalRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    @staticmethod
    def lexical_expressions(query: str):
        """Return mixed RU/EN/technical FTS rank and predicate expressions."""
        simple_document = func.to_tsvector("simple", ContentBlob.content)
        english_document = func.to_tsvector("english", ContentBlob.content)
        russian_document = func.to_tsvector("russian", ContentBlob.content)
        simple_query = func.websearch_to_tsquery("simple", query)
        english_query = func.websearch_to_tsquery("english", query)
        russian_query = func.websearch_to_tsquery("russian", query)
        return (
            func.greatest(
                func.ts_rank_cd(simple_document, simple_query),
                func.ts_rank_cd(english_document, english_query),
                func.ts_rank_cd(russian_document, russian_query),
            ),
            or_(
                simple_document.op("@@")(simple_query),
                english_document.op("@@")(english_query),
                russian_document.op("@@")(russian_query),
            ),
        )

    def resolve_knowledge_base(
        self, tenant_id: uuid.UUID, knowledge_base_id: uuid.UUID | None
    ) -> uuid.UUID:
        with self.session_factory() as session:
            statement = select(KnowledgeBase.id).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.is_enabled.is_(True),
            )
            if knowledge_base_id:
                found = session.scalar(statement.where(KnowledgeBase.id == knowledge_base_id))
                if found is None:
                    raise KnowledgeBaseAccessError("Knowledge base was not found.")
                return found
            rows = list(session.scalars(statement.order_by(KnowledgeBase.created_at).limit(2)))
            if len(rows) != 1:
                raise KnowledgeBaseAccessError(
                    "knowledge_base_id is required unless exactly one knowledge base is enabled."
                )
            return rows[0]

    def search(
        self,
        tenant_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        query: str,
        vector: list[float],
        top_k: int,
        filters: RetrievalFilters,
    ) -> list[RetrievedChunk]:
        with self.session_factory() as session:
            active_id = session.scalar(
                select(KnowledgeIndexVersion.id).where(
                    KnowledgeIndexVersion.tenant_id == tenant_id,
                    KnowledgeIndexVersion.knowledge_base_id == knowledge_base_id,
                    KnowledgeIndexVersion.status == IndexVersionStatus.ACTIVE.value,
                )
            )
            if active_id is None:
                return []
            base = (
                select(
                    KnowledgeIndexEntry,
                    DocumentChunk,
                    ContentBlob,
                    ChunkEmbedding,
                )
                .join(DocumentChunk, DocumentChunk.id == KnowledgeIndexEntry.chunk_id)
                .join(ContentBlob, ContentBlob.id == DocumentChunk.content_blob_id)
                .join(ChunkEmbedding, ChunkEmbedding.id == KnowledgeIndexEntry.chunk_embedding_id)
                .where(
                    KnowledgeIndexEntry.tenant_id == tenant_id,
                    KnowledgeIndexEntry.index_version_id == active_id,
                )
            )
            if filters.source_ids:
                base = base.where(KnowledgeIndexEntry.source_id.in_(filters.source_ids))
            if filters.source_types:
                base = base.where(KnowledgeIndexEntry.source_type.in_(filters.source_types))
            if filters.path_prefix:
                base = base.where(KnowledgeIndexEntry.path.startswith(filters.path_prefix))
            if filters.schema_name:
                base = base.where(KnowledgeIndexEntry.schema_name == filters.schema_name)

            candidate_limit = min(max(top_k * 5, 20), 200)
            distance = ChunkEmbedding.embedding.cosine_distance(vector)
            dense = session.execute(
                base.add_columns(distance.label("distance"))
                .order_by(distance)
                .limit(candidate_limit)
            ).all()
            rank, lexical_match = self.lexical_expressions(query)
            sparse = session.execute(
                base.add_columns(rank.label("rank"))
                .where(lexical_match)
                .order_by(rank.desc(), DocumentChunk.id)
                .limit(candidate_limit)
            ).all()

            scores: dict[uuid.UUID, float] = defaultdict(float)
            records = {}
            dense_ranks = {}
            sparse_ranks = {}
            for position, row in enumerate(dense, 1):
                entry, chunk, blob, _embedding, _distance = row
                scores[chunk.id] += 1.0 / (60 + position)
                records[chunk.id] = (entry, chunk, blob)
                dense_ranks[chunk.id] = position
            for position, row in enumerate(sparse, 1):
                entry, chunk, blob, _embedding, _rank = row
                scores[chunk.id] += 1.0 / (60 + position)
                records[chunk.id] = (entry, chunk, blob)
                sparse_ranks[chunk.id] = position
            ordered = sorted(scores, key=lambda key: (-scores[key], str(key)))[:top_k]
            return [
                RetrievedChunk(
                    chunk_id=chunk_id,
                    source_id=records[chunk_id][0].source_id,
                    source_version_id=records[chunk_id][0].source_version_id,
                    document_id=records[chunk_id][0].document_id,
                    source_type=records[chunk_id][0].source_type,
                    title=records[chunk_id][0].title,
                    canonical_uri=records[chunk_id][0].canonical_uri,
                    text=records[chunk_id][2].content,
                    score=scores[chunk_id],
                    dense_rank=dense_ranks.get(chunk_id),
                    sparse_rank=sparse_ranks.get(chunk_id),
                    metadata=records[chunk_id][1].metadata_json,
                )
                for chunk_id in ordered
            ]


class HybridRetriever:
    def __init__(self, repository, embeddings: EmbeddingClient | Any, reranker=None):
        self.repository = repository
        self.embeddings = embeddings
        self.reranker = reranker

    async def resolve_knowledge_base(self, tenant_id, knowledge_base_id):
        return await api_blocking_io.call(
            self.repository.resolve_knowledge_base, tenant_id, knowledge_base_id
        )

    async def search(
        self,
        tenant_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        query: str,
        *,
        top_k: int = 5,
        filters: RetrievalFilters | None = None,
    ) -> list[dict]:
        if hasattr(self.embeddings, "embed_for_index"):
            vector = (await self.embeddings.embed_for_index(tenant_id, knowledge_base_id, [query]))[
                0
            ]
        else:
            vector = (await self.embeddings.embed([query]))[0]
        rows = await api_blocking_io.call(
            self.repository.search,
            tenant_id,
            knowledge_base_id,
            query,
            vector,
            top_k,
            filters or RetrievalFilters(),
        )
        documents = [
            {
                "doc_id": str(item.chunk_id),
                "source": item.source_type,
                "source_id": str(item.source_id),
                "source_version_id": str(item.source_version_id),
                "document_id": str(item.document_id),
                "score": item.score,
                "title": item.title,
                "uri": item.canonical_uri,
                "text": item.text,
                "metadata": item.metadata,
                "dense_rank": item.dense_rank,
                "sparse_rank": item.sparse_rank,
            }
            for item in rows
        ]
        if self.reranker and documents:
            return await self.reranker.rerank(query, documents, top_k)
        return documents
