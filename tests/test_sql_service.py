import pytest

from app.api.services.sql_service import SQLService


class DummyRetriever:
    def search(
        self,
        query: str,
        top_k: int = 10,
        preferred_sources: list[str] | None = None,
        source_boosts: dict[str, float] | None = None,
        source_filter: list[str] | None = None,
    ):
        assert source_filter == ["database_schema"]
        return [
            {
                "doc_id": "database_schema::rag_kg.orders",
                "source": "database_schema",
                "title": "rag_kg.orders",
                "text": "TABLE rag_kg.orders\nColumns:\n- customer_id\n- total_amount",
                "metadata": {"schema": "rag_kg", "table": "orders"},
                "score": 0.9,
                "raw_score": 0.9,
            },
            {
                "doc_id": "database_schema::rag_kg.customers",
                "source": "database_schema",
                "title": "rag_kg.customers",
                "text": "TABLE rag_kg.customers\nColumns:\n- customer_id\n- segment",
                "metadata": {"schema": "rag_kg", "table": "customers"},
                "score": 0.8,
                "raw_score": 0.8,
            },
        ]


class DummyLLM:
    def is_configured(self) -> bool:
        return True

    async def generate(self, *, prompt: str, max_new_tokens: int, temperature=None) -> str:
        return (
            "EXPLANATION:\n"
            "Join orders with customers and aggregate revenue by segment.\n"
            "SQL:\n"
            "SELECT c.segment, SUM(o.total_amount) AS revenue "
            "FROM rag_kg.orders o "
            "JOIN rag_kg.customers c ON o.customer_id = c.customer_id "
            "GROUP BY c.segment "
            "ORDER BY revenue DESC;"
        )


@pytest.mark.asyncio
async def test_sql_service_uses_database_schema_filter_and_validates_sql():
    service = SQLService(retriever=DummyRetriever(), llm_service=DummyLLM())

    result = await service.ask("Show revenue by customer segment", top_k=10)

    assert result["mode"] == "sql"
    assert "SELECT" in result["answer"]
    assert len(result["retrieved"]) == 2
    assert result["confidence"]["validation"]["is_valid"] is True
