import uuid

from app.api.services.router_service import RouterService


def test_route_plan_can_select_multiple_targets():
    knowledge_base_id = uuid.uuid4()
    plan = RouterService().plan(
        "Show the Python repository code and write a SQL query for revenue",
        knowledge_base_id,
    )

    assert plan.knowledge_base_id == knowledge_base_id
    assert plan.retrieval_targets == ["documentation", "code", "database_schema"]
    assert plan.requires_sql is True
    assert plan.contract_version == "1.0"


def test_route_plan_has_safe_documentation_fallback():
    plan = RouterService().plan("What is Apache Spark?", uuid.uuid4())

    assert plan.retrieval_targets == ["documentation"]
    assert plan.requires_sql is False


def test_explicit_mode_is_honored_without_becoming_exclusive():
    plan = RouterService().plan("Explain the API", uuid.uuid4(), requested_mode="sql")

    assert "documentation" in plan.retrieval_targets
    assert "code" in plan.retrieval_targets
    assert "database_schema" in plan.retrieval_targets
