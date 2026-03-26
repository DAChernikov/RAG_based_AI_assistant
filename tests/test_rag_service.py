import pytest

from app.api.services.rag_service import RAGService


class DummyRetriever:
    def search(
        self,
        query: str,
        top_k: int = 5,
        preferred_sources: list[str] | None = None,
        source_boosts: dict[str, float] | None = None,
    ):
        return [
            {
                "doc_id": "doc1",
                "source": "spark_docs",
                "title": "index.html",
                "text": (
                    "Apache Spark is a unified analytics engine " "for large-scale data processing."
                ),
                "score": 0.9,
                "raw_score": 0.9,
            },
            {
                "doc_id": "doc2",
                "source": "spark_docs",
                "title": "overview.html",
                "text": "Spark supports SQL, MLlib, GraphX, and Structured Streaming.",
                "score": 0.8,
                "raw_score": 0.8,
            },
        ]


class DummyLLM:
    def is_configured(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_rag_service_returns_answer_and_retrieved():
    service = RAGService(retriever=DummyRetriever(), llm_service=DummyLLM())

    result = await service.ask(
        question="What is Apache Spark?",
        top_k=2,
        max_new_tokens=128,
        mode="rag_docs",
    )

    assert result["mode"] == "rag_docs"
    assert result["question"] == "What is Apache Spark?"
    assert "Apache Spark" in result["answer"]
    assert len(result["retrieved"]) == 2
    assert result["confidence"] is not None
    assert result["confidence"]["top_k"] == 2
