from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.api.config import settings
from app.api.dependencies import get_runtime_state, require_admin
from app.api.routes.catalog import _audit
from app.auth.security import Principal
from app.concurrency import api_blocking_io
from app.operations.runtime_registry import RuntimeConfigurationError
from app.secrets.store import SecretStoreError
from app.state.database import database_is_ready

router = APIRouter(prefix="/v1/admin", tags=["platform"])


class TelegramInput(BaseModel):
    enabled: bool = False
    token_credential_ref: str | None = Field(
        default=None, pattern=r"^credential:[A-Za-z0-9_.:/-]+$"
    )
    api_key_credential_ref: str | None = Field(
        default=None, pattern=r"^credential:[A-Za-z0-9_.:/-]+$"
    )


class TelegramBotInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = False
    token_credential_ref: str = Field(pattern=r"^credential:[A-Za-z0-9_.:/-]+$")
    api_key_credential_ref: str = Field(pattern=r"^credential:[A-Za-z0-9_.:/-]+$")


class TelegramBotUpdate(BaseModel):
    enabled: bool


def _telegram(row) -> dict:
    return {
        "enabled": bool(row and row.is_enabled),
        "token_credential_ref": row.token_credential_ref if row else None,
        "api_key_credential_ref": row.api_key_credential_ref if row else None,
        "config_version": row.config_version if row else 0,
        "last_test_status": row.last_test_status if row else None,
        "last_tested_at": row.last_tested_at if row else None,
    }


def _telegram_bot(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "enabled": row.is_enabled,
        "config_version": row.config_version,
        "last_test_status": row.last_test_status,
        "last_tested_at": row.last_tested_at,
    }


async def _component_status(runtime: dict, tenant_id: uuid.UUID) -> dict[str, str]:
    engine = runtime.get("database_engine")
    queue = runtime.get("queue")
    database_ready = bool(engine and await api_blocking_io.call(database_is_ready, engine))
    redis_ready = bool(queue and await queue.ping())
    heartbeat = await queue.latest_heartbeat() if redis_ready else None
    redis = getattr(queue, "redis", None)
    worker_presence = {
        "ingestion": False,
        "indexing": False,
        "scheduler": False,
        "telegram": False,
    }
    if redis_ready and redis is not None:
        worker_presence = {
            "ingestion": bool(
                await redis.exists(
                    f"{settings.ingestion_heartbeat_prefix}:{settings.ingestion_worker_id}"
                )
            ),
            "indexing": bool(
                await redis.exists(
                    f"{settings.indexing_heartbeat_prefix}:{settings.indexing_worker_id}"
                )
            ),
            "scheduler": bool(await redis.exists("rag:scheduler:leader")),
            "telegram": bool(await redis.exists("rag:bot:heartbeat")),
        }
    embedding = (
        await runtime["embedding_client"].readiness()
        if runtime.get("embedding_client")
        else {"status": "not_ready"}
    )
    generation = "not_configured"
    try:
        model = await runtime["catalog_service"].call(
            runtime["runtime_registry"].model, tenant_id, "generation"
        )
        client = await api_blocking_io.call(runtime["runtime_registry"].generation_client, model)
        try:
            ready = await client.readiness()
            generation = "ready" if ready.get("ready") else "unavailable"
        finally:
            await client.aclose()
    except (RuntimeConfigurationError, httpx.HTTPError):
        pass
    return {
        "api": "ready",
        "postgresql": "ready" if database_ready else "not_ready",
        "redis": "ready" if redis_ready else "not_ready",
        "inference_worker": "ready" if heartbeat else "not_ready",
        "ingestion_worker": "ready" if worker_presence["ingestion"] else "not_ready",
        "indexing_worker": "ready" if worker_presence["indexing"] else "not_ready",
        "scheduler": "ready" if worker_presence["scheduler"] else "not_ready",
        "telegram_worker": "ready" if worker_presence["telegram"] else "not_ready",
        "embedding": (
            "ready"
            if embedding.get("status") == "ready" and embedding.get("model_ready") is True
            else (
                "model_loading"
                if embedding.get("status") in {"loading", "model_loading"}
                else "not_ready"
            )
        ),
        "generation": generation,
    }


