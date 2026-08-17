from __future__ import annotations

import json

import pytest

from app.bot.api_client import APIClient


class Response:
    def __init__(self, payload=None, lines=None):
        self.payload = payload or {}
        self.lines = lines or []

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class Client:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return Response({"answer": "ok"})

    async def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return Response({"status": "ready"})

    def stream(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        event = json.dumps({"type": "token", "data": "a"})
        return Response(
            lines=["", "event: token", "data:", "data: {", f"data: {event}", "data: [DONE]"]
        )


@pytest.mark.asyncio
async def test_telegram_api_client_request_stream_and_ready(monkeypatch):
    transport = Client()
    client = APIClient()
    monkeypatch.setattr(client, "_client", lambda _timeout: transport)
    monkeypatch.setattr("app.bot.api_client.bot_settings.api_key", "scoped-key")
    result = await client.ask("question", mode="rag", top_k=3)
    assert result["answer"] == "ok"
    request = transport.requests[0]
    assert request[2]["headers"] == {"X-API-Key": "scoped-key"}
    assert request[2]["json"]["top_k"] == 3
    events = [item async for item in client.ask_stream("question")]
    assert events == [{"type": "token", "data": "a"}]
    assert await client.ready() == {"status": "ready"}
