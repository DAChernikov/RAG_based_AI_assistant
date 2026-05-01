import json
import traceback

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.config import settings
from app.api.dependencies import AppStateError, get_runtime_state
from app.api.schemas import AskRequest, AskResponse, RetrievedDocument
from app.api.services.router_service import RouterService

router = APIRouter(tags=["ask"])


@router.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest, runtime: dict = Depends(get_runtime_state)) -> AskResponse:
    router_service = RouterService()
    mode = router_service.route(payload.question, payload.mode or settings.default_mode)

    if mode == "sql":
        sql_service = runtime.get("sql_service")
        if sql_service is None:
            startup_error = runtime.get("startup_error")
            detail = startup_error or "SQL service is not initialized."
            raise HTTPException(status_code=503, detail=detail)

        try:
            result = await sql_service.ask(
                question=payload.question,
                top_k=payload.top_k or settings.sql_top_k,
                max_new_tokens=payload.max_new_tokens or settings.max_new_tokens,
            )
        except Exception as exc:
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"SQL generation failed: {exc}") from exc
    else:
        rag_service = runtime.get("rag_service")
        if rag_service is None:
            startup_error = runtime.get("startup_error")
            detail = startup_error or "RAG service is not initialized."
            raise HTTPException(status_code=503, detail=detail)

        try:
            result = await rag_service.ask(
                question=payload.question,
                top_k=payload.top_k or settings.top_k,
                max_new_tokens=payload.max_new_tokens or settings.max_new_tokens,
                mode=mode,
            )
        except AppStateError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"Inference failed: {exc}") from exc

    retrieved = [
        RetrievedDocument(
            doc_id=item.get("doc_id", "unknown"),
            source=item.get("source", "unknown"),
            score=float(item.get("score", 0.0)),
            title=item.get("title"),
        )
        for item in result.get("retrieved", [])
    ]

    return AskResponse(
        question=payload.question,
        answer=result.get("answer", ""),
        mode=result.get("mode", mode),
        confidence=result.get("confidence"),
        retrieved=retrieved,
    )


@router.post("/ask/stream")
async def ask_stream(payload: AskRequest, runtime: dict = Depends(get_runtime_state)):
    router_service = RouterService()
    mode = router_service.route(payload.question, payload.mode or settings.default_mode)

    if mode == "sql":
        raise HTTPException(status_code=400, detail="Streaming is supported only for RAG modes.")

    rag_service = runtime.get("rag_service")
    if rag_service is None:
        startup_error = runtime.get("startup_error")
        detail = startup_error or "RAG service is not initialized."
        raise HTTPException(status_code=503, detail=detail)

    try:
        meta, stream = await rag_service.stream_answer(
            question=payload.question,
            top_k=payload.top_k or settings.top_k,
            max_new_tokens=payload.max_new_tokens or settings.max_new_tokens,
            mode=mode,
        )
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Streaming inference failed: {exc}") from exc

    async def event_generator():
        yield f"data: {json.dumps({'type': 'meta', 'data': meta}, ensure_ascii=False)}\n\n"

        try:
            full_text = ""
            async for chunk in stream:
                full_text += chunk
                yield f"data: {json.dumps({'type': 'token', 'data': chunk}, ensure_ascii=False)}\n\n"

            yield f"data: {json.dumps({'type': 'done', 'data': full_text}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'data': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