@router.get("/system")
async def system_status(
    principal: Principal = Depends(require_admin), runtime=Depends(get_runtime_state)
):
    components = await _component_status(runtime, principal.tenant_id)
    telegram = await runtime["catalog_service"].call(
        runtime["operations_repository"].get_telegram_configuration, principal.tenant_id
    )
    telegram_bots = await runtime["catalog_service"].call(
        runtime["operations_repository"].list_telegram_bots, principal.tenant_id
    )
    telegram_enabled = any(row.is_enabled for row in telegram_bots) or bool(
        telegram and telegram.is_enabled
    )
    if not telegram_enabled:
        components["telegram"] = "disabled"
    elif components["telegram_worker"] != "ready":
        components["telegram"] = "not_ready"
    else:
        tested = [row.last_test_status for row in telegram_bots if row.is_enabled]
        components["telegram"] = (
            "ready"
            if tested and all(status == "ready" for status in tested)
            else (telegram.last_test_status if telegram else None) or "configured"
        )
    required = (
        "api",
        "postgresql",
        "redis",
        "inference_worker",
        "ingestion_worker",
        "indexing_worker",
        "scheduler",
        "telegram_worker",
        "embedding",
        "generation",
    )
    ready = all(components[name] == "ready" for name in required)
    if telegram_enabled:
        ready = ready and components["telegram"] == "ready"
    return {"status": "ready" if ready else "degraded", "components": components}


@router.get("/diagnostics")
async def diagnostics(
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    components = await _component_status(runtime, principal.tenant_id)
    redis = getattr(runtime.get("queue"), "redis", None)
    dlq = {}
    if redis is not None:
        for name, stream in (
            ("inference", settings.inference_dlq_stream),
            ("ingestion", settings.ingestion_dlq_stream),
            ("indexing", settings.indexing_dlq_stream),
        ):
            try:
                dlq[name] = int(await redis.xlen(stream))
            except Exception:
                dlq[name] = None
    return {
        "components": components,
        "dlq": dlq,
        "startup_error": runtime.get("startup_error"),
        "correlation_id": str(request.state.correlation_id),
    }


@router.get("/settings")
async def dynamic_settings(_principal: Principal = Depends(require_admin)):
    return {
        "dynamic": [
            "model and prompt activation",
            "Telegram enablement and credential rotation",
            "source schedules",
            "retention policy",
        ],
        "restart_required": [
            "database and Redis endpoints",
            "HTTP bind and trusted proxies",
            "cookie and CORS policy",
            "worker concurrency and resource limits",
            "secret-store master key rotation",
        ],
    }


@router.get("/telegram")
async def get_telegram(
    principal: Principal = Depends(require_admin), runtime=Depends(get_runtime_state)
):
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].get_telegram_configuration, principal.tenant_id
    )
    return _telegram(row)


@router.get("/telegram-bots")
async def list_telegram_bots(
    principal: Principal = Depends(require_admin), runtime=Depends(get_runtime_state)
):
    rows = await runtime["catalog_service"].call(
        runtime["operations_repository"].list_telegram_bots, principal.tenant_id
    )
    return [_telegram_bot(row) for row in rows]


