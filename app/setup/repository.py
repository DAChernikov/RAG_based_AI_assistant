from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.state.models import SystemSetup, Tenant, User

_PROCESS_SETUP_LOCK = threading.Lock()


class SetupClosedError(RuntimeError):
    pass


class SetupRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def status(self) -> dict:
        with self.session_factory() as session:
            state = session.get(SystemSetup, 1)
            admin_count = session.scalar(select(func.count(User.id)).where(User.role == "admin"))
            legacy_initialized = bool(
                admin_count and (state is None or state.bootstrap_completed_at is None)
            )
            return {
                "required": bool(
                    not admin_count and (state is None or not state.bootstrap_completed_at)
                ),
                "current_step": (
                    "complete"
                    if legacy_initialized
                    else state.current_step if state else "administrator"
                ),
                "onboarding_complete": bool(
                    legacy_initialized or (state and state.onboarding_completed_at)
                ),
                "config_version": state.config_version if state else 1,
            }

    def bootstrap(
        self,
        *,
        tenant_slug: str,
        tenant_name: str,
        username: str,
        display_name: str,
        password_hash: str,
    ) -> tuple[Tenant, User]:
        # PostgreSQL row locking protects multiple API replicas; the process lock also makes
        # SQLite contract tests deterministic without weakening the database invariant.
        with _PROCESS_SETUP_LOCK:
            try:
                with self.session_factory.begin() as session:
                    state = session.scalar(
                        select(SystemSetup).where(SystemSetup.id == 1).with_for_update()
                    )
                    if state is None:
                        state = SystemSetup(id=1)
                        session.add(state)
                        session.flush()
                    has_admin = session.scalar(
                        select(func.count(User.id)).where(User.role == "admin")
                    )
                    if state.bootstrap_completed_at or has_admin:
                        raise SetupClosedError("First-run setup is permanently closed.")
                    tenant = Tenant(slug=tenant_slug, name=tenant_name)
                    session.add(tenant)
                    session.flush()
                    administrator = User(
                        tenant_id=tenant.id,
                        username=username,
                        display_name=display_name,
                        password_hash=password_hash,
                        role="admin",
                        is_active=True,
                    )
                    session.add(administrator)
                    session.flush()
                    state.tenant_id = tenant.id
                    state.administrator_id = administrator.id
                    state.bootstrap_completed_at = datetime.now(UTC)
                    state.current_step = "models"
                    state.config_version += 1
                    return tenant, administrator
            except IntegrityError as exc:
                raise SetupClosedError("First-run setup was completed concurrently.") from exc

    def set_progress(self, tenant_id: uuid.UUID, step: str, complete: bool = False) -> dict:
        with self.session_factory.begin() as session:
            state = session.scalar(
                select(SystemSetup)
                .where(SystemSetup.id == 1, SystemSetup.tenant_id == tenant_id)
                .with_for_update()
            )
            if state is None or state.bootstrap_completed_at is None:
                raise SetupClosedError("Setup state is unavailable.")
            state.current_step = "complete" if complete else step
            state.config_version += 1
            if complete and state.onboarding_completed_at is None:
                state.onboarding_completed_at = datetime.now(UTC)
            return {
                "current_step": state.current_step,
                "onboarding_complete": bool(state.onboarding_completed_at),
                "config_version": state.config_version,
            }
