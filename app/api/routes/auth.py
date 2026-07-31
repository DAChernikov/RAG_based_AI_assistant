from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth_schemas import (
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    TokenPairResponse,
)
from app.api.dependencies import get_principal, get_runtime_state
from app.auth.security import AuthenticationError, Principal, TokenReuseError

router = APIRouter(prefix="/v1/auth", tags=["authentication"])


def _service(runtime):
    service = runtime.get("auth_service")
    if service is None:
        raise HTTPException(status_code=503, detail="Authentication service is unavailable.")
    return service


@router.post("/login", response_model=TokenPairResponse)
async def login(payload: LoginRequest, request: Request, runtime=Depends(get_runtime_state)):
    try:
        return await _service(runtime).login(
            payload.tenant_slug,
            payload.username,
            payload.password,
            request.state.correlation_id,
        )
    except AuthenticationError as exc:
        status_code = 429 if "Too many" in str(exc) else 401
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(payload: RefreshRequest, request: Request, runtime=Depends(get_runtime_state)):
    try:
        return await _service(runtime).refresh(payload.refresh_token, request.state.correlation_id)
    except (AuthenticationError, TokenReuseError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/logout", status_code=204)
async def logout(payload: LogoutRequest, request: Request, runtime=Depends(get_runtime_state)):
    await _service(runtime).logout(payload.refresh_token, request.state.correlation_id)


@router.get("/me", response_model=MeResponse)
async def me(principal: Principal = Depends(get_principal)):
    return MeResponse(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        username=principal.username,
        role=principal.role,
        auth_method=principal.auth_method,
        scopes=sorted(principal.scopes),
    )
