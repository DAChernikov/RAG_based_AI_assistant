from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from app.api.dependencies import get_runtime_state, require_admin
from app.api.routes.catalog import _audit
from app.auth.security import Principal
from app.secrets.store import SecretStoreError

router = APIRouter(prefix="/v1/admin/credentials", tags=["credentials"])

CredentialKind = Literal[
    "model_token",
    "telegram_token",
    "telegram_api_key",
    "website_bearer",
    "git_basic",
    "git_ssh",
    "jdbc_password",
]


class CredentialInput(BaseModel):
    reference: str = Field(pattern=r"^credential:[A-Za-z0-9_.:/-]+$", max_length=255)
    kind: CredentialKind
    secret: str | None = Field(default=None, min_length=1, max_length=20_000)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=4_096)
    private_key: str | None = Field(default=None, min_length=1, max_length=20_000)
    known_hosts: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def validate_kind_contract(self):
        if (
            self.kind
            in {
                "model_token",
                "telegram_token",
                "telegram_api_key",
                "website_bearer",
            }
            and not self.secret
        ):
            raise ValueError("A secret value is required for this credential kind.")
        if self.kind in {"git_basic", "jdbc_password"} and not (self.username and self.password):
            raise ValueError("Username and password are required for this credential kind.")
        if self.kind == "git_ssh" and not self.private_key:
            raise ValueError("A private key is required for git_ssh.")
        return self

    def encrypted_value(self) -> dict:
        if self.kind in {"model_token", "telegram_token", "telegram_api_key"}:
            return {"value": self.secret}
        if self.kind == "website_bearer":
            return {"http_headers": {"Authorization": f"Bearer {self.secret}"}}
        if self.kind == "git_basic":
            return {
                "git_environment": {"GIT_USERNAME": self.username, "GIT_PASSWORD": self.password}
            }
        if self.kind == "git_ssh":
            return {
                "git_environment": {
                    "SSH_PRIVATE_KEY": self.private_key,
                    "SSH_KNOWN_HOSTS": self.known_hosts,
                }
            }
        return {"database_parameters": {"username": self.username, "password": self.password}}


def _metadata(row) -> dict:
    return {
        "id": row.id,
        "reference": row.reference,
        "kind": row.kind,
        "masked_value": row.masked_value,
        "key_version": row.key_version,
        "metadata": row.metadata_json,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("")
async def list_credentials(
    principal: Principal = Depends(require_admin), runtime=Depends(get_runtime_state)
):
    rows = await runtime["catalog_service"].call(runtime["secret_store"].list, principal.tenant_id)
    return [_metadata(row) for row in rows]


@router.put("/{reference:path}")
async def put_credential(
    reference: str,
    payload: CredentialInput,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    if reference != payload.reference:
        raise HTTPException(status_code=422, detail="Path and payload references differ.")
    try:
        row = await runtime["catalog_service"].call(
            runtime["secret_store"].put,
            principal.tenant_id,
            reference,
            payload.kind,
            payload.encrypted_value(),
            {"fields": sorted(payload.encrypted_value())},
        )
    except SecretStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit(
        request,
        runtime,
        principal,
        "credential.rotate" if row.key_version > 1 else "credential.create",
        "credential_reference",
        row.id,
        {"reference": reference, "kind": payload.kind, "key_version": row.key_version},
    )
    return _metadata(row)


@router.delete("/{reference:path}", status_code=204)
async def delete_credential(
    reference: str,
    request: Request,
    principal: Principal = Depends(require_admin),
    runtime=Depends(get_runtime_state),
):
    changed = await runtime["catalog_service"].call(
        runtime["secret_store"].delete, principal.tenant_id, reference
    )
    if not changed:
        raise HTTPException(status_code=404, detail="Credential reference was not found.")
    await _audit(
        request,
        runtime,
        principal,
        "credential.delete",
        "tenant",
        principal.tenant_id,
        {"reference": reference},
    )
