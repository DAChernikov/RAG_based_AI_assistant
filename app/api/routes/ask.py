import json

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import StreamingResponse

from app.api.config import settings
from app.api.dependencies import get_runtime_state, require_scope
from app.api.schemas import AskRequest, AskResponse, RetrievedDocument
from app.api.services.llm_service import LLMRateLimitError, LLMTemporaryUnavailableError
from app.api.services.router_service import RouterService
from app.api.services.sql_service import SQLService
from app.auth.security import Principal
from app.state.repositories import (
    ConversationAccessError,
    IdempotencyConflictError,
    IdentityNotSeededError,
)

router = APIRouter(tags=["ask"])


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
            )
            for item in result.get("retrieved", [])
        ],
    )


async def _direct_ask(payload: AskRequest, runtime: dict) -> tuple[dict, str]:
    mode = RouterService().route(payload.question, payload.mode or settings.default_mode)
    try:
        if mode == "sql":
            retriever = runtime.get("retriever")
            llm_service = runtime.get("llm_service")
            if retriever is None or llm_service is None:
                raise HTTPException(status_code=503, detail="Inference runtime is not ready.")
            result = await SQLService(retriever, llm_service).ask(
                question=payload.question,
                top_k=payload.top_k or settings.sql_top_k,
                max_new_tokens=payload.max_new_tokens or settings.sql_max_new_tokens,
            )
        else:
            rag_service = runtime.get("rag_service")
            if rag_service is None:
                raise HTTPException(status_code=503, detail="Inference runtime is not ready.")
            result = await rag_service.ask(
                question=payload.question,
                top_k=payload.top_k or settings.top_k,
                max_new_tokens=payload.max_new_tokens
                or (
                    settings.code_max_new_tokens
                    if mode == "rag_code"
                    else settings.doc_max_new_tokens
                ),
                mode=mode,
            )
        return result, mode
    except LLMRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except LLMTemporaryUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Inference failed.") from exc


async def _submit_queued(payload, runtime, idempotency_key, principal: Principal):
    application = runtime.get("queued_application")
    if application is None:
        raise HTTPException(status_code=503, detail="Queued inference is not ready.")
    try:
        return await application.submit(
            payload.model_dump(mode="json"),
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
    if runtime.get("execution_mode") == "queued":
        creation, contract = await _submit_queued(payload, runtime, idempotency_key, principal)
        response.headers["X-Inference-Job-Id"] = str(creation.job.id)
        response.headers["X-Correlation-Id"] = str(contract.correlation_id)
        terminal = await runtime["queued_application"].wait_for_terminal(creation.job.id)
        if terminal is None:
            raise HTTPException(
                status_code=504,
                detail={
                    "message": "Inference is still running.",
                    "job_id": str(creation.job.id),
                },
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
                    "score": source.score,
                }
                for source in terminal.answer.sources
            ],
        }
        return _response_from_result(payload, result, terminal.answer.mode)

    result, mode = await _direct_ask(payload, runtime)
    return _response_from_result(payload, result, mode)


@router.post("/ask/stream")
async def ask_stream(
    payload: AskRequest,
    principal: Principal = Depends(require_scope("inference:write")),
    runtime: dict = Depends(get_runtime_state),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    if runtime.get("execution_mode") == "queued":
        creation, _ = await _submit_queued(payload, runtime, idempotency_key, principal)

        async def queued_events():
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

    mode = RouterService().route(payload.question, payload.mode or settings.default_mode)
    if mode == "sql":
        result, _ = await _direct_ask(payload, runtime)

        async def sql_events():
            meta = {
                "mode": result.get("mode", "sql"),
                "confidence": result.get("confidence"),
                "retrieved": result.get("retrieved", []),
            }
            yield f"data: {json.dumps({'type': 'meta', 'data': meta}, ensure_ascii=False)}\n\n"
            done_data = {"type": "done", "data": result.get("answer", "")}
            yield (f"data: {json.dumps(done_data, ensure_ascii=False)}\n\n")

        return StreamingResponse(sql_events(), media_type="text/event-stream")

    rag_service = runtime.get("rag_service")
    if rag_service is None:
        raise HTTPException(status_code=503, detail="Inference runtime is not ready.")
    try:
        meta, stream = await rag_service.stream_answer(
            question=payload.question,
            top_k=payload.top_k or settings.top_k,
            max_new_tokens=payload.max_new_tokens
            or (
                settings.code_max_new_tokens if mode == "rag_code" else settings.doc_max_new_tokens
            ),
            mode=mode,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Streaming inference failed.") from exc

    async def direct_events():
        yield f"data: {json.dumps({'type': 'meta', 'data': meta}, ensure_ascii=False)}\n\n"
        full_text = ""
        try:
            async for chunk in stream:
                full_text += chunk
                token_data = {"type": "token", "data": chunk}
                yield f"data: {json.dumps(token_data, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'data': full_text}, ensure_ascii=False)}\n\n"
        except Exception:
            yield f"data: {json.dumps({'type': 'error', 'data': 'Streaming failed.'})}\n\n"

    return StreamingResponse(direct_events(), media_type="text/event-stream")
