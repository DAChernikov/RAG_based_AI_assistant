from __future__ import annotations

import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.config import settings
from app.api.dependencies import get_runtime_state, require_admin
from app.api.routes.auth import _set_browser_cookies
from app.auth.security import Principal
from app.concurrency import api_blocking_io
from app.setup.repository import SetupClosedError

router = APIRouter(prefix="/v1/setup", tags=["setup"])


class SetupBootstrapInput(BaseModel):
    tenant_slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    tenant_name: str = Field(min_length=2, max_length=255)
    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=12, max_length=512)


class SetupProgressInput(BaseModel):
    step: Literal["models", "telegram", "readiness", "complete"]


def _setup_token() -> str | None:
    if settings.setup_bootstrap_token:
        return settings.setup_bootstrap_token
    if not settings.setup_bootstrap_token_file:
        return None
    try:
        token = Path(settings.setup_bootstrap_token_file).read_text().strip()
    except OSError:
        return None
    if (
        settings.app_env == "production"
        and token
        and (
            len(token) < 32
            or any(marker in token.lower() for marker in ("replace", "change-me", ".invalid"))
        )
    ):
        return None
    return token or None


def _public_status(raw: dict) -> dict:
    production = settings.app_env == "production"
    available = not production or bool(_setup_token())
    return {
        **raw,
        "setup_available": bool(raw["required"] and available),
        "token_required": bool(raw["required"] and production),
    }


@router.get("/status")
async def setup_status(runtime=Depends(get_runtime_state)):
    raw = await runtime["catalog_service"].call(runtime["setup_repository"].status)
    return _public_status(raw)


@router.post("/bootstrap", status_code=201)
async def setup_bootstrap(
    payload: SetupBootstrapInput,
    request: Request,
    response: Response,
    bootstrap_token: str | None = Header(default=None, alias="X-Setup-Token"),
    runtime=Depends(get_runtime_state),
):
    if settings.app_env == "production":
        expected = _setup_token()
        if not expected:
            raise HTTPException(status_code=404, detail="First-run setup is disabled.")
        if not bootstrap_token or not secrets.compare_digest(bootstrap_token, expected):
            raise HTTPException(status_code=403, detail="Invalid setup authorization.")
    service = runtime.get("auth_service")
    if service is None:
        raise HTTPException(status_code=503, detail="Authentication service is unavailable.")
    password_hash = await service._call(service.passwords.hash, payload.password)
    try:
        tenant, administrator = await runtime["catalog_service"].call(
            runtime["setup_repository"].bootstrap,
            tenant_slug=payload.tenant_slug,
            tenant_name=payload.tenant_name,
            username=payload.username,
            display_name=payload.display_name,
            password_hash=password_hash,
        )
    except SetupClosedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    principal = service.principal_for_user(administrator)
    tokens = await service._issue_session(principal, administrator.id)
    _set_browser_cookies(response, tokens["refresh_token"])
    tokens["refresh_token"] = None
    await service._call(
        runtime["auth_repository"].audit,
        tenant_id=tenant.id,
        actor_user_id=administrator.id,
        action="setup.bootstrap",
        outcome="success",
        correlation_id=request.state.correlation_id,
        resource_type="tenant",
        resource_id=str(tenant.id),
    )
    return {**tokens, "current_step": "models"}


@router.put("/progress")
async def setup_progress(
    payload: SetupProgressInput,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        result = await runtime["catalog_service"].call(
            runtime["setup_repository"].set_progress,
            principal.tenant_id,
            payload.step,
            payload.step == "complete",
        )
    except SetupClosedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit = runtime["auth_repository"].audit
    audit_call = (
        runtime["auth_service"]._call if runtime.get("auth_service") else api_blocking_io.call
    )
    await audit_call(
        audit,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action="setup.progress",
        outcome="success",
        correlation_id=request.state.correlation_id,
        resource_type="system_setup",
        resource_id="1",
        metadata={"step": payload.step},
    )
    return result
