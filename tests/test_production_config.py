from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.api.config import Settings


def production(**overrides):
    values = {
        "app_env": "production",
        "auth_disabled": False,
        "auth_cookie_secure": True,
        "auth_cookie_samesite": "lax",
        "jwt_secret": "s" * 64,
        "cors_allowed_origins": "https://assistant.internal.test",
        "trusted_proxy_cidrs": "10.0.0.0/8",
        "database_url": "postgresql+psycopg://rag@db.internal.test/rag?sslmode=verify-full",
        "redis_url": "rediss://redis.internal.test:6380/0",
        "model_api_base_url": "https://model.internal.test/v1",
        "embedding_api_base_url": "https://embedding.internal.test/v1",
        "postgres_sslmode": "verify-full",
        "secret_store_master_key": Fernet.generate_key().decode(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_configuration_accepts_explicit_secure_endpoints():
    assert production().app_env == "production"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "postgresql+psycopg://rag@db.example.invalid/rag"),
        ("redis_url", "redis://localhost:6379/0"),
        ("model_api_base_url", "http://127.0.0.1:11434/v1"),
        ("embedding_api_base_url", "https://change-me.invalid/v1"),
        ("cors_allowed_origins", "*"),
        ("trusted_proxy_cidrs", "0.0.0.0/0"),
        ("postgres_sslmode", "disable"),
    ],
)
def test_production_configuration_rejects_placeholders_loopback_and_insecure_values(field, value):
    with pytest.raises(ValidationError):
        production(**{field: value})


def test_production_cookie_and_secret_contracts_fail_closed():
    with pytest.raises(ValidationError):
        production(auth_cookie_secure=False, auth_cookie_samesite="none")
    with pytest.raises(ValidationError):
        production(jwt_secret="CHANGE-ME")


def test_production_setup_token_file_is_optional_but_must_be_strong_when_populated(tmp_path):
    disabled = tmp_path / "disabled"
    disabled.write_text("")
    assert production(setup_bootstrap_token_file=str(disabled))
    disabled.write_text("weak")
    with pytest.raises(ValidationError, match="SETUP_BOOTSTRAP_TOKEN_FILE"):
        production(setup_bootstrap_token_file=str(disabled))
    disabled.unlink()
    with pytest.raises(ValidationError, match="unavailable"):
        production(setup_bootstrap_token_file=str(disabled))


def test_production_allows_only_explicit_internal_plain_http_model_host():
    configured = production(
        embedding_api_base_url="http://rag-assistant-embedding:8001/v1",
        model_http_allowed_hosts="rag-assistant-embedding",
    )
    assert configured.embedding_api_base_url.startswith("http://")
    with pytest.raises(ValidationError):
        production(embedding_api_base_url="http://unlisted-service:8001/v1")
    with pytest.raises(ValidationError):
        production(database_url="postgresql+psycopg://rag@db.internal.test/rag")
