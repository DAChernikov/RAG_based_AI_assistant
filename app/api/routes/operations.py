from __future__ import annotations

import csv
import io
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import get_runtime_state, require_admin
from app.api.routes.catalog import _audit
from app.auth.security import Principal

router = APIRouter(prefix="/v1/admin", tags=["operations"])


class ScheduleInput(BaseModel):
    source_id: uuid.UUID
    interval_seconds: int = Field(ge=300, le=31_536_000)
    is_enabled: bool = True


class RetentionInput(BaseModel):
    source_versions_days: int = Field(ge=1, le=3650)
    index_versions_days: int = Field(ge=1, le=3650)
    run_history_days: int = Field(ge=1, le=3650)
    audit_days: int = Field(ge=30, le=3650)


class ModelInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    role: Literal["generation", "embedding", "reranker"]
    model_id: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=100)
    endpoint_ref: str = Field(pattern=r"^endpoint:[A-Za-z0-9_.:/-]+$")
    credential_ref: str | None = Field(default=None, pattern=r"^credential:[A-Za-z0-9_.:/-]+$")
    capabilities: dict = Field(default_factory=dict)


class PromptInput(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    version: str = Field(min_length=1, max_length=100)
    template: str = Field(min_length=1, max_length=50_000)


def _schedule(row):
    return {
        "id": row.id,
        "source_id": row.source_id,
        "interval_seconds": row.interval_seconds,
        "is_enabled": row.is_enabled,
        "next_run_at": row.next_run_at,
        "last_run_at": row.last_run_at,
    }


def _policy(row):
    return {
        "source_versions_days": row.source_versions_days,
        "index_versions_days": row.index_versions_days,
        "run_history_days": row.run_history_days,
        "audit_days": row.audit_days,
    }


@router.get("/schedules")
async def schedules(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    rows = await runtime["catalog_service"].call(
        runtime["operations_repository"].list_schedules, principal.tenant_id, offset, limit
    )
    return [_schedule(row) for row in rows]


@router.put("/schedules/{source_id}")
async def put_schedule(
    source_id: uuid.UUID,
    payload: ScheduleInput,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    if payload.source_id != source_id:
        raise HTTPException(status_code=422, detail="Path and payload source IDs differ.")
    try:
        row = await runtime["catalog_service"].call(
            runtime["operations_repository"].upsert_schedule,
            principal.tenant_id,
            source_id,
            payload.interval_seconds,
            payload.is_enabled,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _audit(request, runtime, principal, "schedule.upsert", "source_schedule", row.id)
    return _schedule(row)


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    changed = await runtime["catalog_service"].call(
        runtime["operations_repository"].delete_schedule, principal.tenant_id, schedule_id
    )
    if not changed:
        raise HTTPException(status_code=404, detail="Schedule was not found.")
    await _audit(request, runtime, principal, "schedule.delete", "source_schedule", schedule_id)


@router.get("/retention")
async def get_retention(
    principal: Principal = Depends(require_admin), runtime=Depends(get_runtime_state)
):
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].get_retention_policy, principal.tenant_id
    )
    return _policy(row)


@router.put("/retention")
async def update_retention(
    payload: RetentionInput,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].update_retention_policy,
        principal.tenant_id,
        payload.model_dump(),
    )
    await _audit(request, runtime, principal, "retention.update", "tenant", principal.tenant_id)
    return _policy(row)


@router.post("/retention/{action}")
async def retention_action(
    action: Literal["dry-run", "run"],
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    method = (
        runtime["operations_repository"].retention_candidates
        if action == "dry-run"
        else runtime["operations_repository"].execute_retention
    )
    result = await runtime["catalog_service"].call(method, principal.tenant_id, 500)
    await _audit(request, runtime, principal, f"retention.{action}", "tenant", principal.tenant_id)
    return {key: [str(item) for item in values] for key, values in result.items()}


@router.get("/models")
async def list_models(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    rows = await runtime["catalog_service"].call(
        runtime["operations_repository"].list_models, principal.tenant_id, offset, limit
    )
    return [
        {
            "id": row.id,
            "role": row.role,
            "model_id": row.model_id,
            "version": row.version,
            "endpoint_ref": row.endpoint_ref,
            "capabilities": row.capabilities,
            "is_active": row.is_active,
        }
        for row in rows
    ]


@router.post("/models", status_code=201)
async def create_model(
    payload: ModelInput,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].create_model,
        principal.tenant_id,
        payload.model_dump(),
    )
    await _audit(request, runtime, principal, "model.create", "model_definition", row.id)
    return {"id": row.id, "role": row.role, "model_id": row.model_id, "is_active": False}


@router.post("/models/{model_id}/activate")
async def activate_model(
    model_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        row = await runtime["catalog_service"].call(
            runtime["operations_repository"].activate_model, principal.tenant_id, model_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _audit(request, runtime, principal, "model.activate", "model_definition", model_id)
    return {"id": row.id, "role": row.role, "model_id": row.model_id, "is_active": True}


@router.get("/prompts")
async def list_prompts(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    rows = await runtime["catalog_service"].call(
        runtime["operations_repository"].list_prompts, principal.tenant_id, offset, limit
    )
    return [
        {
            "id": row.id,
            "name": row.name,
            "version": row.version,
            "checksum": row.checksum,
            "is_active": row.is_active,
        }
        for row in rows
    ]


@router.post("/prompts", status_code=201)
async def create_prompt(
    payload: PromptInput,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].create_prompt,
        principal.tenant_id,
        payload.model_dump(),
    )
    await _audit(request, runtime, principal, "prompt.create", "prompt_template", row.id)
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "checksum": row.checksum,
        "is_active": False,
    }


@router.post("/prompts/{prompt_id}/activate")
async def activate_prompt(
    prompt_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        row = await runtime["catalog_service"].call(
            runtime["operations_repository"].activate_prompt, principal.tenant_id, prompt_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _audit(request, runtime, principal, "prompt.activate", "prompt_template", prompt_id)
    return {"id": row.id, "name": row.name, "version": row.version, "is_active": True}


@router.get("/audit-events")
async def audit_events(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    rows = await runtime["catalog_service"].call(
        runtime["operations_repository"].list_audit, principal.tenant_id, offset, limit
    )
    return [
        {
            "id": row.id,
            "actor_user_id": row.actor_user_id,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "correlation_id": row.correlation_id,
            "outcome": row.outcome,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/audit-events/export")
async def export_audit_events(
    limit: int = Query(1000, ge=1, le=10_000),
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    rows = await runtime["catalog_service"].call(
        runtime["operations_repository"].list_audit, principal.tenant_id, 0, limit
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "actor_user_id",
            "action",
            "resource_type",
            "resource_id",
            "outcome",
            "correlation_id",
            "created_at",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.id,
                row.actor_user_id,
                row.action,
                row.resource_type,
                row.resource_id,
                row.outcome,
                row.correlation_id,
                row.created_at.isoformat(),
            ]
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-events.csv"},
    )
