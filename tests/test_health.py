from fastapi.testclient import TestClient

from app.api.main import app

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
