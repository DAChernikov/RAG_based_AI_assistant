from app.api.services.router_service import RouterService


def test_route_explicit_sql():
    service = RouterService()
    assert service.route("What is Apache Spark?", requested_mode="sql") == "sql"


def test_route_explicit_rag():
    service = RouterService()
    assert service.route("What is Apache Spark?", requested_mode="rag") == "rag"


def test_route_auto_sql():
    service = RouterService()
    result = service.route("Write SQL query for revenue by day")
    assert result == "sql"


def test_route_auto_rag():
    service = RouterService()
    result = service.route("What is Apache Spark?")
    assert result == "rag"
