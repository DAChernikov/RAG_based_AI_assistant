from __future__ import annotations

import pytest

from app.worker_healthcheck import _identity, check, record_local_heartbeat


class Redis:
    def __init__(self, exists=1):
        self.exists_value = exists
        self.closed = False

    async def ping(self):
        return True

    async def exists(self, _key):
        return self.exists_value

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_worker_healthcheck_liveness_readiness_and_missing_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.worker_healthcheck._heartbeat_path", lambda role: tmp_path / f"{role}.heartbeat"
    )
    for role in ("inference", "ingestion", "indexing", "scheduler"):
        record_local_heartbeat(role)
    redis = Redis()
    monkeypatch.setattr("app.worker_healthcheck.Redis.from_url", lambda *_a, **_k: redis)
    await check("inference", False)
    await check("ingestion", True)
    assert redis.closed
    missing = Redis(exists=0)
    monkeypatch.setattr("app.worker_healthcheck.Redis.from_url", lambda *_a, **_k: missing)
    with pytest.raises(RuntimeError, match="heartbeat"):
        await check("indexing", True)
    await check("scheduler", True)
    with pytest.raises(ValueError, match="Unknown"):
        _identity("legacy")


@pytest.mark.asyncio
async def test_worker_liveness_rejects_missing_event_loop_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.worker_healthcheck._heartbeat_path", lambda role: tmp_path / f"{role}.heartbeat"
    )
    with pytest.raises(RuntimeError, match="event-loop"):
        await check("inference", False)
