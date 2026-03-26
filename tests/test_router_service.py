from app.api.services.router_service import RouterService


def test_route_explicit_sql():
    service = RouterService()
    assert service.route("What is Apache Spark?", requested_mode="sql") == "sql"


def test_route_explicit_rag_code():
    service = RouterService()
    assert service.route("How do I read JSON in Python?", requested_mode="rag_code") == "rag_code"


def test_route_auto_sql():
    service = RouterService()
    result = service.route("Write SQL query for revenue by day")
    assert result == "sql"


def test_route_auto_rag_code():
    service = RouterService()
    result = service.route("How do I safely get a nested value from a Python dict?")
    assert result == "rag_code"


def test_route_auto_rag_docs():
    service = RouterService()
    result = service.route("What is Apache Spark?")
    assert result == "rag_docs"
