import asyncio
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.embedding_service.main import EmbeddingSettings, LocalBGEBackend, create_app
from app.embeddings.client import EmbeddingClient, EmbeddingServiceError


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def encode(self, texts):
        self.calls.append(texts)
        return [[0.6, 0.8] for _ in texts]

    async def close(self):
        self.closed = True


def test_embedding_service_contract_batch_limits_and_shutdown():
    backend = FakeBackend()
    settings = EmbeddingSettings(
        embedding_dimensions=2,
        embedding_max_batch=2,
        embedding_max_text_chars=20,
    )
    with TestClient(create_app(backend, settings)) as client:
        response = client.post(
            "/v1/embeddings",
            json={"model": "BAAI/bge-m3", "input": ["one", "two"]},
        )
        assert response.status_code == 200
        assert response.json()["contract_version"] == "1.0"
        assert response.json()["model_version"] == "bge-m3/1"
        assert response.json()["data"][1]["embedding"] == [0.6, 0.8]
        assert (
            client.post("/v1/embeddings", json={"model": "wrong", "input": "one"}).status_code
            == 404
        )
        assert (
            client.post(
                "/v1/embeddings", json={"model": "BAAI/bge-m3", "input": ["a", "b", "c"]}
            ).status_code
            == 422
        )
    assert backend.closed is True


def test_embedding_readiness_never_claims_ready_for_unloaded_or_failed_backend():
    class NotReady(FakeBackend):
        ready = False
        readiness_error = "model_load_failed"

    with TestClient(create_app(NotReady(), EmbeddingSettings(embedding_dimensions=2))) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert response.json()["model_ready"] is False
        assert response.json()["reason"] == "model_load_failed"


@pytest.mark.asyncio
async def test_local_embedding_model_loads_once_under_concurrency(monkeypatch):
    loads = []

    class Array:
        def __init__(self, values):
            self.values = values

        def astype(self, _kind):
            return self

        def tolist(self):
            return self.values

    class Model:
        def __init__(self, name, device):
            loads.append((name, device))

        def encode(self, texts, **_kwargs):
            return [Array([1.0, 0.0]) for _ in texts]

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Model)
    )
    backend = LocalBGEBackend(EmbeddingSettings(embedding_dimensions=2))
    first, second = await asyncio.gather(backend.encode(["a"]), backend.encode(["b"]))
    assert first == second == [[1.0, 0.0]]
    assert len(loads) == 1
    assert backend.ready is True
    await backend.close()


@pytest.mark.asyncio
async def test_embedding_client_retries_temporary_failure_and_omits_token():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            json={"model": "BAAI/bge-m3", "data": [{"index": 0, "embedding": [1, 0]}]},
        )

    client = EmbeddingClient(
        "http://embedding.test/v1",
        "BAAI/bge-m3",
        transport=httpx.MockTransport(handler),
        retries=1,
        retry_backoff_sec=0,
    )
    assert await client.embed(["query"]) == [[1.0, 0.0]]
    assert len(requests) == 2
    assert all("authorization" not in request.headers for request in requests)
    await client.close()


@pytest.mark.asyncio
async def test_embedding_client_token_and_malformed_response():
    def handler(request):
        assert request.headers["authorization"] == "Bearer local-token"
        return httpx.Response(200, request=request, json={"model": "BAAI/bge-m3", "data": []})

    client = EmbeddingClient(
        "http://embedding.test/v1",
        "BAAI/bge-m3",
        token="local-token",
        transport=httpx.MockTransport(handler),
        retries=0,
    )
    with pytest.raises(EmbeddingServiceError):
        await client.embed(["query"])
    await client.close()


@pytest.mark.asyncio
async def test_embedding_client_enforces_registry_version_and_dimensions():
    def handler(request):
        return httpx.Response(
            200,
            request=request,
            json={
                "model": "BAAI/bge-m3",
                "model_version": "wrong",
                "data": [{"index": 0, "embedding": [1.0]}],
            },
        )

    client = EmbeddingClient(
        "http://embedding.test/v1",
        "BAAI/bge-m3",
        expected_version="bge-m3/1",
        expected_dimensions=2,
        transport=httpx.MockTransport(handler),
        retries=0,
    )
    with pytest.raises(EmbeddingServiceError):
        await client.embed(["query"])
    await client.close()
