import json
from typing import AsyncIterator

import httpx

from app.bot.config import bot_settings


class APIClient:
    def __init__(self):
        self.base_url = bot_settings.api_base_url.rstrip("/")
        self.timeout = bot_settings.request_timeout

    async def ask(self, question: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/ask",
                json={"question": question},
            )
            response.raise_for_status()
            return response.json()

    async def ask_stream(self, question: str) -> AsyncIterator[dict]:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/ask/stream",
                json={"question": question},
                headers={"Accept": "text/event-stream"},
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue

                    raw = line[len("data:") :].strip()
                    if not raw:
                        continue

                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        continue

    async def ready(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/ready")
            response.raise_for_status()
            return response.json()
