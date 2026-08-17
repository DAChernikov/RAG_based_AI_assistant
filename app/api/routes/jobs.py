import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.config import settings
from app.api.dependencies import get_runtime_state, require_scope
from app.api.routes.ask import _submit_queued
from app.api.schemas import AskRequest, InferenceJobAccepted, InferenceJobStatusResponse
from app.auth.security import Principal
from app.inference.application import serialize_job
from app.state.repositories import ConversationAccessError

router = APIRouter(prefix="/v1", tags=["inference"])


class FeedbackRequest(BaseModel):
    rating: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=1000)


def _require_queued(runtime: dict) -> None:
    if runtime.get("execution_mode") != "queued":
        raise HTTPException(status_code=409, detail="Endpoint requires queued execution mode.")


async def _audit_not_found(
    request: Request,
    runtime: dict,
    principal: Principal,
    resource_type: str,
    resource_id: uuid.UUID,
) -> None:
    service = runtime.get("auth_service")
    if service is not None:
        await service._call(
            runtime["auth_repository"].audit,
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            action="access.denied",
            outcome="denied",
            correlation_id=request.state.correlation_id,
            resource_type=resource_type,
            resource_id=str(resource_id),
        )


@router.post("/inference-jobs", status_code=202, response_model=InferenceJobAccepted)
async def create_inference_job(
    payload: AskRequest,
    request: Request,
    principal: Principal = Depends(require_scope("inference:write")),
    runtime: dict = Depends(get_runtime_state),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    _require_queued(runtime)
    creation, contract = await _submit_queued(payload, runtime, idempotency_key, principal)
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
async def get_inference_job(
    job_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_scope("inference:read")),
    runtime: dict = Depends(get_runtime_state),
):
    _require_queued(runtime)
    job = await runtime["repository"].get_job_for_owner(
        job_id, principal.tenant_id, principal.user_id
    )
    if job is None:
        await _audit_not_found(request, runtime, principal, "inference_job", job_id)
        raise HTTPException(status_code=404, detail="Inference job was not found.")
    return serialize_job(job)


@router.get("/inference-jobs/{job_id}/events")
async def inference_job_events(
    job_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_scope("inference:read")),
    runtime: dict = Depends(get_runtime_state),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    _require_queued(runtime)
    if (
        await runtime["repository"].get_job_for_owner(
            job_id, principal.tenant_id, principal.user_id
        )
        is None
    ):
        await _audit_not_found(request, runtime, principal, "inference_job", job_id)
        raise HTTPException(status_code=404, detail="Inference job was not found.")

    async def events():
        async with asyncio.timeout(settings.sse_max_lifetime_sec):
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
    request: Request,
    principal: Principal = Depends(require_scope("inference:read")),
    runtime: dict = Depends(get_runtime_state),
):
    _require_queued(runtime)
    try:
        return await runtime["repository"].conversation_history(
            conversation_id, principal.tenant_id, principal.user_id
        )
    except ConversationAccessError as exc:
        await _audit_not_found(request, runtime, principal, "conversation", conversation_id)
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/conversations")
async def conversations(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require_scope("inference:read")),
    runtime: dict = Depends(get_runtime_state),
):
    return await runtime["repository"].list_conversations(
        principal.tenant_id, principal.user_id, offset, limit
    )


@router.post("/inference-jobs/{job_id}/cancel", status_code=202)
async def cancel_job(
    job_id: uuid.UUID,
    principal: Principal = Depends(require_scope("inference:write")),
    runtime: dict = Depends(get_runtime_state),
):
    try:
        changed = await runtime["repository"].request_cancel(
            job_id, principal.tenant_id, principal.user_id
        )
    except ConversationAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=409, detail="Only queued or running jobs can be cancelled.")
    return {"job_id": job_id, "cancel_requested": True}


@router.post("/inference-jobs/{job_id}/retry", status_code=202, response_model=InferenceJobAccepted)
async def retry_job(
    job_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_scope("inference:write")),
    runtime: dict = Depends(get_runtime_state),
):
    previous = await runtime["repository"].get_job_for_owner(
        job_id, principal.tenant_id, principal.user_id
    )
    if previous is None:
        await _audit_not_found(request, runtime, principal, "inference_job", job_id)
        raise HTTPException(status_code=404, detail="Inference job was not found.")
    if previous.status not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Only failed or cancelled jobs can be retried.")
    payload = AskRequest.model_validate(previous.request_payload)
    creation, contract = await _submit_queued(
        payload, runtime, f"retry:{job_id}:{uuid.uuid4()}", principal
    )
    base = str(request.base_url).rstrip("/")
    return InferenceJobAccepted(
        job_id=creation.job.id,
        status=creation.job.status,
        conversation_id=creation.job.conversation_id,
        correlation_id=contract.correlation_id,
        status_url=f"{base}/v1/inference-jobs/{creation.job.id}",
        events_url=f"{base}/v1/inference-jobs/{creation.job.id}/events",
        reused=False,
    )


@router.post("/answers/{answer_id}/feedback", status_code=201)
async def feedback(
    answer_id: uuid.UUID,
    payload: FeedbackRequest,
    principal: Principal = Depends(require_scope("inference:write")),
    runtime: dict = Depends(get_runtime_state),
):
    if payload.rating == 0:
        raise HTTPException(status_code=422, detail="Rating must be -1 or 1.")
    try:
        row = await runtime["repository"].create_feedback(
            principal.tenant_id,
            principal.user_id,
            answer_id,
            payload.rating,
            payload.comment,
        )
    except ConversationAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "answer_id": answer_id, "rating": row.rating}
