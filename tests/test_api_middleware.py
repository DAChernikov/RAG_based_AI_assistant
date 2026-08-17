from __future__ import annotations

import httpx
import pytest

from app.api.main import app


@pytest.mark.asyncio
async def test_api_middleware_correlation_security_and_request_limits(monkeypatch):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health", headers={"X-Correlation-Id": "invalid"})
        assert response.status_code == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Correlation-Id"]

        invalid = await client.get("/health", headers={"Content-Length": "not-a-number"})
        assert invalid.status_code == 400

        monkeypatch.setattr("app.api.main.settings.max_request_body_bytes", 2)
        large = await client.post("/ask", content=b"large", headers={"Content-Length": "5"})
        assert large.status_code == 413


@pytest.mark.asyncio
async def test_api_rate_limiter_fails_closed_and_limits(monkeypatch):
    class Redis:
        def __init__(self, *, fail=False, value=1):
            self.fail = fail
            self.value = value

        async def eval(self, *_args):
            if self.fail:
                raise RuntimeError("redis down")
            return self.value

    app.state.runtime = {"auth_redis": Redis(fail=True)}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unavailable = await client.post("/ask", json={"question": "q"})
        assert unavailable.status_code == 503
        app.state.runtime = {"auth_redis": Redis(value=1000)}
        limited = await client.post("/ask", json={"question": "q"})
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"
    app.state.runtime = {}
