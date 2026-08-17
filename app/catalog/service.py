from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.catalog.repository import CatalogRepository
from app.concurrency import api_blocking_io
from app.state.models import SourceVersionStatus


class SourceManifest(BaseModel):
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    object_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    parser_version: str = Field(min_length=1, max_length=100)
    config_version: str = Field(min_length=1, max_length=20)


class CatalogService:
    def __init__(self, repository: CatalogRepository):
        self.repository = repository

    async def call(self, method, *args, **kwargs):
        return await api_blocking_io.call(method, *args, **kwargs)

    async def record_fixture_ingestion(
        self,
        tenant_id: uuid.UUID,
        source_id: uuid.UUID,
        *,
        objects: list[dict[str, Any]],
        parser_version: str = "fixture-parser/1",
        connector_version: str = "fixture-connector/1",
        fail_at: str | None = None,
    ):
        """Test/application hook; connectors will replace this in later iterations."""
        version = await self.call(self.repository.create_version, tenant_id, source_id)
        run = await self.call(
            self.repository.create_ingestion_run,
            tenant_id,
            source_id,
            version.id,
            connector_version,
        )
        try:
            await self.call(
                self.repository.transition_version,
                tenant_id,
                source_id,
                version.id,
                SourceVersionStatus.INGESTING.value,
            )
            if fail_at == "ingesting":
                raise RuntimeError("fixture ingestion failure")
            normalized = [
                {
                    "object_key": item["object_key"],
                    "checksum": item["checksum"],
                    "byte_count": int(item.get("byte_count", 0)),
                    "chunk_count": int(item.get("chunk_count", 0)),
                    "metadata_json": item.get("metadata_json", {}),
                }
                for item in objects
            ]
            canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
            checksum = hashlib.sha256(canonical).hexdigest()
            source = await self.call(self.repository.get_source, tenant_id, source_id)
            manifest = SourceManifest(
                checksum=checksum,
                object_count=len(normalized),
                byte_count=sum(item["byte_count"] for item in normalized),
                chunk_count=sum(item["chunk_count"] for item in normalized),
                parser_version=parser_version,
                config_version=source.config_version,
            )
            await self.call(
                self.repository.write_version_content,
                tenant_id,
                source_id,
                version.id,
                objects=normalized,
                manifest=manifest.model_dump(),
                content_checksum=checksum,
            )
            for status in (
                SourceVersionStatus.STAGED.value,
                SourceVersionStatus.VALIDATING.value,
                SourceVersionStatus.READY.value,
            ):
                version = await self.call(
                    self.repository.transition_version,
                    tenant_id,
                    source_id,
                    version.id,
                    status,
                )
                if fail_at == status:
                    raise RuntimeError("fixture ingestion failure")
            await self.call(self.repository.complete_ingestion_run, run.id, "completed")
            return version
        except Exception:
            await self.call(
                self.repository.fail_ingestion,
                tenant_id,
                source_id,
                version.id,
                run.id,
                "ingestion_failed",
                "Ingestion failed. See logs using the correlation ID.",
            )
            raise
