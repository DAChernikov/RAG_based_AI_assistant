import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_principal, get_runtime_state
from app.api.routes import ask, jobs
from app.auth.security import Principal
from app.inference.contracts import CompletedEvent, CompletedPayload, InferenceJobContract


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
        self.payload = payload
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


def test_ask_model_only_does_not_require_or_resolve_a_knowledge_base():
    application = FakeQueuedApplication()
    client = build_client(queued_runtime(application))

    response = client.post("/ask", json={"question": "hello", "mode": "model"})

    assert response.status_code == 200
    assert application.payload["mode"] == "model"
    assert application.payload["knowledge_base_id"] is None


def test_async_job_endpoint_returns_202_and_urls():
    application = FakeQueuedApplication()
    client = build_client(queued_runtime(application))

    response = client.post("/v1/inference-jobs", json={"question": "queued"})

    assert response.status_code == 202
    assert response.json()["job_id"] == str(application.job_id)
    assert response.json()["status_url"].endswith(str(application.job_id))


class FakeRepository:
    def __init__(self, job):
        self.job = job
        self.cancelled = True

    async def get_job_for_owner(self, *_args):
        return self.job

    async def list_conversations(self, *_args):
        return [{"conversation_id": str(self.job.conversation_id), "title": "Question"}]

    async def conversation_history(self, *_args):
        return {"conversation_id": str(self.job.conversation_id), "messages": []}

    async def request_cancel(self, *_args):
        return self.cancelled

    async def create_feedback(self, _tenant, _user, answer_id, rating, _comment):
        return SimpleNamespace(id=uuid.uuid4(), answer_id=answer_id, rating=rating)


class FakeEventQueue:
    async def iter_events(self, job_id, last_event_id="0-0"):
        yield (
            "1-0",
            CompletedEvent(
                event_id=str(uuid.uuid4()),
                sequence=1,
                job_id=job_id,
                correlation_id=uuid.uuid4(),
                event_type="completed",
                payload=CompletedPayload(answer="done", mode="rag_docs"),
            ),
        )


def detailed_runtime(status="completed"):
    job = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        status=status,
        contract_version="1.0",
        attempt_count=1,
        max_attempts=3,
        cancel_requested=False,
        queued_at=datetime.now(UTC),
        started_at=None,
        completed_at=None,
        failed_at=None,
        error_code=None,
        error_message=None,
        answer=None,
        request_payload={"question": "again", "knowledge_base_id": str(uuid.uuid4())},
    )
    return {
        "execution_mode": "queued",
        "repository": FakeRepository(job),
        "queue": FakeEventQueue(),
        "queued_application": FakeQueuedApplication(),
        "hybrid_retriever": FakeHybridRetriever(),
    }, job


def test_job_status_history_events_cancel_feedback_and_negative_cases():
    runtime, job = detailed_runtime("running")
    client = build_client(runtime)
    assert client.get(f"/v1/inference-jobs/{job.id}").json()["status"] == "running"
    stream = client.get(f"/v1/inference-jobs/{job.id}/events")
    assert stream.status_code == 200 and "event: completed" in stream.text
    assert client.get("/v1/conversations").json()[0]["title"] == "Question"
    assert client.get(f"/v1/conversations/{job.conversation_id}").status_code == 200
    assert client.post(f"/v1/inference-jobs/{job.id}/cancel").json()["cancel_requested"]
    feedback = client.post(
        f"/v1/answers/{uuid.uuid4()}/feedback", json={"rating": 1, "comment": "good"}
    )
    assert feedback.status_code == 201
    assert (
        client.post(f"/v1/answers/{uuid.uuid4()}/feedback", json={"rating": 0}).status_code == 422
    )

    job.status = "completed"
    assert client.post(f"/v1/inference-jobs/{job.id}/retry").status_code == 409
    runtime["repository"].job = None
    assert client.get(f"/v1/inference-jobs/{job.id}").status_code == 404
    assert client.get(f"/v1/inference-jobs/{job.id}/events").status_code == 404


def test_failed_job_can_be_retried_with_new_contract():
    runtime, job = detailed_runtime("failed")
    client = build_client(runtime)
    response = client.post(f"/v1/inference-jobs/{job.id}/retry")
    assert response.status_code == 202
    assert response.json()["reused"] is False
