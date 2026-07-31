import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_runtime_state, require_scope
from app.api.routes.ask import _submit_queued
from app.api.schemas import AskRequest, InferenceJobAccepted, InferenceJobStatusResponse
from app.auth.security import Principal
from app.inference.application import serialize_job
from app.state.repositories import ConversationAccessError

router = APIRouter(prefix="/v1", tags=["development-inference"])


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