@router.post("/telegram-bots", status_code=201)
async def create_telegram_bot(
    payload: TelegramBotInput,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    for reference in (payload.token_credential_ref, payload.api_key_credential_ref):
        try:
            await runtime["catalog_service"].call(
                runtime["secret_store"].resolve, principal.tenant_id, reference
            )
        except SecretStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        row = await runtime["catalog_service"].call(
            runtime["operations_repository"].create_telegram_bot,
            principal.tenant_id,
            name=payload.name,
            is_enabled=payload.enabled,
            token_credential_ref=payload.token_credential_ref,
            api_key_credential_ref=payload.api_key_credential_ref,
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Telegram bot name already exists.") from exc
    await _audit(
        request,
        runtime,
        principal,
        "telegram.bot.create",
        "telegram_bot_configuration",
        row.id,
        {"enabled": row.is_enabled},
    )
    return _telegram_bot(row)


@router.patch("/telegram-bots/{bot_id}")
async def update_telegram_bot(
    bot_id: uuid.UUID,
    payload: TelegramBotUpdate,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        row = await runtime["catalog_service"].call(
            runtime["operations_repository"].update_telegram_bot,
            principal.tenant_id,
            bot_id,
            is_enabled=payload.enabled,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _audit(
        request,
        runtime,
        principal,
        "telegram.bot.update",
        "telegram_bot_configuration",
        bot_id,
        {"enabled": row.is_enabled, "config_version": row.config_version},
    )
    return _telegram_bot(row)


@router.delete("/telegram-bots/{bot_id}", status_code=204)
async def delete_telegram_bot(
    bot_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    changed = await runtime["catalog_service"].call(
        runtime["operations_repository"].delete_telegram_bot,
        principal.tenant_id,
        bot_id,
    )
    if not changed:
        raise HTTPException(status_code=404, detail="Telegram bot was not found.")
    await _audit(
        request,
        runtime,
        principal,
        "telegram.bot.delete",
        "telegram_bot_configuration",
        bot_id,
    )


@router.post("/telegram-bots/{bot_id}/test")
async def test_named_telegram_bot(
    bot_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].get_telegram_bot,
        principal.tenant_id,
        bot_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Telegram bot was not found.")
    token = await runtime["catalog_service"].call(
        runtime["secret_store"].resolve,
        principal.tenant_id,
        row.token_credential_ref,
    )
    status = "unavailable"
    client = runtime.get("telegram_test_client")
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), trust_env=False)
    try:
        response = await client.get(f"https://api.telegram.org/bot{token['value']}/getMe")
        status = "ready" if response.is_success and response.json().get("ok") else "unavailable"
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    finally:
        if owns_client:
            await client.aclose()
    await runtime["catalog_service"].call(
        runtime["operations_repository"].record_telegram_bot_test,
        principal.tenant_id,
        bot_id,
        status,
    )
    await _audit(
        request,
        runtime,
        principal,
        "telegram.bot.test",
        "telegram_bot_configuration",
        bot_id,
        {"status": status},
    )
    return {"status": status}


@router.put("/telegram")
async def put_telegram(
    payload: TelegramInput,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    if payload.enabled and not (payload.token_credential_ref and payload.api_key_credential_ref):
        raise HTTPException(
            status_code=422,
            detail="Enabled Telegram requires bot-token and scoped API-key references.",
        )
    for reference in (payload.token_credential_ref, payload.api_key_credential_ref):
        if reference:
            try:
                await runtime["catalog_service"].call(
                    runtime["secret_store"].resolve, principal.tenant_id, reference
                )
            except SecretStoreError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].upsert_telegram_configuration,
        principal.tenant_id,
        is_enabled=payload.enabled,
        token_credential_ref=payload.token_credential_ref,
        api_key_credential_ref=payload.api_key_credential_ref,
    )
    await _audit(
        request,
        runtime,
        principal,
        "telegram.configure",
        "telegram_configuration",
        principal.tenant_id,
        {"enabled": payload.enabled, "config_version": row.config_version},
    )
    return _telegram(row)


@router.post("/telegram/test")
async def test_telegram(
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    row = await runtime["catalog_service"].call(
        runtime["operations_repository"].get_telegram_configuration, principal.tenant_id
    )
    if row is None or not row.token_credential_ref:
        raise HTTPException(status_code=409, detail="Telegram bot token is not configured.")
    token = await runtime["catalog_service"].call(
        runtime["secret_store"].resolve, principal.tenant_id, row.token_credential_ref
    )
    status = "unavailable"
    client = runtime.get("telegram_test_client")
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), trust_env=False)
    try:
        response = await client.get(f"https://api.telegram.org/bot{token['value']}/getMe")
        status = "ready" if response.is_success and response.json().get("ok") else "unavailable"
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    finally:
        if owns_client:
            await client.aclose()
    await runtime["catalog_service"].call(
        runtime["operations_repository"].record_telegram_test, principal.tenant_id, status
    )
    await _audit(
        request,
        runtime,
        principal,
        "telegram.test",
        "telegram_configuration",
        principal.tenant_id,
        {"status": status},
    )
    return {"status": status}
