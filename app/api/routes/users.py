import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from app.api.auth_schemas import UserCreateRequest, UserResponse, UserUpdateRequest
from app.api.dependencies import get_runtime_state, require_admin
from app.auth.security import Principal

router = APIRouter(prefix="/v1/admin/users", tags=["admin-users"])


def _response(user):
    return UserResponse(
        id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
    )


@router.get("", response_model=list[UserResponse])
async def list_users(
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    users = await runtime["auth_service"]._call(
        runtime["auth_repository"].list_users, principal.tenant_id
    )
    return [_response(user) for user in users]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    user = await runtime["auth_service"]._call(
        runtime["auth_repository"].get_user, principal.tenant_id, user_id
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User was not found.")
    return _response(user)


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    try:
        password_hash = runtime["auth_service"].passwords.hash(payload.password)
        user = await runtime["auth_service"]._call(
            runtime["auth_repository"].create_user,
            principal.tenant_id,
            payload.username,
            payload.display_name,
            password_hash,
            payload.role,
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Username already exists.") from exc
    await runtime["auth_service"]._call(
        runtime["auth_repository"].audit,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action="user.create",
        outcome="success",
        correlation_id=request.state.correlation_id,
        resource_type="user",
        resource_id=str(user.id),
        metadata={"role": user.role},
    )
    return _response(user)


@router.delete("/{user_id}", status_code=204)
async def deactivate_user(
    user_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    if user_id == principal.user_id:
        raise HTTPException(status_code=409, detail="Administrator cannot deactivate itself.")
    user = await runtime["auth_service"]._call(
        runtime["auth_repository"].update_user,
        principal.tenant_id,
        user_id,
        display_name=None,
        role=None,
        is_active=False,
        password_hash=None,
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User was not found.")
    await runtime["auth_service"]._call(
        runtime["auth_repository"].audit,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action="user.deactivate",
        outcome="success",
        correlation_id=request.state.correlation_id,
        resource_type="user",
        resource_id=str(user.id),
    )


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    if user_id == principal.user_id and payload.is_active is False:
        raise HTTPException(status_code=409, detail="Administrator cannot deactivate itself.")
    password_hash = (
        runtime["auth_service"].passwords.hash(payload.password)
        if payload.password is not None
        else None
    )
    user = await runtime["auth_service"]._call(
        runtime["auth_repository"].update_user,
        principal.tenant_id,
        user_id,
        display_name=payload.display_name,
        role=payload.role,
        is_active=payload.is_active,
        password_hash=password_hash,
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User was not found.")
    await runtime["auth_service"]._call(
        runtime["auth_repository"].audit,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action="user.update",
        outcome="success",
        correlation_id=request.state.correlation_id,
        resource_type="user",
        resource_id=str(user.id),
        metadata={"role": user.role, "is_active": user.is_active},
    )
    return _response(user)
