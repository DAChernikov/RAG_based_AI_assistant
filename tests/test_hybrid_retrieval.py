from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.retrieval.contracts import RetrievalFilters, RetrievedChunk
from app.retrieval.hybrid import HybridRetrievalRepository, HybridRetriever


class Embeddings:
    def __init__(self):
        self.calls = []

    async def embed_for_index(self, tenant_id, knowledge_base_id, texts):
        self.calls.append((tenant_id, knowledge_base_id, texts))
        return [[1.0, 0.0]]


class Repository:
    def __init__(self):
        self.calls = []

    def resolve_knowledge_base(self, tenant_id, knowledge_base_id):
        if knowledge_base_id:
            return knowledge_base_id
        return uuid.UUID(int=1)

    def search(self, tenant_id, knowledge_base_id, query, vector, top_k, filters):
        self.calls.append((tenant_id, knowledge_base_id, query, vector, top_k, filters))
        return [
            RetrievedChunk(
                chunk_id=uuid.UUID(int=2),
                source_id=uuid.UUID(int=3),
                source_version_id=uuid.UUID(int=4),
                document_id=uuid.UUID(int=5),
                source_type="git",
                title="README",
                canonical_uri="git://repo/README.md",
                text="Python функция process_event",
                score=0.03,
                dense_rank=1,
                sparse_rank=2,
                metadata={"path": "README.md"},
            )
        ]


class Reranker:
    async def rerank(self, query, documents, top_k):
        rows = documents[:top_k]
        rows[0]["rerank_score"] = 0.9
        return rows


@pytest.mark.asyncio
async def test_hybrid_retriever_uses_active_index_embedding_and_provenance():
    repository = Repository()
    embeddings = Embeddings()
    retriever = HybridRetriever(repository, embeddings, Reranker())
    tenant_id = uuid.uuid4()
    kb_id = uuid.uuid4()
    resolved = await retriever.resolve_knowledge_base(tenant_id, kb_id)
    rows = await retriever.search(
        tenant_id,
        resolved,
        "Как работает process_event?",
        top_k=3,
        filters=RetrievalFilters(source_types=["git"]),
    )
    assert embeddings.calls[0][0:2] == (tenant_id, kb_id)
    assert repository.calls[0][5].source_types == ["git"]
    assert rows[0]["uri"] == "git://repo/README.md"
    assert rows[0]["source_version_id"] == str(uuid.UUID(int=4))
    assert rows[0]["rerank_score"] == 0.9


@pytest.mark.parametrize(
    "query",
    ["настройка очереди", "queue configuration", "process_event HTTPStatusError"],
)
def test_lexical_strategy_compiles_ru_en_and_technical_configurations(query):
    rank, predicate = HybridRetrievalRepository.lexical_expressions(query)
    statement = select(rank).where(predicate)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "to_tsvector" in sql
    assert {"russian", "english", "simple"}.issubset(set(compiled.params.values()))
    assert "@@" in sql


class FakeSession:
    def __init__(self, *, active=True, rows=None, scalar_value=None, scalar_rows=None):
        self.active = active
        self.rows = rows or []
        self.scalar_value = scalar_value
        self.scalar_rows = scalar_rows or []
        self.executions = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def scalar(self, _statement):
        if self.scalar_value is not None:
            return self.scalar_value
        return uuid.UUID(int=10) if self.active else None

    def scalars(self, _statement):
        return self.scalar_rows

    def execute(self, _statement):
        result = self.rows[self.executions]
        self.executions += 1
        return SimpleNamespace(all=lambda: result)


def test_hybrid_repository_resolves_default_and_enforces_tenant_boundary():
    expected = uuid.uuid4()
    repository = HybridRetrievalRepository(
        lambda: FakeSession(scalar_value=expected, scalar_rows=[expected])
    )
    assert repository.resolve_knowledge_base(uuid.uuid4(), expected) == expected
    repository = HybridRetrievalRepository(lambda: FakeSession(scalar_rows=[expected]))
    assert repository.resolve_knowledge_base(uuid.uuid4(), None) == expected
    repository = HybridRetrievalRepository(lambda: FakeSession(active=False, scalar_rows=[]))
    with pytest.raises(Exception, match="knowledge_base_id is required"):
        repository.resolve_knowledge_base(uuid.uuid4(), None)
    repository = HybridRetrievalRepository(lambda: FakeSession(active=False))
    with pytest.raises(Exception, match="not found"):
        repository.resolve_knowledge_base(uuid.uuid4(), uuid.uuid4())


def test_hybrid_repository_search_rrf_filters_and_empty_active_index():
    dense_chunk = SimpleNamespace(id=uuid.UUID(int=20), metadata_json={"lang": "ru"})
    sparse_chunk = SimpleNamespace(id=uuid.UUID(int=21), metadata_json={"lang": "en"})
    shared_chunk = SimpleNamespace(id=uuid.UUID(int=22), metadata_json={"path": "src/a.py"})

    def entry(seed, source_type):
        return SimpleNamespace(
            source_id=uuid.UUID(int=seed),
            source_version_id=uuid.UUID(int=seed + 10),
            document_id=uuid.UUID(int=seed + 20),
            source_type=source_type,
            title=f"title-{seed}",
            canonical_uri=f"local://{seed}",
        )

    dense = [
        (entry(30, "website"), dense_chunk, SimpleNamespace(content="русский"), object(), 0.1),
        (entry(31, "git"), shared_chunk, SimpleNamespace(content="process_event"), object(), 0.2),
    ]
    sparse = [
        (entry(31, "git"), shared_chunk, SimpleNamespace(content="process_event"), object(), 0.8),
        (entry(32, "website"), sparse_chunk, SimpleNamespace(content="english"), object(), 0.7),
    ]
    session = FakeSession(rows=[dense, sparse])
    repository = HybridRetrievalRepository(lambda: session)
    rows = repository.search(
        uuid.uuid4(),
        uuid.uuid4(),
        "процесс process_event",
        [0.1, 0.2],
        3,
        RetrievalFilters(
            source_ids=[uuid.uuid4()],
            source_types=["git"],
            path_prefix="src/",
            schema_name="public",
        ),
    )
    assert [row.chunk_id for row in rows] == [shared_chunk.id, dense_chunk.id, sparse_chunk.id]
    assert rows[0].dense_rank == 2
    assert rows[0].sparse_rank == 1
    assert rows[0].metadata == {"path": "src/a.py"}

    empty = HybridRetrievalRepository(lambda: FakeSession(active=False))
    assert empty.search(uuid.uuid4(), uuid.uuid4(), "q", [0.0], 5, RetrievalFilters()) == []


@pytest.mark.asyncio
async def test_hybrid_retriever_supports_plain_embedding_and_empty_results():
    class PlainEmbeddings:
        async def embed(self, _texts):
            return [[0.0, 1.0]]

    repository = Repository()
    repository.search = lambda *_args: []
    retriever = HybridRetriever(repository, PlainEmbeddings(), Reranker())
    assert await retriever.search(uuid.uuid4(), uuid.uuid4(), "empty") == []
