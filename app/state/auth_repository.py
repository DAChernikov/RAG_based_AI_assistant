from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.auth.security import Principal, TokenReuseError
from app.state.models import APIKey, AuditEvent, RefreshSession, Tenant, User


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class AuthRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def find_user_for_login(self, tenant_slug: str, username: str) -> User | None:
        with self.session_factory() as session:
            return session.scalar(
                select(User)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(Tenant.slug == tenant_slug, User.username == username)
            )

    def get_active_user(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> User | None:
        with self.session_factory() as session:
            return session.scalar(
                select(User).where(
                    User.id == user_id,
                    User.tenant_id == tenant_id,
                    User.is_active.is_(True),
                )
            )

    def mark_login(self, user_id: uuid.UUID) -> None:
        with self.session_factory.begin() as session:
            user = session.get(User, user_id)
            if user is not None:
                user.last_login_at = datetime.now(UTC)

    def create_refresh_session(
        self,
        user_id: uuid.UUID,
        family_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
    ) -> RefreshSession:
        with self.session_factory.begin() as session:
            item = RefreshSession(
                user_id=user_id,
                family_id=family_id,
                token_hash=token_hash,
                expires_at=expires_at,
            )
            session.add(item)
            session.flush()
            return item

    def rotate_refresh_session(
        self,
        token_hash: str,
        new_token_hash: str,
        new_expires_at: datetime,
    ) -> tuple[RefreshSession, User]:
        reuse_error: str | None = None
        result = None
        with self.session_factory.begin() as session:
            current = session.scalar(
                select(RefreshSession)
                .where(RefreshSession.token_hash == token_hash)
                .with_for_update()
            )
            now = datetime.now(UTC)
            if current is None:
                reuse_error = "Refresh token is invalid."
            elif (
                current.used_at is not None
                or current.revoked_at is not None
                or _utc(current.expires_at) <= now
            ):
                session.query(RefreshSession).filter(
                    RefreshSession.family_id == current.family_id,
                    RefreshSession.revoked_at.is_(None),
                ).update({"revoked_at": now})
                reuse_error = "Refresh token was expired, revoked, or reused."
            else:
                user = session.get(User, current.user_id)
                if user is None or not user.is_active:
                    reuse_error = "Refresh token user is inactive."
                else:
                    replacement = RefreshSession(
                        user_id=current.user_id,
                        family_id=current.family_id,
                        token_hash=new_token_hash,
                        expires_at=new_expires_at,
                    )
                    session.add(replacement)
                    session.flush()
                    current.used_at = now
                    current.revoked_at = now
                    current.replaced_by_id = replacement.id
                    result = replacement, user
        if reuse_error:
            raise TokenReuseError(reuse_error)
        return result

    def revoke_refresh_session(self, token_hash: str) -> RefreshSession | None:
        with self.session_factory.begin() as session:
            item = session.scalar(
                select(RefreshSession)
                .where(RefreshSession.token_hash == token_hash)
                .with_for_update()
            )
            if item is not None and item.revoked_at is None:
                item.revoked_at = datetime.now(UTC)
            return item

    def create_api_key(
        self,
        principal: Principal,
        name: str,
        key_hash: str,
        prefix: str,
        scopes: list[str],
        expires_at: datetime | None,
    ) -> APIKey:
        with self.session_factory.begin() as session:
            item = APIKey(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                name=name,
                key_hash=key_hash,
                prefix=prefix,
                scopes=scopes,
                expires_at=expires_at,
            )
            session.add(item)
            session.flush()
            return item

    def list_api_keys(self, principal: Principal) -> list[APIKey]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(APIKey)
                    .where(
                        APIKey.tenant_id == principal.tenant_id,
                        APIKey.user_id == principal.user_id,
                    )
                    .order_by(APIKey.created_at.desc())
                )
            )

    def authenticate_api_key(self, key_hash: str) -> tuple[APIKey, User] | None:
        with self.session_factory.begin() as session:
            now = datetime.now(UTC)
            item = session.scalar(
                select(APIKey).where(APIKey.key_hash == key_hash).with_for_update()
            )
            if (
                item is None
                or item.revoked_at is not None
                or (item.expires_at is not None and _utc(item.expires_at) <= now)
            ):
                return None
            user = session.get(User, item.user_id)
            if user is None or not user.is_active or user.tenant_id != item.tenant_id:
                return None
            item.last_used_at = now
            return item, user

    def revoke_api_key(self, principal: Principal, key_id: uuid.UUID) -> APIKey | None:
        with self.session_factory.begin() as session:
            item = session.scalar(
                select(APIKey)
                .where(
                    APIKey.id == key_id,
                    APIKey.tenant_id == principal.tenant_id,
                    APIKey.user_id == principal.user_id,
                )
                .with_for_update()
            )
            if item is not None and item.revoked_at is None:
                item.revoked_at = datetime.now(UTC)
            return item

    def list_users(self, tenant_id: uuid.UUID) -> list[User]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(User).where(User.tenant_id == tenant_id).order_by(User.created_at)
                )
            )

    def get_user(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> User | None:
        with self.session_factory() as session:
            return session.scalar(
                select(User).where(
                    User.id == user_id,
                    User.tenant_id == tenant_id,
                )
            )

    def create_user(
        self,
        tenant_id: uuid.UUID,
        username: str,
        display_name: str,
        password_hash: str,
        role: str,
    ) -> User:
        with self.session_factory.begin() as session:
            item = User(
                tenant_id=tenant_id,
                username=username,
                display_name=display_name,
                password_hash=password_hash,
                role=role,
                is_active=True,
            )
            session.add(item)
            session.flush()
            return item

    def update_user(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        display_name: str | None,
        role: str | None,
        is_active: bool | None,
        password_hash: str | None,
    ) -> User | None:
        with self.session_factory.begin() as session:
            user = session.scalar(
                select(User)
                .where(User.id == user_id, User.tenant_id == tenant_id)
                .with_for_update()
            )
            if user is None:
                return None
            if display_name is not None:
                user.display_name = display_name
            if role is not None:
                user.role = role
            if is_active is not None:
                user.is_active = is_active
            if password_hash is not None:
                user.password_hash = password_hash
                session.query(RefreshSession).filter(
                    RefreshSession.user_id == user.id,
                    RefreshSession.revoked_at.is_(None),
                ).update({"revoked_at": datetime.now(UTC)})
            return user

    def audit(
        self,
        *,
        tenant_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        action: str,
        outcome: str,
        correlation_id: uuid.UUID | None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        safe_metadata = {
            key: value
            for key, value in (metadata or {}).items()
            if key not in {"password", "token", "refresh_token", "api_key", "question", "prompt"}
        }
        with self.session_factory.begin() as session:
            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    action=action,
                    outcome=outcome,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    correlation_id=correlation_id,
                    metadata_json=safe_metadata,
                )
            )
