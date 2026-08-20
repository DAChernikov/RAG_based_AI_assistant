from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.bot.config import bot_settings


class APIClient:
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None):
        self.base_url = (base_url or bot_settings.api_base_url).rstrip("/")
        self.api_key = api_key
        self.timeout = bot_settings.request_timeout
        self.stream_timeout = httpx.Timeout(
            connect=bot_settings.stream_connect_timeout,
            read=bot_settings.stream_read_timeout,
            write=bot_settings.stream_write_timeout,
            pool=bot_settings.stream_pool_timeout,
        )

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _client(self, timeout: float | httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def ask(
        self,
        question: str,
        mode: str | None = None,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"question": question}
        if mode:
            payload["mode"] = mode
        if top_k is not None:
            payload["top_k"] = top_k

        async with self._client(self.timeout) as client:
            response = await client.post(f"{self.base_url}/ask", json=payload, headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def ask_stream(
        self,
        question: str,
        mode: str | None = None,
        top_k: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        payload: dict[str, Any] = {"question": question}
        if mode:
            payload["mode"] = mode
        if top_k is not None:
            payload["top_k"] = top_k

        async with self._client(self.stream_timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/ask/stream",
                json=payload,
                headers={"Accept": "text/event-stream", **self.headers},
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue

                    raw = line[len("data:") :].strip()
                    if not raw or raw == "[DONE]":
                        continue

                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        continue

    async def ready(self) -> dict[str, Any]:
        async with self._client(self.timeout) as client:
            response = await client.get(f"{self.base_url}/ready")
            response.raise_for_status()
            return response.json()
