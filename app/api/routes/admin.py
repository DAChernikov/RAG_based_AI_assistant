from fastapi import APIRouter, Depends, Request

from app.api.dependencies import get_runtime_state, require_admin
from app.auth.security import Principal

router = APIRouter(tags=["admin"])


@router.get("/admin/runtime")
async def admin_runtime(
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime: dict = Depends(get_runtime_state),
) -> dict:
    auth_service = runtime.get("auth_service")
    if auth_service is not None:
        await auth_service._call(
            runtime["auth_repository"].audit,
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            action="admin.runtime.read",
            outcome="success",
            correlation_id=request.state.correlation_id,
            resource_type="runtime",
        )
    runtime = getattr(request.app.state, "runtime", {})
    return {
        "llm_name": runtime.get("llm_name"),
        "startup_error": runtime.get("startup_error"),
        "execution_mode": "queued",
        "database_configured": runtime.get("database_engine") is not None,
        "redis_configured": runtime.get("queue") is not None,
        "embedding_configured": runtime.get("embedding_client") is not None,
    }
