import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.auth_schemas import APIKeyCreatedResponse, APIKeyCreateRequest, APIKeyResponse
from app.api.dependencies import get_principal, get_runtime_state
from app.auth.security import Principal

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


def _response(item, api_key: str | None = None):
    values = {
        "id": item.id,
        "name": item.name,
        "prefix": item.prefix,
        "scopes": item.scopes,
        "expires_at": item.expires_at,
        "last_used_at": item.last_used_at,
        "revoked_at": item.revoked_at,
        "created_at": item.created_at,
    }
    return APIKeyCreatedResponse(api_key=api_key, **values) if api_key else APIKeyResponse(**values)


def _require_session(principal: Principal) -> None:
    if principal.auth_method == "api_key":
        raise HTTPException(
            status_code=403,
            detail="API keys can only be managed with an access token.",
        )


@router.post("", response_model=APIKeyCreatedResponse, status_code=201)
async def create_api_key(
    payload: APIKeyCreateRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    runtime=Depends(get_runtime_state),
):
    _require_session(principal)
    try:
        item, value = await runtime["auth_service"].create_api_key(
            principal, payload.name, payload.scopes, payload.expires_at
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await runtime["auth_service"]._call(
        runtime["auth_repository"].audit,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action="api_key.create",
        outcome="success",
        correlation_id=request.state.correlation_id,
        resource_type="api_key",
        resource_id=str(item.id),
        metadata={"scopes": item.scopes},
    )
    return _response(item, value)


@router.get("", response_model=list[APIKeyResponse])
async def list_api_keys(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    runtime=Depends(get_runtime_state),
):
    _require_session(principal)
    items = await runtime["auth_service"]._call(runtime["auth_repository"].list_api_keys, principal)
    return [_response(item) for item in items[offset : offset + limit]]


@router.delete("/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    runtime=Depends(get_runtime_state),
):
    _require_session(principal)
    item = await runtime["auth_service"]._call(
        runtime["auth_repository"].revoke_api_key, principal, key_id
    )
    if item is None:
        raise HTTPException(status_code=404, detail="API key was not found.")
    await runtime["auth_service"]._call(
        runtime["auth_repository"].audit,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action="api_key.revoke",
        outcome="success",
        correlation_id=request.state.correlation_id,
        resource_type="api_key",
        resource_id=str(item.id),
    )
