from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.api.config import settings
from app.api.services.llm_service import OpenAICompatibleLLMService
from app.embeddings.client import EmbeddingClient
from app.operations.repository import OperationsRepository
from app.retrieval.reranker import RerankerClient
from app.secrets.store import SecretStore, SecretStoreError


class RuntimeConfigurationError(RuntimeError):
    """Raised when a required active model or prompt cannot be resolved safely."""


@dataclass(frozen=True)
class ResolvedModel:
    definition_id: uuid.UUID
    role: str
    model_id: str
    version: str
    endpoint_ref: str
    credential_ref: str | None
    capabilities: dict
    tenant_id: uuid.UUID = uuid.UUID(int=0)


@dataclass(frozen=True)
class ResolvedPrompt:
    template_id: uuid.UUID
    name: str
    version: str
    template: str


class RuntimeRegistry:
    """Resolves tenant overrides with deterministic global fallback.

    Endpoint and credential references are allowlisted deployment references. Database rows
    never contain bearer tokens or arbitrary runtime URLs.
    """

    def __init__(self, repository: OperationsRepository, secret_store: SecretStore | None = None):
        self.repository = repository
        self.secret_store = secret_store

    def model(self, tenant_id: uuid.UUID, role: str) -> ResolvedModel:
        row = self.repository.resolve_active_model(tenant_id, role)
        if row is None:
            raise RuntimeConfigurationError(f"No active {role} model is configured.")
        return ResolvedModel(
            definition_id=row.id,
            role=row.role,
            model_id=row.model_id,
            version=row.version,
            endpoint_ref=row.endpoint_ref,
            credential_ref=row.credential_ref,
            capabilities=row.capabilities or {},
            tenant_id=tenant_id,
        )

    def prompt(self, tenant_id: uuid.UUID, name: str) -> ResolvedPrompt:
        row = self.repository.resolve_active_prompt(tenant_id, name)
        if row is None:
            raise RuntimeConfigurationError(f"No active {name} prompt is configured.")
        return ResolvedPrompt(row.id, row.name, row.version, row.template)

    def endpoint(self, model: ResolvedModel) -> tuple[str, str | None]:
        references = {
            "endpoint:generation": (settings.model_api_base_url, settings.model_api_token),
            "endpoint:embedding": (
                settings.embedding_api_base_url,
                settings.embedding_api_token,
            ),
            "endpoint:reranker": (
                settings.reranker_api_base_url,
                settings.reranker_api_token,
            ),
        }
        configured_url = model.capabilities.get("base_url")
        value: tuple[str | None, str | None] | None
        if configured_url is not None:
            if not isinstance(configured_url, str):
                raise RuntimeConfigurationError("Model endpoint URL is invalid.")
            parsed = urlsplit(configured_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
                raise RuntimeConfigurationError("Model endpoint URL is invalid.")
            value = (configured_url.rstrip("/"), None)
        else:
            value = references.get(model.endpoint_ref)
        if value is None or not value[0]:
            raise RuntimeConfigurationError(
                f"Endpoint reference for {model.role} is not configured."
            )
        token = value[1]
        if model.credential_ref:
            if self.secret_store is None:
                expected_credential = f"credential:{model.role}"
                if model.credential_ref != expected_credential:
                    raise RuntimeConfigurationError(
                        f"Credential reference for {model.role} is not allowlisted."
                    )
            else:
                try:
                    payload = self.secret_store.resolve(model.tenant_id, model.credential_ref)
                except SecretStoreError as exc:
                    raise RuntimeConfigurationError(
                        f"Credential reference for {model.role} is unavailable."
                    ) from exc
                resolved = payload.get("value")
                if not isinstance(resolved, str) or not resolved:
                    raise RuntimeConfigurationError(
                        f"Credential reference for {model.role} has an invalid contract."
                    )
                token = resolved
        return value[0], token

    def generation_client(self, model: ResolvedModel) -> OpenAICompatibleLLMService:
        endpoint, token = self.endpoint(model)
        return OpenAICompatibleLLMService(
            api_base_url=endpoint,
            model=model.model_id,
            api_token=token,
        )

    def embedding_client(self, model: ResolvedModel) -> EmbeddingClient:
        endpoint, token = self.endpoint(model)
        return EmbeddingClient(
            endpoint,
            model.model_id,
            token=token,
            timeout=settings.embedding_request_timeout,
            expected_version=model.version,
            expected_dimensions=(
                int(model.capabilities["dimensions"])
                if model.capabilities.get("dimensions") is not None
                else None
            ),
        )

    def reranker_client(self, model: ResolvedModel) -> RerankerClient:
        endpoint, token = self.endpoint(model)
        return RerankerClient(endpoint, token, settings.reranker_request_timeout)


class RegistryEmbeddingGateway:
    """Creates short-lived HTTP clients from the model pinned to a run or active index."""

    def __init__(self, registry: RuntimeRegistry):
        self.registry = registry

    def _stored_model(self, definition_id: uuid.UUID) -> ResolvedModel:
        row = self.registry.repository.get_model(definition_id)
        if row is None:
            raise RuntimeConfigurationError("Pinned embedding model definition was removed.")
        return ResolvedModel(
            row.id,
            row.role,
            row.model_id,
            row.version,
            row.endpoint_ref,
            row.credential_ref,
            row.capabilities or {},
            row.tenant_id or uuid.UUID(int=0),
        )

    async def embed_for_model(
        self, definition_id: uuid.UUID, texts: list[str]
    ) -> list[list[float]]:
        client = self.registry.embedding_client(self._stored_model(definition_id))
        try:
            return await client.embed(texts)
        finally:
            await client.close()

    async def embed_for_index(
        self,
        tenant_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        texts: list[str],
    ) -> list[list[float]]:
        row = self.registry.repository.resolve_active_index_model(tenant_id, knowledge_base_id)
        if row is None:
            raise RuntimeConfigurationError("Active index has no pinned embedding model metadata.")
        return await self.embed_for_model(row.id, texts)

    async def readiness(self, tenant_id: uuid.UUID | None = None) -> dict:
        model = self.registry.model(tenant_id or uuid.UUID(int=0), "embedding")
        client = self.registry.embedding_client(model)
        try:
            return await client.readiness()
        finally:
            await client.close()

    async def close(self) -> None:
        return None


def render_prompt(prompt: ResolvedPrompt, *, question: str, context: str, mode: str) -> str:
    try:
        return prompt.template.format(question=question, context=context, mode=mode)
    except (KeyError, ValueError) as exc:
        raise RuntimeConfigurationError(
            f"Active {prompt.name} prompt has an invalid template contract."
        ) from exc
