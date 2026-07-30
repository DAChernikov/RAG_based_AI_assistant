import time

from fastapi import APIRouter, Request

from app.api.config import settings
from app.api.schemas import HealthResponse, ReadyResponse
from app.state.database import database_is_ready

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", app_env=settings.app_env)


@router.get("/ready", response_model=ReadyResponse)
async def ready(request: Request) -> ReadyResponse:
    runtime = getattr(request.app.state, "runtime", {})
    mode = runtime.get("execution_mode", settings.inference_execution_mode)
    startup_error = runtime.get("startup_error")

    if mode == "queued":
        engine = runtime.get("database_engine")
        queue = runtime.get("queue")
        database_ready = bool(engine and database_is_ready(engine))
        redis_ready = bool(queue and await queue.ping())
        if redis_ready:
            try:
                await queue.ensure_group()
                startup_error = None
            except Exception:
                redis_ready = False
        heartbeat = await queue.latest_heartbeat() if redis_ready else None
        worker_ready = heartbeat is not None
        retriever_ready = bool(heartbeat and heartbeat.get("retriever_ready"))
        model_ready = bool(heartbeat and heartbeat.get("model_ready"))
        model_status = heartbeat.get("model_status") if heartbeat else "worker_unavailable"
        heartbeat_age = max(0.0, time.time() - heartbeat["last_seen_unix"]) if heartbeat else None
        all_ready = (
            database_ready and redis_ready and worker_ready and retriever_ready and model_ready
        )
        return ReadyResponse(
            status="ready" if all_ready else "not_ready",
            artifacts_ready=retriever_ready,
            rag_ready=retriever_ready,
            startup_error=startup_error,
            execution_mode=mode,
            model_ready=model_ready,
            model_status=model_status,
            database_ready=database_ready,
            redis_ready=redis_ready,
            worker_ready=worker_ready,
            worker_heartbeat_age_sec=heartbeat_age,
        )

    artifacts_ready = bool(runtime.get("artifacts_ready", False))
    rag_ready = bool(runtime.get("rag_ready", False))
    llm_service = runtime.get("llm_service")
    model = (
        await llm_service.readiness()
        if llm_service is not None
        else {"ready": False, "status": "not_initialized"}
    )
    all_ready = artifacts_ready and rag_ready and model["ready"] and not startup_error
    return ReadyResponse(
        status="ready" if all_ready else "not_ready",
        artifacts_ready=artifacts_ready,
        rag_ready=rag_ready,
        startup_error=startup_error,
        execution_mode=mode,
        model_ready=model["ready"],
        model_status=model["status"],
    )
