from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

INGESTION_CONTRACT_VERSION = "1.0"


class IngestionJobContract(BaseModel):
    contract_version: Literal["1.0"] = INGESTION_CONTRACT_VERSION
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    source_id: uuid.UUID
    source_version_id: uuid.UUID
    correlation_id: uuid.UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IngestionEvent(BaseModel):
    event_version: Literal["1.0"] = "1.0"
    run_id: uuid.UUID
    event_type: Literal[
        "queued", "started", "progress", "retrying", "completed", "failed", "cancelled"
    ]
    stage: str | None = None
    message: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
