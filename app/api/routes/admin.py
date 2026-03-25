from fastapi import APIRouter, Request

router = APIRouter(tags=["admin"])


@router.get("/admin/runtime")
def admin_runtime(request: Request) -> dict:
    runtime = getattr(request.app.state, "runtime", {})
    return {
        "artifacts_ready": bool(runtime.get("artifacts_ready", False)),
        "rag_ready": bool(runtime.get("rag_ready", False)),
        "llm_name": runtime.get("llm_name"),
        "artifacts_dir": runtime.get("artifacts_dir"),
        "startup_error": runtime.get("startup_error"),
    }
