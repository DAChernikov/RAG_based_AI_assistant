import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.state.models import Answer, Base, InferenceJob, Tenant, User
from app.state.repositories import ApplicationRepository, IdempotencyConflictError


@pytest.fixture
def repository():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug="development", name="Development")
        session.add(tenant)
        session.flush()
        user = User(
            tenant_id=tenant.id,
            external_id="development-user",
            display_name="Development User",
        )
        session.add(user)
        session.flush()
        identity = (tenant.id, user.id)
    return ApplicationRepository(factory), factory, identity


def test_repository_persists_job_answer_sources_and_conversation(repository):
    repo, factory, (tenant_id, user_id) = repository
    payload = {"question": "What is Spark?", "mode": "rag_docs"}
    created = repo.create_job(
        tenant_id=tenant_id,
        user_id=user_id,
        question=payload["question"],
        request_payload=payload,
        contract_version="1.0",
        max_attempts=3,
    )
    repo.mark_running(created.job.id)
    repo.complete_job(
        created.job.id,
        result={
            "answer": "Spark is an analytics engine.",
            "mode": "rag_docs",
            "confidence": {"top1_score": 0.9},
            "retrieved": [{"doc_id": "spark", "source": "docs", "title": "Spark", "score": 0.9}],
        },
        model_name="fake-model",
    )

    stored = repo.get_job(created.job.id)
    history = repo.conversation_history(created.job.conversation_id, tenant_id, user_id)

    assert stored.status == "completed"
    assert stored.answer.answer_text.startswith("Spark")
    assert len(stored.answer.sources) == 1
    assert [message["role"] for message in history["messages"]] == ["user", "assistant"]
    with factory() as session:
        assert session.query(InferenceJob).count() == 1
        assert session.query(Answer).count() == 1


def test_idempotency_reuses_same_payload_and_rejects_conflict(repository):
    repo, _, (tenant_id, user_id) = repository
    common = dict(
        tenant_id=tenant_id,
        user_id=user_id,
        question="q",
        contract_version="1.0",
        max_attempts=3,
        idempotency_key="same-key",
    )
    first = repo.create_job(request_payload={"question": "q"}, **common)
    second = repo.create_job(request_payload={"question": "q"}, **common)

    assert first.created is True
    assert second.created is False
    assert first.job.id == second.job.id

    with pytest.raises(IdempotencyConflictError):
        repo.create_job(request_payload={"question": "different"}, **common)


def test_duplicate_completion_creates_one_answer(repository):
    repo, factory, (tenant_id, user_id) = repository
    created = repo.create_job(
        tenant_id=tenant_id,
        user_id=user_id,
        question="q",
        request_payload={"question": "q"},
        contract_version="1.0",
        max_attempts=3,
    )
    result = {"answer": "one", "mode": "rag_docs", "retrieved": []}
    first = repo.complete_job(created.job.id, result=result, model_name="fake")
    second = repo.complete_job(created.job.id, result=result, model_name="fake")

    assert first.id == second.id
    with factory() as session:
        assert session.query(Answer).count() == 1
