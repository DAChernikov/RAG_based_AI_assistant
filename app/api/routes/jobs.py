import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.config import settings
from app.api.dependencies import get_runtime_state
from app.api.routes.ask import _submit_queued
from app.api.schemas import AskRequest, InferenceJobAccepted, InferenceJobStatusResponse
from app.inference.application import serialize_job
from app.state.repositories import ConversationAccessError, IdentityNotSeededError

router = APIRouter(prefix="/v1", tags=["development-inference"])


def _require_queued(runtime: dict) -> None:
    if runtime.get("execution_mode") != "queued":
        raise HTTPException(status_code=409, detail="Endpoint requires queued execution mode.")


@router.post("/inference-jobs", status_code=202, response_model=InferenceJobAccepted)
async def create_inference_job(
    payload: AskRequest,
    request: Request,
    runtime: dict = Depends(get_runtime_state),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    _require_queued(runtime)
    creation, contract = await _submit_queued(payload, runtime, idempotency_key)
    base = str(request.base_url).rstrip("/")
    return InferenceJobAccepted(
        job_id=creation.job.id,
        status=creation.job.status,
        conversation_id=creation.job.conversation_id,
        correlation_id=contract.correlation_id,
        status_url=f"{base}/v1/inference-jobs/{creation.job.id}",
        events_url=f"{base}/v1/inference-jobs/{creation.job.id}/events",
        reused=not creation.created,
    )


@router.get("/inference-jobs/{job_id}", response_model=InferenceJobStatusResponse)
async def get_inference_job(job_id: uuid.UUID, runtime: dict = Depends(get_runtime_state)):
    _require_queued(runtime)
    job = runtime["repository"].get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Inference job was not found.")
    return serialize_job(job)


@router.get("/inference-jobs/{job_id}/events")
async def inference_job_events(
    job_id: uuid.UUID,
    runtime: dict = Depends(get_runtime_state),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    _require_queued(runtime)
    if runtime["repository"].get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Inference job was not found.")

    async def events():
        async for redis_id, event in runtime["queue"].iter_events(
            job_id, last_event_id=last_event_id or "0-0"
        ):
            yield (
                f"id: {redis_id}\n"
                f"event: {event.event_type}\n"
                f"data: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            )

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get("/conversations/{conversation_id}")
async def conversation_history(
    conversation_id: uuid.UUID,
    runtime: dict = Depends(get_runtime_state),
):
    _require_queued(runtime)
    try:
        tenant_id, user_id = runtime["repository"].get_compatibility_identity(
            settings.compatibility_tenant_slug,
            settings.compatibility_user_external_id,
        )
        return runtime["repository"].conversation_history(conversation_id, tenant_id, user_id)
    except IdentityNotSeededError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ConversationAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
