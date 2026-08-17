from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class RetrievalFilters(BaseModel):
    source_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    source_types: list[str] = Field(default_factory=list, max_length=10)
    path_prefix: str | None = None
    schema_name: str | None = None


class RetrievedChunk(BaseModel):
    contract_version: str = "1.0"
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_version_id: uuid.UUID
    document_id: uuid.UUID
    source_type: str
    title: str
    canonical_uri: str
    text: str
    score: float
    dense_rank: int | None = None
    sparse_rank: int | None = None
    metadata: dict = Field(default_factory=dict)
