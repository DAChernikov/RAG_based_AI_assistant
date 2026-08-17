import secrets

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response

from app.api.auth_schemas import (
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    TokenPairResponse,
)
from app.api.config import settings
from app.api.dependencies import get_principal, get_runtime_state
from app.auth.security import AuthenticationError, Principal, TokenReuseError

router = APIRouter(prefix="/v1/auth", tags=["authentication"])


def _service(runtime):
    service = runtime.get("auth_service")
    if service is None:
        raise HTTPException(status_code=503, detail="Authentication service is unavailable.")
    return service


def _set_browser_cookies(response: Response, refresh_token: str) -> str:
    csrf = secrets.token_urlsafe(32)
    response.set_cookie(
        "rag_refresh",
        refresh_token,
        max_age=settings.refresh_token_ttl_sec,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/v1/auth",
    )
    response.set_cookie(
        "rag_csrf",
        csrf,
        max_age=settings.refresh_token_ttl_sec,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )
    return csrf


def _cookie_token(
    body_token: str | None,
    cookie_token: str | None,
    csrf_cookie: str | None,
    csrf_header: str | None,
) -> tuple[str, bool]:
    if body_token:
        return body_token, False
    if not cookie_token:
        raise HTTPException(status_code=401, detail="Refresh token is required.")
    if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")
    return cookie_token, True


@router.post("/login", response_model=TokenPairResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    runtime=Depends(get_runtime_state),
):
    try:
        result = await _service(runtime).login(
            payload.tenant_slug,
            payload.username,
            payload.password,
            request.state.correlation_id,
        )
        if payload.use_cookie:
            _set_browser_cookies(response, result["refresh_token"])
            result["refresh_token"] = None
        return result
    except AuthenticationError as exc:
        status_code = 429 if "Too many" in str(exc) else 401
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    response: Response,
    rag_refresh: str | None = Cookie(default=None),
    rag_csrf: str | None = Cookie(default=None),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    runtime=Depends(get_runtime_state),
):
    try:
        token, cookie_mode = _cookie_token(
            payload.refresh_token, rag_refresh, rag_csrf, csrf_header
        )
        result = await _service(runtime).refresh(token, request.state.correlation_id)
        if cookie_mode:
            _set_browser_cookies(response, result["refresh_token"])
            result["refresh_token"] = None
        return result
    except (AuthenticationError, TokenReuseError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/logout", status_code=204)
async def logout(
    payload: LogoutRequest,
    request: Request,
    response: Response,
    rag_refresh: str | None = Cookie(default=None),
    rag_csrf: str | None = Cookie(default=None),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    runtime=Depends(get_runtime_state),
):
    token, _cookie_mode = _cookie_token(payload.refresh_token, rag_refresh, rag_csrf, csrf_header)
    await _service(runtime).logout(token, request.state.correlation_id)
    response.delete_cookie("rag_refresh", path="/v1/auth")
    response.delete_cookie("rag_csrf", path="/")


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
