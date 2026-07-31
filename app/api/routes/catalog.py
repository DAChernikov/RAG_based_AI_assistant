from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import get_runtime_state, require_admin
from app.auth.security import Principal
from app.catalog.repository import (
    CatalogNotFoundError,
    IdempotencyConflictError,
    InvalidVersionTransitionError,
    SourceHistoryExistsError,
)
from app.catalog.schemas import (
    IngestionEventResponse,
    IngestionRunResponse,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
    KnowledgeSourceCreate,
    KnowledgeSourceResponse,
    KnowledgeSourceUpdate,
    SourceRefreshRequest,
    SourceVersionResponse,
)

router = APIRouter(prefix="/v1/admin", tags=["knowledge-catalog"])


def _knowledge_base_response(item) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=item.id,
        tenant_id=item.tenant_id,
        name=item.name,
        description=item.description,
        is_enabled=item.is_enabled,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _source_response(item) -> KnowledgeSourceResponse:
    return KnowledgeSourceResponse(
        id=item.id,
        tenant_id=item.tenant_id,
        name=item.name,
        source_type=item.source_type,
        config_version=item.config_version,
        config=item.config,
        is_enabled=item.is_enabled,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _version_response(item) -> SourceVersionResponse:
    return SourceVersionResponse(
        id=item.id,
        source_id=item.source_id,
        version_number=item.version_number,
        status=item.status,
        manifest=item.manifest,
        content_checksum=item.content_checksum,
        created_at=item.created_at,
        activated_at=item.activated_at,
    )


def _run_response(item) -> IngestionRunResponse:
    return IngestionRunResponse(
        id=item.id,
        source_id=item.source_id,
        source_version_id=item.source_version_id,
        status=item.status,
        connector_version=item.connector_version,
        started_at=item.started_at,
        completed_at=item.completed_at,
        error_code=item.error_code,
        error_message=item.error_message,
        attempt_count=item.attempt_count,
        max_attempts=item.max_attempts,
        cancel_requested=item.cancel_requested,
        auto_activate=item.auto_activate,
    )


async def _audit(
    request: Request,
    runtime: dict,
    principal: Principal,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    kwargs = {
        "tenant_id": principal.tenant_id,
        "actor_user_id": principal.user_id,
        "action": action,
        "outcome": "success",
        "correlation_id": request.state.correlation_id,
        "resource_type": resource_type,
        "resource_id": str(resource_id),
        "metadata": metadata,
    }
    service = runtime.get("auth_service")
    if service is not None:
        await service._call(runtime["auth_repository"].audit, **kwargs)
    else:
        await asyncio.to_thread(runtime["auth_repository"].audit, **kwargs)


async def _audit_denied(
    request: Request,
    runtime: dict,
    principal: Principal,
    resource_type: str,
    resource_id: uuid.UUID,
) -> None:
    kwargs = {
        "tenant_id": principal.tenant_id,
        "actor_user_id": principal.user_id,
        "action": "access.denied",
        "outcome": "denied",
        "correlation_id": request.state.correlation_id,
        "resource_type": resource_type,
        "resource_id": str(resource_id),
    }
    service = runtime.get("auth_service")
    if service is not None:
        await service._call(runtime["auth_repository"].audit, **kwargs)
    else:
        await asyncio.to_thread(runtime["auth_repository"].audit, **kwargs)


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    items = await runtime["catalog_service"].call(
        runtime["catalog_repository"].list_knowledge_bases, principal.tenant_id
    )
    await _audit(
        request,
        runtime,
        principal,
        "knowledge_base.list",
        "tenant",
        principal.tenant_id,
    )
    return [_knowledge_base_response(item) for item in items]


@router.post("/knowledge-bases", response_model=KnowledgeBaseResponse, status_code=201)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        item = await runtime["catalog_service"].call(
            runtime["catalog_repository"].create_knowledge_base,
            principal.tenant_id,
            payload.name,
            payload.description,
            payload.is_enabled,
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Knowledge base name already exists.") from exc
    await _audit(request, runtime, principal, "knowledge_base.create", "knowledge_base", item.id)
    return _knowledge_base_response(item)


@router.get("/knowledge-bases/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    knowledge_base_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    item = await runtime["catalog_service"].call(
        runtime["catalog_repository"].get_knowledge_base,
        principal.tenant_id,
        knowledge_base_id,
    )
    if item is None:
        await _audit_denied(request, runtime, principal, "knowledge_base", knowledge_base_id)
        raise HTTPException(status_code=404, detail="Knowledge base was not found.")
    await _audit(
        request, runtime, principal, "knowledge_base.read", "knowledge_base", knowledge_base_id
    )
    return _knowledge_base_response(item)


@router.patch("/knowledge-bases/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    knowledge_base_id: uuid.UUID,
    payload: KnowledgeBaseUpdate,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        item = await runtime["catalog_service"].call(
            runtime["catalog_repository"].update_knowledge_base,
            principal.tenant_id,
            knowledge_base_id,
            payload.model_dump(exclude_unset=True),
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Knowledge base name already exists.") from exc
    if item is None:
        await _audit_denied(request, runtime, principal, "knowledge_base", knowledge_base_id)
        raise HTTPException(status_code=404, detail="Knowledge base was not found.")
    await _audit(request, runtime, principal, "knowledge_base.update", "knowledge_base", item.id)
    return _knowledge_base_response(item)


@router.delete("/knowledge-bases/{knowledge_base_id}", status_code=204)
async def delete_knowledge_base(
    knowledge_base_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    deleted = await runtime["catalog_service"].call(
        runtime["catalog_repository"].delete_knowledge_base,
        principal.tenant_id,
        knowledge_base_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Knowledge base was not found.")
    await _audit(
        request, runtime, principal, "knowledge_base.delete", "knowledge_base", knowledge_base_id
    )


@router.get("/knowledge-sources", response_model=list[KnowledgeSourceResponse])
async def list_sources(
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    items = await runtime["catalog_service"].call(
        runtime["catalog_repository"].list_sources, principal.tenant_id
    )
    await _audit(
        request,
        runtime,
        principal,
        "knowledge_source.list",
        "tenant",
        principal.tenant_id,
    )
    return [_source_response(item) for item in items]


@router.post("/knowledge-sources", response_model=KnowledgeSourceResponse, status_code=201)
async def create_source(
    payload: KnowledgeSourceCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    config = payload.config.model_dump(mode="json")
    try:
        item = await runtime["catalog_service"].call(
            runtime["catalog_repository"].create_source,
            principal.tenant_id,
            payload.name,
            payload.config.source_type,
            payload.config.config_version,
            config,
            payload.is_enabled,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="Knowledge source name already exists."
        ) from exc
    await _audit(
        request,
        runtime,
        principal,
        "knowledge_source.create",
        "knowledge_source",
        item.id,
        {"source_type": item.source_type},
    )
    return _source_response(item)


@router.get("/knowledge-sources/{source_id}", response_model=KnowledgeSourceResponse)
async def get_source(
    source_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    item = await runtime["catalog_service"].call(
        runtime["catalog_repository"].get_source, principal.tenant_id, source_id
    )
    if item is None:
        await _audit_denied(request, runtime, principal, "knowledge_source", source_id)
        raise HTTPException(status_code=404, detail="Knowledge source was not found.")
    await _audit(
        request, runtime, principal, "knowledge_source.read", "knowledge_source", source_id
    )
    return _source_response(item)


@router.patch("/knowledge-sources/{source_id}", response_model=KnowledgeSourceResponse)
async def update_source(
    source_id: uuid.UUID,
    payload: KnowledgeSourceUpdate,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    existing = await runtime["catalog_service"].call(
        runtime["catalog_repository"].get_source, principal.tenant_id, source_id
    )
    if existing is None:
        await _audit_denied(request, runtime, principal, "knowledge_source", source_id)
        raise HTTPException(status_code=404, detail="Knowledge source was not found.")
    changes = payload.model_dump(exclude_unset=True, exclude={"config"})
    if payload.config is not None:
        if payload.config.source_type != existing.source_type:
            raise HTTPException(status_code=409, detail="Source type cannot be changed.")
        changes.update(
            {
                "config": payload.config.model_dump(mode="json"),
                "config_version": payload.config.config_version,
            }
        )
    try:
        item = await runtime["catalog_service"].call(
            runtime["catalog_repository"].update_source,
            principal.tenant_id,
            source_id,
            changes,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="Knowledge source name already exists."
        ) from exc
    await _audit(
        request, runtime, principal, "knowledge_source.update", "knowledge_source", source_id
    )
    return _source_response(item)


@router.delete("/knowledge-sources/{source_id}", status_code=204)
async def delete_source(
    source_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        deleted = await runtime["catalog_service"].call(
            runtime["catalog_repository"].delete_source, principal.tenant_id, source_id
        )
    except SourceHistoryExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Knowledge source was not found.")
    await _audit(
        request, runtime, principal, "knowledge_source.delete", "knowledge_source", source_id
    )


@router.get(
    "/knowledge-bases/{knowledge_base_id}/sources",
    response_model=list[KnowledgeSourceResponse],
)
async def list_linked_sources(
    knowledge_base_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    items = await runtime["catalog_service"].call(
        runtime["catalog_repository"].list_linked_sources,
        principal.tenant_id,
        knowledge_base_id,
    )
    await _audit(
        request,
        runtime,
        principal,
        "knowledge_base.sources.list",
        "knowledge_base",
        knowledge_base_id,
    )
    return [_source_response(item) for item in items]


@router.put("/knowledge-bases/{knowledge_base_id}/sources/{source_id}", status_code=204)
async def link_source(
    knowledge_base_id: uuid.UUID,
    source_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        await runtime["catalog_service"].call(
            runtime["catalog_repository"].link_source,
            principal.tenant_id,
            knowledge_base_id,
            source_id,
        )
    except CatalogNotFoundError as exc:
        await _audit_denied(request, runtime, principal, "knowledge_base", knowledge_base_id)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _audit(
        request,
        runtime,
        principal,
        "knowledge_base.source.link",
        "knowledge_base",
        knowledge_base_id,
        {"source_id": str(source_id)},
    )


@router.delete("/knowledge-bases/{knowledge_base_id}/sources/{source_id}", status_code=204)
async def unlink_source(
    knowledge_base_id: uuid.UUID,
    source_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    deleted = await runtime["catalog_service"].call(
        runtime["catalog_repository"].unlink_source,
        principal.tenant_id,
        knowledge_base_id,
        source_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Knowledge base source link was not found.")
    await _audit(
        request,
        runtime,
        principal,
        "knowledge_base.source.unlink",
        "knowledge_base",
        knowledge_base_id,
        {"source_id": str(source_id)},
    )


@router.get(
    "/knowledge-sources/{source_id}/versions",
    response_model=list[SourceVersionResponse],
)
async def list_versions(
    source_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    items = await runtime["catalog_service"].call(
        runtime["catalog_repository"].list_versions, principal.tenant_id, source_id
    )
    await _audit(
        request,
        runtime,
        principal,
        "source_version.list",
        "knowledge_source",
        source_id,
    )
    return [_version_response(item) for item in items]


@router.get(
    "/knowledge-sources/{source_id}/ingestion-runs",
    response_model=list[IngestionRunResponse],
)
async def list_ingestion_runs(
    source_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    items = await runtime["catalog_service"].call(
        runtime["catalog_repository"].list_ingestion_runs,
        principal.tenant_id,
        source_id,
    )
    await _audit(
        request,
        runtime,
        principal,
        "ingestion_run.list",
        "knowledge_source",
        source_id,
    )
    return [_run_response(item) for item in items]


@router.post(
    "/knowledge-sources/{source_id}/refresh",
    response_model=IngestionRunResponse,
    status_code=202,
)
async def refresh_source(
    source_id: uuid.UUID,
    payload: SourceRefreshRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    queue = runtime.get("ingestion_queue")
    if queue is None:
        raise HTTPException(status_code=503, detail="Ingestion queue is unavailable.")
    request_hash = hashlib.sha256(
        json.dumps(
            {"source_id": str(source_id), "auto_activate": payload.auto_activate},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    try:
        run, version, created = await runtime["catalog_service"].call(
            runtime["catalog_repository"].create_refresh_job,
            principal.tenant_id,
            source_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            correlation_id=request.state.correlation_id,
            auto_activate=payload.auto_activate,
            max_attempts=runtime["settings"].ingestion_max_attempts,
        )
    except CatalogNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (IdempotencyConflictError, InvalidVersionTransitionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from app.ingestion.contracts import IngestionEvent, IngestionJobContract

    contract = IngestionJobContract(
        run_id=run.id,
        tenant_id=principal.tenant_id,
        source_id=source_id,
        source_version_id=version.id,
        correlation_id=request.state.correlation_id,
    )
    if run.status == "queued":
        try:
            await queue.enqueue(contract)
            if created:
                await queue.publish_event(IngestionEvent(run_id=run.id, event_type="queued"))
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Ingestion job was persisted but the queue is temporarily unavailable; "
                    "retry with the same Idempotency-Key."
                ),
            ) from exc
    await _audit(
        request,
        runtime,
        principal,
        "ingestion_run.create",
        "ingestion_run",
        run.id,
        {"source_id": str(source_id), "auto_activate": payload.auto_activate},
    )
    return _run_response(run)


@router.get("/ingestion-runs/{run_id}", response_model=IngestionRunResponse)
async def get_ingestion_run(
    run_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    run = await runtime["catalog_service"].call(
        runtime["catalog_repository"].get_ingestion_run, principal.tenant_id, run_id
    )
    if run is None:
        await _audit_denied(request, runtime, principal, "ingestion_run", run_id)
        raise HTTPException(status_code=404, detail="Ingestion run was not found.")
    await _audit(request, runtime, principal, "ingestion_run.read", "ingestion_run", run_id)
    return _run_response(run)


@router.get("/ingestion-runs/{run_id}/events", response_model=list[IngestionEventResponse])
async def get_ingestion_events(
    run_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    run = await runtime["catalog_service"].call(
        runtime["catalog_repository"].get_ingestion_run, principal.tenant_id, run_id
    )
    if run is None:
        await _audit_denied(request, runtime, principal, "ingestion_run", run_id)
        raise HTTPException(status_code=404, detail="Ingestion run was not found.")
    queue = runtime.get("ingestion_queue")
    if queue is None:
        raise HTTPException(status_code=503, detail="Ingestion events are unavailable.")
    events = await queue.list_events(run_id)
    await _audit(request, runtime, principal, "ingestion_run.events", "ingestion_run", run_id)
    return events


@router.post("/ingestion-runs/{run_id}/cancel", response_model=IngestionRunResponse)
async def cancel_ingestion_run(
    run_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        changed = await runtime["catalog_service"].call(
            runtime["catalog_repository"].request_ingestion_cancel,
            principal.tenant_id,
            run_id,
        )
    except CatalogNotFoundError as exc:
        await _audit_denied(request, runtime, principal, "ingestion_run", run_id)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=409, detail="Completed ingestion cannot be cancelled.")
    run = await runtime["catalog_service"].call(
        runtime["catalog_repository"].get_ingestion_run, principal.tenant_id, run_id
    )
    await _audit(request, runtime, principal, "ingestion_run.cancel", "ingestion_run", run_id)
    return _run_response(run)


async def _activate(
    source_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    principal: Principal,
    runtime: dict,
    action: str,
):
    try:
        version = await runtime["catalog_service"].call(
            runtime["catalog_repository"].activate_version,
            principal.tenant_id,
            source_id,
            version_id,
        )
    except CatalogNotFoundError as exc:
        await _audit_denied(request, runtime, principal, "source_version", version_id)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidVersionTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _audit(request, runtime, principal, action, "source_version", version_id)
    return _version_response(version)


@router.post(
    "/knowledge-sources/{source_id}/versions/{version_id}/activate",
    response_model=SourceVersionResponse,
)
async def activate_version(
    source_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    return await _activate(
        source_id, version_id, request, principal, runtime, "source_version.activate"
    )


@router.post(
    "/knowledge-sources/{source_id}/versions/{version_id}/rollback",
    response_model=SourceVersionResponse,
)
async def rollback_version(
    source_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    return await _activate(
        source_id, version_id, request, principal, runtime, "source_version.rollback"
    )
    IngestionEventResponse,
    SourceRefreshRequest,
