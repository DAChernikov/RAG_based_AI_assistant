from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from app.api.config import settings
from app.auth.security import (
    AuthenticationError,
    JWTManager,
    PasswordManager,
    Principal,
    TokenReuseError,
    generate_api_key,
    generate_refresh_token,
    secret_hash,
)
from app.state.auth_repository import AuthRepository

ALL_INFERENCE_SCOPES = frozenset({"profile:read", "inference:read", "inference:write"})


class LoginRateLimiter:
    def __init__(self, attempts: int, window_seconds: int):
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.history: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        bucket = self.history[key]
        while bucket and bucket[0] <= now - self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.attempts:
            raise AuthenticationError("Too many login attempts. Try again later.")
        bucket.append(now)

    def reset(self, key: str) -> None:
        self.history.pop(key, None)


class AuthService:
    def __init__(self, repository: AuthRepository):
        self.repository = repository
        self.passwords = PasswordManager()
        self.jwt = JWTManager(
            settings.jwt_secret or "",
            settings.jwt_issuer,
            settings.jwt_audience,
            settings.access_token_ttl_sec,
        )
        self.rate_limiter = LoginRateLimiter(
            settings.login_rate_limit_attempts,
            settings.login_rate_limit_window_sec,
        )

    async def _call(self, method, *args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)

    def principal_for_user(self, user, method: str = "jwt") -> Principal:
        return Principal(
            tenant_id=user.tenant_id,
            user_id=user.id,
            username=user.username,
            role=user.role,
            auth_method=method,
            scopes=frozenset({"*"}),
        )

    async def login(
        self, tenant_slug: str, username: str, password: str, correlation_id: uuid.UUID
    ) -> dict:
        rate_key = f"{tenant_slug}:{username}"
        self.rate_limiter.check(rate_key)
        user = await self._call(self.repository.find_user_for_login, tenant_slug, username)
        if (
            user is None
            or not user.is_active
            or not user.password_hash
            or not self.passwords.verify(user.password_hash, password)
        ):
            await self._call(
                self.repository.audit,
                tenant_id=user.tenant_id if user else None,
                actor_user_id=user.id if user else None,
                action="auth.login",
                outcome="failure",
                correlation_id=correlation_id,
            )
            raise AuthenticationError("Invalid credentials.")
        self.rate_limiter.reset(rate_key)
        await self._call(self.repository.mark_login, user.id)
        principal = self.principal_for_user(user)
        result = await self._issue_session(principal, user.id)
        await self._call(
            self.repository.audit,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            action="auth.login",
            outcome="success",
            correlation_id=correlation_id,
        )
        return result

    async def _issue_session(
        self, principal: Principal, user_id: uuid.UUID, family_id: uuid.UUID | None = None
    ) -> dict:
        access_token, access_expires_at = self.jwt.issue(principal)
        refresh_token = generate_refresh_token()
        refresh_expires_at = datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_sec)
        await self._call(
            self.repository.create_refresh_session,
            user_id,
            family_id or uuid.uuid4(),
            secret_hash(refresh_token),
            refresh_expires_at,
        )
        return {
            "access_token": access_token,
            "access_expires_at": access_expires_at,
            "refresh_token": refresh_token,
            "refresh_expires_at": refresh_expires_at,
            "token_type": "bearer",
        }

    async def refresh(self, refresh_token: str, correlation_id: uuid.UUID) -> dict:
        new_refresh = generate_refresh_token()
        expires_at = datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_sec)
        try:
            session, user = await self._call(
                self.repository.rotate_refresh_session,
                secret_hash(refresh_token),
                secret_hash(new_refresh),
                expires_at,
            )
        except TokenReuseError:
            await self._call(
                self.repository.audit,
                tenant_id=None,
                actor_user_id=None,
                action="auth.refresh",
                outcome="denied",
                correlation_id=correlation_id,
            )
            raise
        principal = self.principal_for_user(user)
        access_token, access_expires_at = self.jwt.issue(principal)
        await self._call(
            self.repository.audit,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            action="auth.refresh",
            outcome="success",
            correlation_id=correlation_id,
            resource_type="refresh_session",
            resource_id=str(session.id),
        )
        return {
            "access_token": access_token,
            "access_expires_at": access_expires_at,
            "refresh_token": new_refresh,
            "refresh_expires_at": expires_at,
            "token_type": "bearer",
        }

    async def logout(self, refresh_token: str, correlation_id: uuid.UUID) -> None:
        session = await self._call(
            self.repository.revoke_refresh_session, secret_hash(refresh_token)
        )
        await self._call(
            self.repository.audit,
            tenant_id=None,
            actor_user_id=session.user_id if session else None,
            action="auth.logout",
            outcome="success",
            correlation_id=correlation_id,
        )

    async def authenticate_access_token(self, token: str) -> Principal:
        payload = self.jwt.decode(token)
        tenant_id = uuid.UUID(payload["tenant_id"])
        user_id = uuid.UUID(payload["sub"])
        user = await self._call(self.repository.get_active_user, tenant_id, user_id)
        if user is None:
            raise AuthenticationError("Access token user is inactive.")
        return self.principal_for_user(user)

    async def authenticate_api_key(self, value: str) -> Principal:
        result = await self._call(self.repository.authenticate_api_key, secret_hash(value))
        if result is None:
            raise AuthenticationError("Invalid or revoked API key.")
        item, user = result
        return Principal(
            tenant_id=user.tenant_id,
            user_id=user.id,
            username=user.username,
            role=user.role,
            auth_method="api_key",
            scopes=frozenset(item.scopes),
            credential_id=item.id,
        )

    async def create_api_key(
        self,
        principal: Principal,
        name: str,
        scopes: list[str],
        expires_at: datetime | None,
    ) -> tuple[object, str]:
        unknown = set(scopes) - ALL_INFERENCE_SCOPES
        if unknown:
            raise ValueError(f"Unsupported API key scopes: {sorted(unknown)}")
        now = datetime.now(UTC)
        if expires_at is None:
            expires_at = now + timedelta(seconds=settings.api_key_default_ttl_sec)
        elif expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            raise ValueError("API key expiration must be in the future.")
        value = generate_api_key()
        item = await self._call(
            self.repository.create_api_key,
            principal,
            name,
            secret_hash(value),
            value[:12],
            sorted(set(scopes)),
            expires_at,
        )
        return item, value
