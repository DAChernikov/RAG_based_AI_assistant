from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.config import settings
from app.api.schemas import HealthResponse, ReadyResponse
from app.concurrency import api_blocking_io
from app.state.database import database_is_ready

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", app_env=settings.app_env)


@router.get("/ready", response_model=ReadyResponse)
async def ready(request: Request) -> ReadyResponse:
    runtime = getattr(request.app.state, "runtime", {})
    engine = runtime.get("database_engine")
    queue = runtime.get("queue")
    database_ready = bool(engine and await api_blocking_io.call(database_is_ready, engine))
    redis_ready = bool(queue and await queue.ping())
    heartbeat = await queue.latest_heartbeat() if redis_ready else None
    embedding = (
        await runtime["embedding_client"].readiness()
        if runtime.get("embedding_client")
        else {"status": "not_ready"}
    )
    components = {
        "database": "ready" if database_ready else "not_ready",
        "redis": "ready" if redis_ready else "not_ready",
        "inference_worker": "ready" if heartbeat else "not_ready",
        "generation_model": (
            "ready" if heartbeat and heartbeat.get("model_ready") else "not_ready"
        ),
        "hybrid_retrieval": (
            "ready" if heartbeat and heartbeat.get("retriever_ready") else "not_ready"
        ),
        "embedding_model": (
            "ready"
            if embedding.get("status") == "ready" or embedding.get("model_ready") is True
            else "not_ready"
        ),
    }
    required = ("database", "redis", "inference_worker", "generation_model", "embedding_model")
    status = "ready" if all(components[item] == "ready" for item in required) else "not_ready"
    return ReadyResponse(status=status, components=components)
