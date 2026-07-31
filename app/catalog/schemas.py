from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.catalog.configs import SourceConfig


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    is_enabled: bool = True


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    is_enabled: bool | None = None


class KnowledgeBaseResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class KnowledgeSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    config: SourceConfig
    is_enabled: bool = True


class KnowledgeSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: SourceConfig | None = None
    is_enabled: bool | None = None


class KnowledgeSourceResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    source_type: str
    config_version: str
    config: dict[str, Any]
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class SourceVersionResponse(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    version_number: int
    status: str
    manifest: dict[str, Any] | None
    content_checksum: str | None
    created_at: datetime
    activated_at: datetime | None


class IngestionRunResponse(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    source_version_id: uuid.UUID
    status: str
    connector_version: str
    started_at: datetime
    completed_at: datetime | None
    error_code: str | None
    error_message: str | None
