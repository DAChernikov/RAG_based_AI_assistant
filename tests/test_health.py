from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_runtime_state
from app.api.main import app
from app.api.routes import health

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200

    payload = response.json()
    assert payload["status"] == "ok"
    assert "app_env" in payload


def test_ready():
    response = client.get("/ready")
    assert response.status_code == 200

    payload = response.json()
    assert "status" in payload
    assert "artifacts_ready" in payload
    assert "rag_ready" in payload
    assert "startup_error" in payload


class FakeQueue:
    async def ensure_group(self):
        return None

    async def ping(self):
        return True

    async def latest_heartbeat(self):
        return {
            "worker_id": "worker-1",
            "last_seen_unix": __import__("time").time(),
            "retriever_ready": True,
            "model_ready": True,
            "model_status": "available",
        }


def test_queued_ready_uses_worker_heartbeat(monkeypatch):
    queued_app = FastAPI()
    queued_app.include_router(health.router)
    queued_app.dependency_overrides[get_runtime_state] = lambda: {}
    queued_app.state.runtime = {
        "execution_mode": "queued",
        "database_engine": object(),
        "queue": FakeQueue(),
        "startup_error": None,
    }
    monkeypatch.setattr("app.api.routes.health.database_is_ready", lambda engine: True)

    response = TestClient(queued_app).get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["worker_ready"] is True
    assert response.json()["model_ready"] is True
