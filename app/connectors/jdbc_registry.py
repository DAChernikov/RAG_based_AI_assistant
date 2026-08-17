from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import psycopg
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.base import ConnectorError
from app.state.models import JDBCDriverRegistryEntry


class ManagedDriverError(ConnectorError):
    pass


@dataclass(frozen=True)
class ManagedDriver:
    driver_id: str
    registry_version: str
    dialect: str
    adapter: str
    adapter_version: str
    manifest_checksum: str
    allowed_properties: frozenset[str]


def manifest_checksum(driver: ManagedDriver) -> str:
    payload = {
        "adapter": driver.adapter,
        "adapter_version": driver.adapter_version,
        "allowed_properties": sorted(driver.allowed_properties),
        "dialect": driver.dialect,
        "driver_id": driver.driver_id,
        "registry_version": driver.registry_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ManagedDriverRegistry:
    """Database-backed allowlist of reviewed, preinstalled metadata adapters."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def require(self, driver_id: str, registry_version: str) -> ManagedDriver:
        with self.session_factory() as session:
            entry = session.scalar(
                select(JDBCDriverRegistryEntry).where(
                    JDBCDriverRegistryEntry.driver_id == driver_id,
                    JDBCDriverRegistryEntry.registry_version == registry_version,
                )
            )
        if entry is None or not entry.is_enabled:
            raise ManagedDriverError("JDBC driver is not enabled in the managed registry.")
        driver = ManagedDriver(
            driver_id=entry.driver_id,
            registry_version=entry.registry_version,
            dialect=entry.dialect,
            adapter=entry.adapter,
            adapter_version=entry.adapter_version,
            manifest_checksum=entry.manifest_checksum,
            allowed_properties=frozenset(item.casefold() for item in entry.allowed_properties),
        )
        if driver.adapter != "psycopg" or driver.adapter_version != psycopg.__version__:
            raise ManagedDriverError("JDBC adapter version does not match the reviewed runtime.")
        if manifest_checksum(driver) != driver.manifest_checksum:
            raise ManagedDriverError("JDBC driver manifest checksum validation failed.")
        return driver
