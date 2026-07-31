from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class AuthenticationError(RuntimeError):
    pass


class TokenReuseError(AuthenticationError):
    pass


@dataclass(frozen=True)
class Principal:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    role: str
    auth_method: str
    scopes: frozenset[str]
    credential_id: uuid.UUID | None = None

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


class PasswordManager:
    def __init__(self) -> None:
        self.hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
        self._dummy_hash = self.hasher.hash("constant-time-dummy-password")

    def hash(self, password: str) -> str:
        if len(password) < 12:
            raise ValueError("Password must contain at least 12 characters.")
        return self.hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        candidate = password_hash or self._dummy_hash
        try:
            valid = self.hasher.verify(candidate, password)
            return bool(valid and password_hash)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


class JWTManager:
    def __init__(
        self,
        secret: str,
        issuer: str,
        audience: str,
        ttl_seconds: int,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters.")
        self.secret = secret
        self.issuer = issuer
        self.audience = audience
        self.ttl_seconds = ttl_seconds

    def issue(self, principal: Principal) -> tuple[str, datetime]:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        payload = {
            "sub": str(principal.user_id),
            "tenant_id": str(principal.tenant_id),
            "username": principal.username,
            "role": principal.role,
            "type": "access",
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": expires_at,
            "iss": self.issuer,
            "aud": self.audience,
        }
        return jwt.encode(payload, self.secret, algorithm="HS256"), expires_at

    def decode(self, token: str) -> dict:
        try:
            payload = jwt.decode(
                token,
                self.secret,
                algorithms=["HS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["sub", "tenant_id", "type", "exp", "iat"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError("Invalid or expired access token.") from exc
        if payload.get("type") != "access":
            raise AuthenticationError("Invalid access token type.")
        return payload


def generate_refresh_token() -> str:
    return f"rrt_{secrets.token_urlsafe(48)}"


def generate_api_key() -> str:
    return f"rag_{secrets.token_urlsafe(40)}"


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
