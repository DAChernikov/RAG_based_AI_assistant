from __future__ import annotations

import httpx


class RerankerClient:
    """Optional self-hosted reranking capability with safe retrieval-order fallback."""

    def __init__(
        self,
        base_url: str,
        token: str | None,
        timeout: float,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if client is not None and transport is not None:
            raise ValueError("Pass either client or transport, not both.")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )

    async def rerank(self, query: str, documents: list[dict], top_k: int) -> list[dict]:
        try:
            response = await self.client.post(
                f"{self.base_url}/rerank",
                json={
                    "query": query,
                    "documents": [row["text"] for row in documents],
                    "top_n": top_k,
                },
            )
            response.raise_for_status()
            results = response.json()["results"]
            ordered = []
            for result in results:
                row = dict(documents[int(result["index"])])
                row["rerank_score"] = float(result["relevance_score"])
                ordered.append(row)
            return ordered[:top_k] if ordered else documents[:top_k]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            return documents[:top_k]

    async def readiness(self) -> dict:
        try:
            response = await self.client.get(f"{self.base_url}/ready")
            response.raise_for_status()
            payload = response.json()
            return {
                "ready": bool(payload.get("ready") or payload.get("status") == "ready"),
                "status": payload.get("status", "ready"),
            }
        except (httpx.HTTPError, TypeError, ValueError):
            return {"ready": False, "status": "unavailable"}

    async def close(self) -> None:
        if self._owns_client and not self.client.is_closed:
            await self.client.aclose()
