from __future__ import annotations

import abc
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

_ALLOWED_HTTP_HEADERS = frozenset({"authorization", "cookie", "x-api-key"})
_ALLOWED_GIT_ENVIRONMENT = frozenset(
    {"GIT_USERNAME", "GIT_PASSWORD", "SSH_PRIVATE_KEY", "SSH_KNOWN_HOSTS", "SSH_AUTH_SOCK"}
)
_ALLOWED_DATABASE_PARAMETERS = frozenset(
    {"username", "password", "sslrootcert", "sslcert", "sslkey"}
)


class ConnectorError(RuntimeError):
    pass


class CredentialIsolationError(ConnectorError):
    pass


class TransientConnectorError(ConnectorError):
    pass


class SourceLimitError(ConnectorError):
    pass


class SSRFProtectionError(ConnectorError):
    pass


@dataclass(frozen=True)
class CredentialMaterial:
    http_headers: dict[str, str] = field(default_factory=dict)
    git_environment: dict[str, str] = field(default_factory=dict)
    database_parameters: dict[str, str] = field(default_factory=dict)


def validate_credential_material(material: CredentialMaterial) -> CredentialMaterial:
    invalid_headers = set(map(str.casefold, material.http_headers)) - _ALLOWED_HTTP_HEADERS
    invalid_git = set(material.git_environment) - _ALLOWED_GIT_ENVIRONMENT
    invalid_database = set(material.database_parameters) - _ALLOWED_DATABASE_PARAMETERS
    if invalid_headers or invalid_git or invalid_database:
        raise CredentialIsolationError("Credential material contains unsupported fields.")
    return material


class CredentialResolver(abc.ABC):
    @abc.abstractmethod
    async def resolve(self, reference: str) -> CredentialMaterial:
        """Resolve an opaque reference without persisting or logging returned secrets."""


class RejectingCredentialResolver(CredentialResolver):
    async def resolve(self, reference: str) -> CredentialMaterial:
        raise RuntimeError("Credential resolution is not configured.")


class EnvironmentCredentialResolver(CredentialResolver):
    """Resolve a reference through a non-reversible environment-variable name."""

    async def resolve(self, reference: str) -> CredentialMaterial:
        suffix = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:16].upper()
        raw = os.getenv(f"RAG_CREDENTIAL_{suffix}")
        if not raw:
            raise RuntimeError("Referenced credential is not configured.")
        try:
            payload = json.loads(raw)
            http_headers = payload.get("http_headers", {})
            git_environment = payload.get("git_environment", {})
            database_parameters = payload.get("database_parameters", {})
            if not all(
                isinstance(item, dict)
                for item in (http_headers, git_environment, database_parameters)
            ):
                raise ValueError("Credential fields must be objects.")
            if not all(
                isinstance(key, str) and isinstance(value, str)
                for values in (http_headers, git_environment, database_parameters)
                for key, value in values.items()
            ):
                raise ValueError("Credential fields must be strings.")
            return validate_credential_material(
                CredentialMaterial(
                    http_headers=dict(http_headers),
                    git_environment=dict(git_environment),
                    database_parameters=dict(database_parameters),
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Referenced credential is invalid.") from exc


@dataclass(frozen=True)
class PreviousObject:
    object_key: str
    checksum: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedChunk:
    text: str
    checksum: str
    chunk_index: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorDocument:
    object_key: str
    canonical_uri: str
    title: str
    text: str
    checksum: str
    byte_count: int
    metadata: dict[str, Any]
    chunks: tuple[ParsedChunk, ...]


@dataclass(frozen=True)
class DiscoveryResult:
    documents: tuple[ConnectorDocument, ...]
    added: tuple[str, ...]
    modified: tuple[str, ...]
    unchanged: tuple[str, ...]
    deleted: tuple[str, ...]
    renamed: tuple[tuple[str, str], ...] = ()
    source_revision: str | None = None
    complete: bool = True
    incomplete_reasons: tuple[str, ...] = ()
