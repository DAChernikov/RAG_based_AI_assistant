import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    tenant_slug: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)
    use_cookie: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=20, max_length=500)


class LogoutRequest(RefreshRequest):
    pass


class TokenPairResponse(BaseModel):
    access_token: str
    access_expires_at: datetime
    refresh_token: str | None
    refresh_expires_at: datetime
    token_type: str


class MeResponse(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    username: str
    role: str
    auth_method: str
    scopes: list[str]


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=12, max_length=500)
    role: str = Field(default="user", pattern=r"^(admin|user)$")


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=12, max_length=500)
    role: str | None = Field(default=None, pattern=r"^(admin|user)$")
    is_active: bool | None = None


class UserResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    username: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime


class APIKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=lambda: ["inference:read", "inference:write"])
    expires_at: datetime | None = None


class APIKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class APIKeyCreatedResponse(APIKeyResponse):
    api_key: str
