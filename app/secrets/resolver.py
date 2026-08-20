from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

from app.concurrency import connector_blocking_io
from app.connectors.base import CredentialMaterial, CredentialResolver, validate_credential_material
from app.secrets.store import SecretStore


class StoredCredentialResolver(CredentialResolver):
    def __init__(self, store: SecretStore, tenant_id: uuid.UUID):
        self.store = store
        self.tenant_id = tenant_id

    async def resolve(self, reference: str) -> CredentialMaterial:
        payload = await connector_blocking_io.call(self.store.resolve, self.tenant_id, reference)
        return validate_credential_material(
            CredentialMaterial(
                http_headers=dict(payload.get("http_headers", {})),
                git_environment=dict(payload.get("git_environment", {})),
                database_parameters=dict(payload.get("database_parameters", {})),
            )
        )


class TenantAwareStoredCredentialResolver(CredentialResolver):
    """Resolve credentials only for the tenant bound by the durable ingestion job."""

    def __init__(self, store: SecretStore):
        self.store = store
        self._tenant: ContextVar[uuid.UUID | None] = ContextVar("credential_tenant", default=None)

    def bind(self, tenant_id: uuid.UUID) -> Token:
        return self._tenant.set(tenant_id)

    def reset(self, token: Token) -> None:
        self._tenant.reset(token)

    async def resolve(self, reference: str) -> CredentialMaterial:
        tenant_id = self._tenant.get()
        if tenant_id is None:
            raise RuntimeError("Credential resolution requires a tenant-bound job.")
        return await StoredCredentialResolver(self.store, tenant_id).resolve(reference)
