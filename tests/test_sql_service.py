import pytest

from app.api.services.sql_service import SQLService


class DummyRetriever:
    def __init__(self):
        self.last_kwargs = None

    def search(self, query, top_k=10, source_filter=None, **kwargs):
        self.last_kwargs = {
            "query": query,
            "top_k": top_k,
            "source_filter": source_filter,
            **kwargs,
        }

        return [
            {
                "doc_id": "database_schema::rag_kg.customers",
                "source": "database_schema",
                "title": "rag_kg.customers",
                "text": (
                    "TABLE rag_kg.customers\n"
                    "Description: Customer dimension.\n"
                    "Columns:\n"
                    "- customer_id (bigint): Customer identifier.\n"
                    "- segment (text): Customer segment.\n"
                ),
                "metadata": {
                    "schema": "rag_kg",
                    "table": "customers",
                    "columns": ["customer_id", "segment"],
                },
                "score": 0.9,
                "raw_score": 0.9,
            },
            {
                "doc_id": "database_schema::rag_kg.orders",
                "source": "database_schema",
                "title": "rag_kg.orders",
                "text": (
                    "TABLE rag_kg.orders\n"
                    "Description: Orders fact table.\n"
                    "Columns:\n"
                    "- order_id (bigint): Order identifier.\n"
                    "- customer_id (bigint): Customer identifier.\n"
                    "- total_amount (numeric): Total order amount.\n"
                ),
                "metadata": {
                    "schema": "rag_kg",
                    "table": "orders",
                    "columns": ["order_id", "customer_id", "total_amount"],
                },
                "score": 0.8,
                "raw_score": 0.8,
            },
        ]


class FakeModelClient:
    def is_configured(self) -> bool:
        return True

    async def generate(self, *args, **kwargs):
        return (
            "EXPLANATION:\n"
            "This query calculates revenue by customer segment.\n\n"
            "SQL:\n"
            "SELECT\n"
            "  c.segment,\n"
            "  SUM(o.total_amount) AS total_revenue\n"
            "FROM rag_kg.orders AS o\n"
            "JOIN rag_kg.customers AS c\n"
            "  ON o.customer_id = c.customer_id\n"
            "GROUP BY c.segment\n"
            "ORDER BY total_revenue DESC;"
        )


@pytest.mark.asyncio
async def test_sql_service_uses_database_schema_source_filter(monkeypatch):
    monkeypatch.setattr(
        "app.api.services.sql_service.settings.sql_enable_explain_validation",
        False,
    )

    retriever = DummyRetriever()
    service = SQLService(retriever=retriever, llm_service=FakeModelClient())

    result = await service.ask(question="Show revenue by customer segment", top_k=10)

    assert result["mode"] == "sql"
    assert retriever.last_kwargs["source_filter"] == ["database_schema"]
    assert retriever.last_kwargs["top_k"] == 10
    assert "SELECT" in result["answer"]
    assert result["confidence"]["validation"]["is_valid"] is True


@pytest.mark.asyncio
async def test_sql_validation_rejects_forbidden_keyword():
    service = SQLService(retriever=DummyRetriever(), llm_service=FakeModelClient())
    docs = DummyRetriever().search("q", source_filter=["database_schema"])

    validation = await service.validate_sql("SQL:\nDROP TABLE rag_kg.orders", docs)

    assert validation.is_valid is False
    assert any("Forbidden" in err for err in validation.errors)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT order_id FROM rag_kg.orders",
        "WITH recent AS (SELECT order_id FROM rag_kg.orders) SELECT order_id FROM recent",
        (
            "WITH nested AS (WITH inner_cte AS (SELECT order_id FROM rag_kg.orders) "
            "SELECT order_id FROM inner_cte) SELECT order_id FROM nested"
        ),
        ("SELECT order_id FROM rag_kg.orders UNION " "SELECT customer_id FROM rag_kg.customers"),
        "SELECT COUNT(order_id) AS count_orders FROM rag_kg.orders",
        "SELECT order_id FROM rag_kg.orders /* DROP TABLE rag_kg.orders */",
    ],
)
async def test_sql_validation_accepts_read_only_ast_shapes_and_enforces_limit(sql, monkeypatch):
    monkeypatch.setattr(
        "app.api.services.sql_service.settings.sql_enable_explain_validation", False
    )
    service = SQLService(DummyRetriever(), FakeModelClient())
    docs = DummyRetriever().search("q")
    result = await service.validate_sql(sql, docs)
    assert result.is_valid, result.errors
    assert "LIMIT 500" in result.sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sql", "error"),
    [
        ("SELECT 1; SELECT 2", "Exactly one"),
        ("UPDATE rag_kg.orders SET total_amount = 0", "Only a read-only"),
        ("SELECT missing FROM rag_kg.orders", "Column is not present"),
        ("SELECT order_id FROM private.unknown", "Table is not present"),
    ],
)
async def test_sql_validation_rejects_bypass_and_unknown_schema(sql, error, monkeypatch):
    monkeypatch.setattr(
        "app.api.services.sql_service.settings.sql_enable_explain_validation", False
    )
    service = SQLService(DummyRetriever(), FakeModelClient())
    result = await service.validate_sql(sql, DummyRetriever().search("q"))
    assert not result.is_valid
    assert any(error in item for item in result.errors)


@pytest.mark.asyncio
async def test_sql_validation_complexity_and_explain_failure_are_sanitized(monkeypatch):
    service = SQLService(
        DummyRetriever(), FakeModelClient(), explain_parameters=lambda: parameters()
    )
    docs = DummyRetriever().search("q")
    monkeypatch.setattr(service, "MAX_AST_NODES", 2)
    too_complex = await service.validate_sql("SELECT order_id FROM rag_kg.orders", docs)
    assert any("complexity" in item for item in too_complex.errors)

    async def parameters():
        return {"host": "db.internal", "password": "never-log-this"}

    async def fail(_function, *_args, **_kwargs):
        import psycopg

        raise psycopg.OperationalError("connection rejected with secret")

    service.MAX_AST_NODES = 500
    monkeypatch.setattr("app.api.services.sql_service.settings.sql_enable_explain_validation", True)
    monkeypatch.setattr("app.api.services.sql_service.api_blocking_io.call", fail)
    explained = await service.validate_sql("SELECT order_id FROM rag_kg.orders", docs)
    assert explained.explain_error == "Database rejected SQL during read-only EXPLAIN."
    assert "secret" not in str(explained.to_dict())
