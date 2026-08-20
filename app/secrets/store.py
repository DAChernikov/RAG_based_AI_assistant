from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.config import Settings, settings
from app.state.models import CredentialSecret


class SecretStoreError(RuntimeError):
    pass


class SecretStore(Protocol):
    def put(
        self,
        tenant_id: uuid.UUID,
        reference: str,
        kind: str,
        value: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> CredentialSecret: ...

    def resolve(self, tenant_id: uuid.UUID, reference: str) -> dict[str, Any]: ...

    def list(self, tenant_id: uuid.UUID) -> list[CredentialSecret]: ...

    def delete(self, tenant_id: uuid.UUID, reference: str) -> bool: ...


def load_master_key(config: Settings = settings) -> bytes:
    if config.secret_store_master_key:
        return config.secret_store_master_key.encode("ascii")
    if config.secret_store_master_key_file:
        try:
            key = Path(config.secret_store_master_key_file).read_bytes().strip()
            Fernet(key)
            return key
        except (OSError, ValueError, TypeError) as exc:
            raise SecretStoreError("Secret-store key file is unavailable or invalid.") from exc
    if config.app_env not in {"dev", "test"}:
        raise SecretStoreError("Encrypted secret storage requires a configured master key.")
    path = Path(config.secret_store_key_file)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        candidate = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}")
        descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(Fernet.generate_key())
                target.flush()
                os.fsync(target.fileno())
            try:
                os.link(candidate, path)
            except FileExistsError:
                # Another container atomically installed a complete key first.
                pass
        finally:
            candidate.unlink(missing_ok=True)
    key = path.read_bytes().strip()
    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise SecretStoreError("Secret-store key is invalid.") from exc
    return key


class EncryptedDatabaseSecretStore:
    """Tenant-bound encrypted values; only masked metadata leaves this boundary."""

    def __init__(self, session_factory: sessionmaker[Session], master_key: bytes):
        self.session_factory = session_factory
        self.cipher = Fernet(master_key)

    @staticmethod
    def _envelope(tenant_id: uuid.UUID, reference: str, value: dict[str, Any]) -> bytes:
        return json.dumps(
            {"tenant_id": str(tenant_id), "reference": reference, "value": value},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    def put(
        self,
        tenant_id: uuid.UUID,
        reference: str,
        kind: str,
        value: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> CredentialSecret:
        ciphertext = self.cipher.encrypt(self._envelope(tenant_id, reference, value))
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(CredentialSecret)
                .where(
                    CredentialSecret.tenant_id == tenant_id,
                    CredentialSecret.reference == reference,
                )
                .with_for_update()
            )
            if row is None:
                row = CredentialSecret(
                    tenant_id=tenant_id,
                    reference=reference,
                    kind=kind,
                    ciphertext=ciphertext,
                    metadata_json=metadata or {},
                )
                session.add(row)
            else:
                row.kind = kind
                row.ciphertext = ciphertext
                row.key_version += 1
                row.metadata_json = metadata or {}
            session.flush()
            session.refresh(row)
            return row

    def resolve(self, tenant_id: uuid.UUID, reference: str) -> dict[str, Any]:
        with self.session_factory() as session:
            row = session.scalar(
                select(CredentialSecret).where(
                    CredentialSecret.tenant_id == tenant_id,
                    CredentialSecret.reference == reference,
                )
            )
            if row is None:
                raise SecretStoreError("Credential reference was not found.")
            ciphertext = row.ciphertext
        try:
            envelope = json.loads(self.cipher.decrypt(ciphertext))
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise SecretStoreError("Credential reference cannot be decrypted.") from exc
        if envelope.get("tenant_id") != str(tenant_id) or envelope.get("reference") != reference:
            raise SecretStoreError("Credential reference tenant binding is invalid.")
        value = envelope.get("value")
        if not isinstance(value, dict):
            raise SecretStoreError("Credential reference payload is invalid.")
        return value

    def list(self, tenant_id: uuid.UUID) -> list[CredentialSecret]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(CredentialSecret)
                    .where(CredentialSecret.tenant_id == tenant_id)
                    .order_by(CredentialSecret.reference)
                )
            )

    def delete(self, tenant_id: uuid.UUID, reference: str) -> bool:
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(CredentialSecret)
                .where(
                    CredentialSecret.tenant_id == tenant_id,
                    CredentialSecret.reference == reference,
                )
                .with_for_update()
            )
            if row is None:
                return False
            session.delete(row)
            return True
