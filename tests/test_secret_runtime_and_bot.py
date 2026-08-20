from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.api.config import Settings
from app.bot import main as bot_main
from app.operations.runtime_registry import RuntimeConfigurationError, RuntimeRegistry
from app.secrets.resolver import StoredCredentialResolver, TenantAwareStoredCredentialResolver
from app.secrets.store import SecretStoreError, load_master_key


class Store:
    def __init__(self, payload=None):
        self.payload = payload or {}

    def resolve(self, tenant_id, reference):
        assert tenant_id.int == 7
        assert reference == "credential:runtime"
        return self.payload


class Repository:
    def resolve_active_model(self, tenant_id, role):
        return SimpleNamespace(
            id=__import__("uuid").UUID(int=9),
            tenant_id=tenant_id,
            role=role,
            model_id="self-hosted",
            version="v1",
            endpoint_ref=f"endpoint:{role}",
            credential_ref="credential:runtime",
            capabilities={"base_url": "http://model.internal/v1", "dimensions": 3},
        )


def test_local_master_key_is_created_once_and_production_fails_closed(tmp_path):
    path = tmp_path / "keys" / "master.key"
    local = Settings(
        _env_file=None,
        app_env="test",
        secret_store_key_file=str(path),
        jwt_secret="x" * 40,
    )
    first = load_master_key(local)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_master_key(local) == first
    assert load_master_key(
        Settings(
            _env_file=None,
            app_env="test",
            secret_store_master_key=Fernet.generate_key().decode(),
            jwt_secret="x" * 40,
        )
    )
    path.write_text("not-a-fernet-key")
    with pytest.raises(SecretStoreError, match="invalid"):
        load_master_key(local)


def test_master_key_can_be_loaded_from_mounted_secret_file(tmp_path):
    path = tmp_path / "mounted-master-key"
    expected = Fernet.generate_key()
    path.write_bytes(expected + b"\n")
    configured = Settings(
        _env_file=None,
        app_env="test",
        secret_store_master_key_file=str(path),
        jwt_secret="x" * 40,
    )
    assert load_master_key(configured) == expected
    path.write_text("invalid")
    with pytest.raises(SecretStoreError, match="unavailable or invalid"):
        load_master_key(configured)


def test_runtime_registry_resolves_dynamic_url_and_encrypted_token():
    registry = RuntimeRegistry(Repository(), Store({"value": "opaque-token"}))
    model = registry.model(__import__("uuid").UUID(int=7), "generation")
    assert registry.endpoint(model) == ("http://model.internal/v1", "opaque-token")
    client = registry.generation_client(model)
    assert client.endpoint == "http://model.internal/v1/chat/completions"

    invalid = RuntimeRegistry(Repository(), Store({"unexpected": "value"}))
    with pytest.raises(RuntimeConfigurationError, match="invalid contract"):
        invalid.endpoint(invalid.model(__import__("uuid").UUID(int=7), "generation"))


@pytest.mark.asyncio
async def test_stored_connector_resolver_preserves_tenant_boundary():
    resolver = StoredCredentialResolver(
        Store(
            {
                "http_headers": {"Authorization": "Bearer hidden"},
                "git_environment": {"GIT_USERNAME": "owner"},
                "database_parameters": {"username": "readonly", "password": "hidden"},
            }
        ),
        __import__("uuid").UUID(int=7),
    )
    material = await resolver.resolve("credential:runtime")
    assert material.http_headers["Authorization"].endswith("hidden")
    assert material.database_parameters["username"] == "readonly"

    aware = TenantAwareStoredCredentialResolver(resolver.store)
    with pytest.raises(RuntimeError, match="tenant-bound"):
        await aware.resolve("credential:runtime")
    token = aware.bind(__import__("uuid").UUID(int=7))
    try:
        assert (await aware.resolve("credential:runtime")).http_headers
    finally:
        aware.reset(token)


class Updater:
    def __init__(self):
        self.calls = []

    async def start_polling(self):
        self.calls.append("poll")

    async def stop(self):
        self.calls.append("stop-updater")


class Application:
    def __init__(self):
        self.updater = Updater()
        self.calls = []

    async def initialize(self):
        self.calls.append("initialize")

    async def start(self):
        self.calls.append("start")

    async def stop(self):
        self.calls.append("stop")

    async def shutdown(self):
        self.calls.append("shutdown")


@pytest.mark.asyncio
async def test_bot_application_lifecycle_and_idle_token_contract():
    with pytest.raises(RuntimeError, match="required"):
        bot_main.build_application("")
    built = bot_main.build_application("123456:abcdefghijklmnopqrstuvwxyzABCDE")
    assert built.handlers

    application = Application()
    await bot_main._start(application)
    assert application.calls == ["initialize", "start"]
    assert application.updater.calls == ["poll"]
    await bot_main._stop(application)
    assert application.updater.calls[-1] == "stop-updater"
    assert application.calls[-2:] == ["stop", "shutdown"]
    await bot_main._stop(None)
