import json

from app import observability


def test_structured_events_allowlist_fields_and_redact_sensitive_values(monkeypatch):
    records: list[str] = []
    monkeypatch.setattr(observability.logger, "info", records.append)
    observability.log_event(
        "worker.completed\nunsafe",
        correlation_id="correlation-1",
        job_id="job-1",
        status="completed",
        password="plain-password",
        authorization="Bearer secret-token",
        prompt="private prompt",
        content="retrieved content",
        query="select secret",
        database_url="postgresql://user:password@example/db",
        unexpected="must not be logged",
    )

    payload = json.loads(records[-1])
    assert payload == {
        "event_type": "worker.completed_unsafe",
        "correlation_id": "correlation-1",
        "job_id": "job-1",
        "status": "completed",
    }
    rendered = records[-1]
    for secret in (
        "plain-password",
        "secret-token",
        "private prompt",
        "retrieved content",
        "select secret",
        "postgresql://",
        "must not be logged",
    ):
        assert secret not in rendered


def test_metrics_sanitize_dynamic_names_without_user_controlled_labels():
    before = observability.COUNTERS["queue_age_secret"]
    observability.metric("queue age/secret")
    assert observability.COUNTERS["queue_age_secret"] == before + 1
