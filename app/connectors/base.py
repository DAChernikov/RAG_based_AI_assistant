from __future__ import annotations

import abc
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CredentialMaterial:
    http_headers: dict[str, str] = field(default_factory=dict)
    git_environment: dict[str, str] = field(default_factory=dict)


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
            if not isinstance(http_headers, dict) or not isinstance(git_environment, dict):
                raise ValueError("Credential fields must be objects.")
            if not all(
                isinstance(key, str) and isinstance(value, str)
                for values in (http_headers, git_environment)
                for key, value in values.items()
            ):
                raise ValueError("Credential fields must be strings.")
            return CredentialMaterial(
                http_headers=dict(http_headers),
                git_environment=dict(git_environment),
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


class ConnectorError(RuntimeError):
    pass


class TransientConnectorError(ConnectorError):
    pass


class SourceLimitError(ConnectorError):
    pass


class SSRFProtectionError(ConnectorError):
    pass
