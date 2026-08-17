from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx


class EmbeddingServiceError(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        token: str | None = None,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        retries: int = 2,
        retry_backoff_sec: float = 0.5,
        expected_version: str | None = None,
        expected_dimensions: int | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.retries = retries
        self.retry_backoff_sec = retry_backoff_sec
        self.expected_version = expected_version
        self.expected_dimensions = expected_dimensions
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if client is not None and transport is not None:
            raise ValueError("Pass either client or transport, not both.")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            transport=transport,
            trust_env=False,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await self.client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": list(texts), "encoding_format": "float"},
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "temporary embedding error", request=response.request, response=response
                    )
                response.raise_for_status()
                payload = response.json()
                if payload.get("model") != self.model:
                    raise ValueError("Embedding response model mismatch.")
                if self.expected_version and payload.get("model_version") != self.expected_version:
                    raise ValueError("Embedding response version mismatch.")
                rows = sorted(payload["data"], key=lambda item: item["index"])
                vectors = [list(map(float, item["embedding"])) for item in rows]
                if len(vectors) != len(texts):
                    raise ValueError("Embedding response count mismatch.")
                if self.expected_dimensions and any(
                    len(vector) != self.expected_dimensions for vector in vectors
                ):
                    raise ValueError("Embedding response dimension mismatch.")
                return vectors
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(self.retry_backoff_sec * (2**attempt))
        raise EmbeddingServiceError("Embedding service request failed.") from last_error

    async def readiness(self) -> dict:
        try:
            response = await self.client.get(f"{self.base_url.rsplit('/v1', 1)[0]}/ready")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            return {"status": "not_ready", "model_ready": False}

    async def close(self) -> None:
        if self._owns_client and not self.client.is_closed:
            await self.client.aclose()
