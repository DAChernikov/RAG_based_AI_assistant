from __future__ import annotations

import csv
import io
import uuid
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from app.api.dependencies import get_runtime_state, require_admin
from app.api.routes.catalog import _audit
from app.auth.security import Principal
from app.concurrency import api_blocking_io

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
    base_url: AnyHttpUrl | None = None
    endpoint_ref: str | None = Field(default=None, pattern=r"^endpoint:[A-Za-z0-9_.:/-]+$")
    credential_ref: str | None = Field(default=None, pattern=r"^credential:[A-Za-z0-9_.:/-]+$")
    capabilities: dict = Field(default_factory=dict)


class PromptInput(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    version: str = Field(min_length=1, max_length=100)
    template: str = Field(min_length=1, max_length=50_000)


class OllamaPullInput(BaseModel):
    model: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:/-]+$")


def _ollama_root(base_url: str) -> str:
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


async def _ollama_request(runtime, model, method: str, path: str, payload: dict | None = None):
    if (model.capabilities or {}).get("provider") != "ollama":
        raise HTTPException(status_code=422, detail="This model is not configured as Ollama.")
    from app.operations.runtime_registry import ResolvedModel

    resolved = ResolvedModel(
        model.id,
        model.role,
        model.model_id,
        model.version,
        model.endpoint_ref,
        model.credential_ref,
        model.capabilities or {},
        model.tenant_id or uuid.UUID(int=0),
    )
    base_url, token = await api_blocking_io.call(runtime["runtime_registry"].endpoint, resolved)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = runtime.get("ollama_http_transport")
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(600.0, connect=10.0, read=600.0),
            trust_env=False,
        ) as client:
            response = await client.request(
                method,
                f"{_ollama_root(base_url)}{path}",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Ollama endpoint is unavailable.") from exc


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
            "base_url": (row.capabilities or {}).get("base_url"),
            "credential_ref": row.credential_ref,
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
    capabilities = dict(payload.capabilities)
    if payload.base_url is not None:
        capabilities["base_url"] = str(payload.base_url).rstrip("/")
    values = {
        "role": payload.role,
        "model_id": payload.model_id,
        "version": payload.version,
        "endpoint_ref": payload.endpoint_ref or f"endpoint:{payload.role}",
        "credential_ref": payload.credential_ref,
        "capabilities": capabilities,
    }
    try:
        row = await runtime["catalog_service"].call(
            runtime["operations_repository"].create_model,
            principal.tenant_id,
            values,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _audit(request, runtime, principal, "model.create", "model_definition", row.id)
    return {"id": row.id, "role": row.role, "model_id": row.model_id, "is_active": False}


@router.post("/models/{model_id}/test")
async def test_model(
    model_id: uuid.UUID,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].get_model, model_id
    )
    if row is None or row.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Model definition was not found.")
    resolved = (
        runtime["runtime_registry"].model(principal.tenant_id, row.role) if row.is_active else None
    )
    if resolved is None:
        from app.operations.runtime_registry import ResolvedModel

        resolved = ResolvedModel(
            row.id,
            row.role,
            row.model_id,
            row.version,
            row.endpoint_ref,
            row.credential_ref,
            row.capabilities or {},
            principal.tenant_id,
        )
    try:
        if row.role == "generation" and (row.capabilities or {}).get("provider") == "ollama":
            catalog = await _ollama_request(runtime, row, "GET", "/api/tags")
            installed = {
                item.get("name")
                for item in catalog.get("models", [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            requested = row.model_id
            aliases = {requested, f"{requested}:latest"} if ":" not in requested else {requested}
            if installed.isdisjoint(aliases):
                return {
                    "status": "model_missing",
                    "model_id": row.model_id,
                    "version": row.version,
                    "message": "The configured model is not installed in Ollama.",
                }
        if row.role == "generation":
            client = await api_blocking_io.call(
                runtime["runtime_registry"].generation_client, resolved
            )
            try:
                result = await client.readiness()
            finally:
                await client.aclose()
        elif row.role == "embedding":
            client = await api_blocking_io.call(
                runtime["runtime_registry"].embedding_client, resolved
            )
            try:
                result = await client.readiness()
            finally:
                await client.close()
        else:
            client = await api_blocking_io.call(
                runtime["runtime_registry"].reranker_client, resolved
            )
            try:
                result = await client.readiness()
            finally:
                await client.close()
    except Exception as exc:
        from app.operations.runtime_registry import RuntimeConfigurationError

        if isinstance(exc, RuntimeConfigurationError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "unavailable", "error": "Model endpoint is unavailable."}
    state = result.get("status")
    ready = bool(result.get("ready") or state == "ready")
    status = (
        "ready"
        if ready
        else (
            "model_loading"
            if state in {"loading", "model_loading"}
            else "model_missing" if state == "model_missing" else "unavailable"
        )
    )
    return {
        "status": status,
        "model_id": row.model_id,
        "version": row.version,
    }


@router.post("/models/{model_id}/activate")
async def activate_model(
    model_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].get_model, model_id
    )
    if row is None or row.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Model definition was not found.")
    if row.role == "generation":
        check = await test_model(model_id, principal, runtime)
        if check.get("status") != "ready":
            raise HTTPException(
                status_code=409,
                detail="The generation model must pass its connection test before activation.",
            )
    try:
        row = await runtime["catalog_service"].call(
            runtime["operations_repository"].activate_model, principal.tenant_id, model_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _audit(request, runtime, principal, "model.activate", "model_definition", model_id)
    return {"id": row.id, "role": row.role, "model_id": row.model_id, "is_active": True}


@router.get("/models/{model_id}/ollama/models")
async def ollama_models(
    model_id: uuid.UUID,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].get_model, model_id
    )
    if row is None or row.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Model definition was not found.")
    result = await _ollama_request(runtime, row, "GET", "/api/tags")
    return [
        {
            "name": item.get("name", ""),
            "size": item.get("size"),
            "modified_at": item.get("modified_at"),
            "details": item.get("details") or {},
        }
        for item in result.get("models", [])
        if item.get("name")
    ]


@router.post("/models/{model_id}/ollama/pull")
async def ollama_pull(
    model_id: uuid.UUID,
    payload: OllamaPullInput,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].get_model, model_id
    )
    if row is None or row.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Model definition was not found.")
    result = await _ollama_request(
        runtime, row, "POST", "/api/pull", {"name": payload.model, "stream": False}
    )
    await _audit(
        request,
        runtime,
        principal,
        "ollama.model.pull",
        "model_definition",
        row.id,
        {"model_id": payload.model},
    )
    return {"status": result.get("status", "success"), "model": payload.model}


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
