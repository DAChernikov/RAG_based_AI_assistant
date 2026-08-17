from __future__ import annotations

import hashlib
import json
import uuid

from app.catalog.configs import (
    GitSourceConfig,
    JDBCSourceConfig,
    WebsiteSourceConfig,
    parse_source_config,
)
from app.catalog.repository import CatalogRepository
from app.concurrency import api_blocking_io
from app.connectors.git import GitConnector
from app.connectors.jdbc import JDBCMetadataConnector
from app.connectors.website import WebsiteConnector
from app.state.models import SourceVersionStatus


class IngestionCancelled(RuntimeError):
    pass


class UnsupportedConnectorError(RuntimeError):
    pass


class IngestionPipeline:
    def __init__(
        self,
        repository: CatalogRepository,
        website_connector: WebsiteConnector,
        git_connector: GitConnector,
        jdbc_connector: JDBCMetadataConnector | None = None,
    ):
        self.repository = repository
        self.website_connector = website_connector
        self.git_connector = git_connector
        self.jdbc_connector = jdbc_connector

    async def execute(self, contract, emit, *, lease_token: uuid.UUID | None = None) -> None:
        run = await self._call(
            self.repository.get_ingestion_run, contract.tenant_id, contract.run_id
        )
        source = await self._call(
            self.repository.get_source, contract.tenant_id, contract.source_id
        )
        version = await self._call(
            self.repository.get_version,
            contract.tenant_id,
            contract.source_id,
            contract.source_version_id,
        )
        if run is None or source is None or version is None:
            raise UnsupportedConnectorError("Ingestion state was not found.")
        if await self._cancelled(contract.run_id):
            raise IngestionCancelled()
        if version.status == SourceVersionStatus.DISCOVERED.value:
            await self._call(
                self.repository.transition_version,
                contract.tenant_id,
                contract.source_id,
                contract.source_version_id,
                SourceVersionStatus.INGESTING.value,
                run_id=contract.run_id if lease_token else None,
                lease_token=lease_token,
            )
        if version.status in {
            SourceVersionStatus.DISCOVERED.value,
            SourceVersionStatus.INGESTING.value,
        }:
            await emit("progress", "discover")
            previous, previous_revision, previous_version_id = await self._call(
                self.repository.load_previous_objects, contract.tenant_id, contract.source_id
            )
            config = parse_source_config(version.config_snapshot)
            if isinstance(config, WebsiteSourceConfig):
                discovery = await self.website_connector.discover(config, previous)
            elif isinstance(config, GitSourceConfig):
                discovery = await self.git_connector.discover(
                    config, previous, previous_revision=previous_revision
                )
            elif isinstance(config, JDBCSourceConfig) and self.jdbc_connector is not None:
                discovery = await self.jdbc_connector.discover(config, previous)
            else:
                raise UnsupportedConnectorError("Source connector is not configured.")
            if await self._cancelled(contract.run_id):
                raise IngestionCancelled()
            await emit("progress", "incremental_diff")
            current = {
                key: (item.checksum, item.metadata)
                for key, item in previous.items()
                if key not in discovery.deleted
                and not any(old == key for old, _new in discovery.renamed)
            }
            current.update(
                (document.object_key, (document.checksum, document.metadata))
                for document in discovery.documents
            )
            canonical = json.dumps(
                [(key, value[0]) for key, value in sorted(current.items())],
                separators=(",", ":"),
            ).encode()
            content_checksum = hashlib.sha256(canonical).hexdigest()
            manifest = {
                "checksum": content_checksum,
                "object_count": len(current),
                "byte_count": sum(document.byte_count for document in discovery.documents)
                + sum(
                    int(metadata.get("_byte_count", 0))
                    for key, (_checksum, metadata) in current.items()
                    if key not in {item.object_key for item in discovery.documents}
                ),
                "chunk_count": sum(len(document.chunks) for document in discovery.documents)
                + sum(
                    int(metadata.get("_chunk_count", 0))
                    for key, (_checksum, metadata) in current.items()
                    if key not in {item.object_key for item in discovery.documents}
                ),
                "parser_version": "structured-parser/1.0",
                "config_version": version.config_snapshot["config_version"],
                "connector_version": run.connector_version,
                "source_revision": discovery.source_revision,
                "discovery_complete": discovery.complete,
                "incomplete_reasons": list(discovery.incomplete_reasons),
                "incremental": {
                    "added": len(discovery.added),
                    "modified": len(discovery.modified),
                    "unchanged": len(discovery.unchanged),
                    "deleted": len(discovery.deleted),
                    "renamed": len(discovery.renamed),
                },
            }
            await emit("progress", "parse_chunk")
            await self._call(
                self.repository.persist_discovery,
                contract.tenant_id,
                contract.source_id,
                contract.source_version_id,
                discovery,
                previous_version_id,
                manifest,
                content_checksum,
                run_id=contract.run_id if lease_token else None,
                lease_token=lease_token,
            )
            await self._call(
                self.repository.transition_version,
                contract.tenant_id,
                contract.source_id,
                contract.source_version_id,
                SourceVersionStatus.STAGED.value,
                run_id=contract.run_id if lease_token else None,
                lease_token=lease_token,
            )
            version.status = SourceVersionStatus.STAGED.value
        if await self._cancelled(contract.run_id):
            raise IngestionCancelled()
        if version.status == SourceVersionStatus.STAGED.value:
            await emit("progress", "validate")
            await self._call(
                self.repository.transition_version,
                contract.tenant_id,
                contract.source_id,
                contract.source_version_id,
                SourceVersionStatus.VALIDATING.value,
                run_id=contract.run_id if lease_token else None,
                lease_token=lease_token,
            )
            version.status = SourceVersionStatus.VALIDATING.value
        if version.status == SourceVersionStatus.VALIDATING.value:
            await self._call(
                self.repository.transition_version,
                contract.tenant_id,
                contract.source_id,
                contract.source_version_id,
                SourceVersionStatus.READY.value,
                run_id=contract.run_id if lease_token else None,
                lease_token=lease_token,
            )
        if await self._cancelled(contract.run_id):
            raise IngestionCancelled()
        if run.auto_activate:
            await emit("progress", "activate")
            await self._call(
                self.repository.activate_version,
                contract.tenant_id,
                contract.source_id,
                contract.source_version_id,
                run_id=contract.run_id if lease_token else None,
                lease_token=lease_token,
            )

    async def _cancelled(self, run_id: uuid.UUID) -> bool:
        return await self._call(self.repository.ingestion_cancel_requested, run_id)

    @staticmethod
    async def _call(method, *args, **kwargs):
        return await api_blocking_io.call(method, *args, **kwargs)
