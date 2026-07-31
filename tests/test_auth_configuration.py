import pytest

from app.api.config import settings
from app.api.main import validate_auth_configuration


def test_auth_disabled_fails_closed_in_production(monkeypatch):
    monkeypatch.setattr(settings, "auth_disabled", True)
    monkeypatch.setattr(settings, "app_env", "production")

    with pytest.raises(RuntimeError, match="only in dev/test"):
        validate_auth_configuration()


def test_auth_disabled_is_explicitly_allowed_in_test(monkeypatch):
    monkeypatch.setattr(settings, "auth_disabled", True)
    monkeypatch.setattr(settings, "app_env", "test")

    validate_auth_configuration()


def test_auth_requires_signing_secret(monkeypatch):
    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "jwt_secret", "")

    with pytest.raises(RuntimeError, match="at least 32"):
        validate_auth_configuration()
