import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import StreamingResponse

from app.api.config import settings
from app.api.dependencies import get_runtime_state, require_scope
from app.api.schemas import AskRequest, AskResponse, RetrievedDocument
from app.auth.security import Principal
from app.state.repositories import (
    ConversationAccessError,
    IdempotencyConflictError,
    IdentityNotSeededError,
)

router = APIRouter(tags=["ask"])


@asynccontextmanager
async def _admission(runtime: dict):
    semaphore = runtime.get("inference_semaphore")
    if semaphore is None:  # Lightweight injected runtimes used by contract tests.
        yield
    else:
        async with semaphore:
            yield


def _response_from_result(payload: AskRequest, result: dict, mode: str) -> AskResponse:
    return AskResponse(
        question=payload.question,
        answer=result.get("answer", ""),
        mode=result.get("mode", mode),
        confidence=result.get("confidence"),
        retrieved=[
            RetrievedDocument(
                doc_id=item.get("doc_id", "unknown"),
                source=item.get("source", "unknown"),
                score=float(item.get("score", 0.0)),
                title=item.get("title"),
                uri=item.get("uri"),
                metadata=item.get("metadata") or {},
            )
            for item in result.get("retrieved", [])
        ],
    )


async def _submit_queued(payload, runtime, idempotency_key, principal: Principal):
    application = runtime.get("queued_application")
    if application is None:
        raise HTTPException(status_code=503, detail="Queued inference is not ready.")
    try:
        knowledge_base_id = await runtime["hybrid_retriever"].resolve_knowledge_base(
            principal.tenant_id, payload.knowledge_base_id
        )
        request_payload = payload.model_dump(mode="json")
        request_payload["knowledge_base_id"] = knowledge_base_id
        return await application.submit(
            request_payload,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            idempotency_key=idempotency_key,
            conversation_id=payload.conversation_id,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConversationAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IdentityNotSeededError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Unable to enqueue inference job.") from exc


@router.post("/ask", response_model=AskResponse)
async def ask(
    payload: AskRequest,
    response: Response,
    principal: Principal = Depends(require_scope("inference:write")),
    runtime: dict = Depends(get_runtime_state),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> AskResponse:
    async with _admission(runtime):
        creation, contract = await _submit_queued(payload, runtime, idempotency_key, principal)
        response.headers["X-Inference-Job-Id"] = str(creation.job.id)
        response.headers["X-Correlation-Id"] = str(contract.correlation_id)
        terminal = await runtime["queued_application"].wait_for_terminal(creation.job.id)
    if terminal is None:
        raise HTTPException(
            status_code=504,
            detail={"message": "Inference is still running.", "job_id": str(creation.job.id)},
            headers={
                "X-Inference-Job-Id": str(creation.job.id),
                "X-Correlation-Id": str(contract.correlation_id),
            },
        )
    if terminal.status == "failed":
        raise HTTPException(
            status_code=503,
            detail={
                "message": terminal.error_message or "Inference failed.",
                "job_id": str(terminal.id),
            },
        )
    result = {
        "answer": terminal.answer.answer_text,
        "mode": terminal.answer.mode,
        "confidence": terminal.answer.confidence,
        "retrieved": [
            {
                "doc_id": source.source_id,
                "source": source.source_type,
                "title": source.title,
                "uri": source.uri,
                "metadata": source.metadata_json,
                "score": source.score,
            }
            for source in terminal.answer.sources
        ],
    }
    return _response_from_result(payload, result, terminal.answer.mode)


@router.post("/ask/stream")
async def ask_stream(
    payload: AskRequest,
    principal: Principal = Depends(require_scope("inference:write")),
    runtime: dict = Depends(get_runtime_state),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    creation, _ = await _submit_queued(payload, runtime, idempotency_key, principal)

    async def queued_events():
        async with _admission(runtime):
            async with asyncio.timeout(settings.sse_max_lifetime_sec):
                async for redis_id, event in runtime["queue"].iter_events(
                    creation.job.id, last_event_id=last_event_id or "0-0"
                ):
                    data = event.model_dump(mode="json")
                    legacy_type = "done" if event.event_type == "completed" else event.event_type
                    payload_data = data["payload"]
                    if event.event_type == "token":
                        payload_data = data["payload"]["text"]
                    elif event.event_type == "completed":
                        payload_data = data["payload"]["answer"]
                    data.update({"type": legacy_type, "data": payload_data})
                    yield (
                        f"id: {redis_id}\n"
                        f"event: {event.event_type}\n"
                        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                    )

    return StreamingResponse(queued_events(), media_type="text/event-stream")
