import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_principal, get_runtime_state
from app.api.routes import ask, jobs
from app.auth.security import Principal
from app.inference.contracts import InferenceJobContract


def build_client(runtime):
    app = FastAPI()
    app.include_router(ask.router)
    app.include_router(jobs.router)
    app.dependency_overrides[get_runtime_state] = lambda: runtime
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="test-user",
        role="user",
        auth_method="test",
        scopes=frozenset({"*"}),
    )
    return TestClient(app)


class FakeQueuedApplication:
    def __init__(self):
        self.job_id = uuid.uuid4()
        self.conversation_id = uuid.uuid4()
        self.contract = InferenceJobContract(
            job_id=self.job_id,
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            conversation_id=self.conversation_id,
            message_id=uuid.uuid4(),
            knowledge_base_id=uuid.uuid4(),
            question="queued",
        )
        self.job = SimpleNamespace(
            id=self.job_id,
            conversation_id=self.conversation_id,
            status="queued",
        )

    async def submit(
        self,
        payload,
        *,
        tenant_id,
        user_id,
        idempotency_key,
        conversation_id,
    ):
        self.idempotency_key = idempotency_key
        return SimpleNamespace(job=self.job, created=True), self.contract

    async def wait_for_terminal(self, job_id):
        answer = SimpleNamespace(
            answer_text="queued answer",
            mode="rag_docs",
            confidence=None,
            sources=[],
        )
        return SimpleNamespace(status="completed", answer=answer)


class FakeHybridRetriever:
    async def resolve_knowledge_base(self, tenant_id, requested_id):
        return requested_id or uuid.uuid4()


def queued_runtime(application):
    return {
        "execution_mode": "queued",
        "queued_application": application,
        "hybrid_retriever": FakeHybridRetriever(),
    }


def test_ask_queued_mode_preserves_response_and_exposes_job_headers():
    application = FakeQueuedApplication()
    client = build_client(queued_runtime(application))

    response = client.post(
        "/ask",
        json={"question": "queued"},
        headers={"Idempotency-Key": "request-1"},
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "queued answer"
    assert response.headers["X-Inference-Job-Id"] == str(application.job_id)
    assert application.idempotency_key == "request-1"


def test_async_job_endpoint_returns_202_and_urls():
    application = FakeQueuedApplication()
    client = build_client(queued_runtime(application))

    response = client.post("/v1/inference-jobs", json={"question": "queued"})

    assert response.status_code == 202
    assert response.json()["job_id"] == str(application.job_id)
    assert response.json()["status_url"].endswith(str(application.job_id))
