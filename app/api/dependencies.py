import uuid

from fastapi import Depends, Header, HTTPException, Request, status

from app.api.config import settings
from app.auth.security import AuthenticationError, Principal


class AppStateError(RuntimeError):
    pass


def get_settings():
    return settings


def get_runtime_state(request: Request) -> dict:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise AppStateError("Application runtime state is not initialized.")
    return runtime


async def get_principal(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    api_key: str | None = Header(default=None, alias="X-API-Key"),
    runtime: dict = Depends(get_runtime_state),
) -> Principal:
    correlation_id = getattr(request.state, "correlation_id", uuid.uuid4())
    if settings.auth_disabled:
        if settings.app_env not in {"dev", "test"}:
            raise HTTPException(status_code=503, detail="Invalid authentication configuration.")
        try:
            tenant_id, user_id = await runtime["repository"].get_compatibility_identity(
                settings.compatibility_tenant_slug,
                settings.compatibility_user_external_id,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="Development identity is unavailable."
            ) from exc
        return Principal(
            tenant_id=tenant_id,
            user_id=user_id,
            username=settings.compatibility_user_external_id,
            role="admin",
            auth_method="disabled",
            scopes=frozenset({"*"}),
        )

    service = runtime.get("auth_service")
    if service is None:
        raise HTTPException(status_code=503, detail="Authentication service is unavailable.")
    try:
        if api_key:
            return await service.authenticate_api_key(api_key)
        if authorization and authorization.startswith("Bearer "):
            return await service.authenticate_access_token(authorization[7:].strip())
        raise AuthenticationError("Authentication credentials are required.")
    except AuthenticationError as exc:
        repository = runtime.get("auth_repository")
        if repository is not None:
            await service._call(
                repository.audit,
                tenant_id=None,
                actor_user_id=None,
                action="access.denied",
                outcome="denied",
                correlation_id=correlation_id,
                metadata={"reason": str(exc)[:100]},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_scope(scope: str):
    async def dependency(
        request: Request,
        principal: Principal = Depends(get_principal),
        runtime: dict = Depends(get_runtime_state),
    ) -> Principal:
        if not principal.has_scope(scope):
            service = runtime.get("auth_service")
            if service is not None:
                await service._call(
                    runtime["auth_repository"].audit,
                    tenant_id=principal.tenant_id,
                    actor_user_id=principal.user_id,
                    action="access.denied",
                    outcome="denied",
                    correlation_id=request.state.correlation_id,
                    metadata={"required_scope": scope},
                )
            raise HTTPException(status_code=403, detail="API key scope is insufficient.")
        return principal

    return dependency


async def require_admin(
    request: Request,
    principal: Principal = Depends(get_principal),
    runtime: dict = Depends(get_runtime_state),
) -> Principal:
    if principal.role != "admin":
        service = runtime.get("auth_service")
        if service is not None:
            await service._call(
                runtime["auth_repository"].audit,
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action="access.denied",
                outcome="denied",
                correlation_id=request.state.correlation_id,
                metadata={"required_role": "admin"},
            )
        raise HTTPException(status_code=403, detail="Administrator role is required.")
    return principal
