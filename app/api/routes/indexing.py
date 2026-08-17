from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel

from app.api.dependencies import get_runtime_state, require_admin
from app.api.routes.catalog import _audit
from app.auth.security import Principal
from app.indexing.contracts import IndexingJobContract
from app.indexing.repository import IndexingConflictError

router = APIRouter(prefix="/v1/admin", tags=["indexing"])


class IndexingRunResponse(BaseModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    index_version_id: uuid.UUID
    status: str
    attempt_count: int
    checkpoint: dict
    cancel_requested: bool


class IndexVersionResponse(BaseModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    version_number: int
    status: str
    manifest: dict
    pinned: bool


class PinRequest(BaseModel):
    pinned: bool


def _run(item):
    return IndexingRunResponse(
        id=item.id,
        knowledge_base_id=item.knowledge_base_id,
        index_version_id=item.index_version_id,
        status=item.status,
        attempt_count=item.attempt_count,
        checkpoint=item.checkpoint,
        cancel_requested=item.cancel_requested,
    )


def _version(item):
    return IndexVersionResponse(
        id=item.id,
        knowledge_base_id=item.knowledge_base_id,
        version_number=item.version_number,
        status=item.status,
        manifest=item.manifest,
        pinned=item.pinned,
    )


@router.post(
    "/knowledge-bases/{knowledge_base_id}/indexing-runs",
    response_model=IndexingRunResponse,
    status_code=202,
)
async def start_indexing(
    knowledge_base_id: uuid.UUID,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    request_hash = hashlib.sha256(str(knowledge_base_id).encode()).hexdigest()
    try:
        run, created = await runtime["catalog_service"].call(
            runtime["index_repository"].create_run,
            principal.tenant_id,
            knowledge_base_id,
            idempotency_key,
            request_hash,
            runtime["settings"].indexing_max_attempts,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IndexingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        await runtime["indexing_queue"].enqueue(
            IndexingJobContract(
                run_id=run.id,
                tenant_id=principal.tenant_id,
                knowledge_base_id=knowledge_base_id,
                index_version_id=run.index_version_id,
                correlation_id=request.state.correlation_id,
            )
        )
    await _audit(request, runtime, principal, "indexing.start", "indexing_run", run.id)
    return _run(run)


@router.get("/indexing-runs", response_model=list[IndexingRunResponse])
async def list_runs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    rows = await runtime["catalog_service"].call(
        runtime["index_repository"].list_runs, principal.tenant_id, offset, limit
    )
    return [_run(item) for item in rows]


@router.get("/indexing-runs/{run_id}", response_model=IndexingRunResponse)
async def get_run(
    run_id: uuid.UUID,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    row = await runtime["catalog_service"].call(
        runtime["index_repository"].get_run, principal.tenant_id, run_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Indexing run was not found.")
    return _run(row)


@router.get("/indexing-runs/{run_id}/events")
async def list_events(
    run_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        rows = await runtime["catalog_service"].call(
            runtime["index_repository"].list_events,
            principal.tenant_id,
            run_id,
            offset,
            limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [
        {
            "id": row.id,
            "run_id": row.run_id,
            "event_type": row.event_type,
            "payload": row.payload,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get(
    "/knowledge-bases/{knowledge_base_id}/index-versions",
    response_model=list[IndexVersionResponse],
)
async def list_versions(
    knowledge_base_id: uuid.UUID,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    rows = await runtime["catalog_service"].call(
        runtime["index_repository"].list_versions,
        principal.tenant_id,
        knowledge_base_id,
    )
    return [_version(item) for item in rows]


@router.post("/indexing-runs/{run_id}/cancel", status_code=204)
async def cancel_run(
    run_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        changed = await runtime["catalog_service"].call(
            runtime["index_repository"].cancel, principal.tenant_id, run_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=409, detail="Terminal indexing run cannot be cancelled.")
    await _audit(request, runtime, principal, "indexing.cancel", "indexing_run", run_id)


async def _activate(version_id, request, principal, runtime, action):
    try:
        version = await runtime["catalog_service"].call(
            runtime["index_repository"].activate, principal.tenant_id, version_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IndexingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _audit(request, runtime, principal, action, "index_version", version_id)
    return _version(version)


@router.post("/index-versions/{version_id}/activate", response_model=IndexVersionResponse)
async def activate(
    version_id: uuid.UUID,
    request: Request,
    principal=Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    return await _activate(version_id, request, principal, runtime, "index.activate")


@router.post("/index-versions/{version_id}/rollback", response_model=IndexVersionResponse)
async def rollback(
    version_id: uuid.UUID,
    request: Request,
    principal=Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    return await _activate(version_id, request, principal, runtime, "index.rollback")


@router.put("/index-versions/{version_id}/pin", response_model=IndexVersionResponse)
async def pin_version(
    version_id: uuid.UUID,
    payload: PinRequest,
    request: Request,
    principal=Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        row = await runtime["catalog_service"].call(
            runtime["index_repository"].set_pinned,
            principal.tenant_id,
            version_id,
            payload.pinned,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _audit(request, runtime, principal, "index.pin", "index_version", version_id)
    return _version(row)


@router.get("/embedding-runtime")
async def embedding_runtime(
    _principal: Principal = Depends(require_admin), runtime=Depends(get_runtime_state)
):
    return await runtime["embedding_client"].readiness()
