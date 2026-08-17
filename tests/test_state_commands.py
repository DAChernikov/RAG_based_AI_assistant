from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.state import bootstrap_admin, seed, validate_config
from app.state.models import Base, Tenant, User


@pytest.fixture
def command_database(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (bootstrap_admin, seed):
        monkeypatch.setattr(module, "create_database_engine", lambda: engine)
        monkeypatch.setattr(module, "create_session_factory", lambda _engine: factory)
    return factory


def test_bootstrap_admin_is_secure_and_refuses_existing_identity(command_database):
    tenant_id, user_id = bootstrap_admin.bootstrap_admin(
        "tenant", "Tenant", "admin", "Administrator", "long-password-value"
    )
    with command_database() as session:
        user = session.get(User, uuid.UUID(user_id))
        assert user.role == "admin"
        assert user.password_hash != "long-password-value"
    with pytest.raises(RuntimeError, match="already exists"):
        bootstrap_admin.bootstrap_admin(
            "tenant", "Tenant", "admin", "Administrator", "other-password"
        )
    assert tenant_id


def test_bootstrap_rejects_existing_non_admin(command_database):
    with command_database.begin() as session:
        tenant = Tenant(slug="member", name="Member")
        session.add(tenant)
        session.flush()
        session.add(User(tenant_id=tenant.id, username="same", display_name="Same", role="user"))
    with pytest.raises(RuntimeError, match="not an administrator"):
        bootstrap_admin.bootstrap_admin("member", "Member", "same", "Same", "long-password-value")


def test_development_identity_seed_is_idempotent(command_database, monkeypatch):
    monkeypatch.setattr(seed.settings, "compatibility_tenant_slug", "development")
    monkeypatch.setattr(seed.settings, "compatibility_user_external_id", "developer")
    first = seed.seed_development_identity()
    second = seed.seed_development_identity()
    assert first == second


def test_configuration_preflight_reports_only_fields(monkeypatch, capsys):
    assert validate_config.main() == 0
    assert "valid" in capsys.readouterr().out

    class Invalid:
        def __init__(self):
            raise ValidationError.from_exception_data(
                "Settings",
                [
                    {
                        "type": "value_error",
                        "loc": ("jwt_secret",),
                        "input": "hidden",
                        "ctx": {"error": ValueError("secret value must not print")},
                    }
                ],
            )

    monkeypatch.setattr(validate_config, "Settings", Invalid)
    assert validate_config.main() == 1
    output = capsys.readouterr().out
    assert "jwt_secret" in output
    assert "hidden" not in output
