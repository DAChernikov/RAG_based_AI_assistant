from fastapi import APIRouter, Request

from app.api.config import settings
from app.api.schemas import HealthResponse, ReadyResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", app_env=settings.app_env)


@router.get("/ready", response_model=ReadyResponse)
def ready(request: Request) -> ReadyResponse:
    runtime = getattr(request.app.state, "runtime", {})
    artifacts_ready = bool(runtime.get("artifacts_ready", False))
    rag_ready = bool(runtime.get("rag_ready", False))
    startup_error = runtime.get("startup_error")
    status = "ready" if artifacts_ready and rag_ready else "not_ready"

    return ReadyResponse(
        status=status,
        artifacts_ready=artifacts_ready,
        rag_ready=rag_ready,
        startup_error=startup_error,
    )
