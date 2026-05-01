import pytest

from app.api.services.sql_service import SQLService


class DummyRetriever:
    def __init__(self):
        self.last_kwargs = None

    def search(
        self, query, top_k=5, preferred_sources=None, source_boosts=None, source_filter=None
    ):
        self.last_kwargs = {
            "query": query,
            "top_k": top_k,
            "source_filter": source_filter,
        }
        return [
            {
                "doc_id": "database_schema::rag_kg.orders",
                "source": "database_schema",
                "title": "rag_kg.orders",
                "text": "TABLE rag_kg.orders\nColumns:\n- customer_id\n- total_amount",
                "score": 0.9,
                "raw_score": 0.9,
                "metadata": {
                    "schema": "rag_kg",
                    "table": "orders",
                    "columns": ["customer_id", "total_amount"],
                },
            },
            {
                "doc_id": "database_schema::rag_kg.customers",
                "source": "database_schema",
                "title": "rag_kg.customers",
                "text": "TABLE rag_kg.customers\nColumns:\n- customer_id\n- segment",
                "score": 0.8,
                "raw_score": 0.8,
                "metadata": {
                    "schema": "rag_kg",
                    "table": "customers",
                    "columns": ["customer_id", "segment"],
                },
            },
        ]


class DummyLLM:
    def is_configured(self):
        return True

    async def generate(self, *, prompt, max_new_tokens, temperature=None):
        return """EXPLANATION:
Join orders to customers and aggregate total_amount by segment.

SQL:
SELECT
    c.segment,
    SUM(o.total_amount) AS revenue
FROM rag_kg.orders o
JOIN rag_kg.customers c
    ON o.customer_id = c.customer_id
GROUP BY c.segment
ORDER BY revenue DESC"""


@pytest.mark.asyncio
async def test_sql_service_uses_database_schema_source_filter():
    retriever = DummyRetriever()
    service = SQLService(retriever=retriever, llm_service=DummyLLM())

    result = await service.ask(question="Show revenue by customer segment", top_k=10)

    assert result["mode"] == "sql"
    assert retriever.last_kwargs["source_filter"] == ["database_schema"]
    assert retriever.last_kwargs["top_k"] == 10
    assert "SELECT" in result["answer"]
    assert result["confidence"]["validation"]["is_valid"] is True
    assert "rag_kg.orders" in result["confidence"]["validation"]["used_tables"]
    assert "rag_kg.customers" in result["confidence"]["validation"]["used_tables"]


@pytest.mark.asyncio
async def test_sql_validation_rejects_forbidden_keyword():
    service = SQLService(retriever=DummyRetriever(), llm_service=DummyLLM())
    docs = DummyRetriever().search("q", source_filter=["database_schema"])

    validation = await service.validate_sql("SQL:\nDROP TABLE rag_kg.orders", docs)

    assert validation.is_valid is False
    assert any("Forbidden" in err for err in validation.errors)
