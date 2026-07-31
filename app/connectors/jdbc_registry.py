from __future__ import annotations

from dataclasses import dataclass

from app.connectors.base import ConnectorError


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


POSTGRESQL_DRIVER = ManagedDriver(
    driver_id="postgresql",
    registry_version="1",
    dialect="postgresql",
    adapter="psycopg",
    adapter_version="3.2.4",
    manifest_checksum="e7be8ed45d683286c9ec548c1528e86c777b7fc6bdb6542f4d4c1c4b58e23c90",
    allowed_properties=frozenset({"sslmode"}),
)


class ManagedDriverRegistry:
    """Allowlist of reviewed, preinstalled metadata adapters.

    Registry entries are versioned. No source configuration can provide an adapter,
    module path, JAR path or arbitrary connection property.
    """

    def __init__(self, drivers: tuple[ManagedDriver, ...] = (POSTGRESQL_DRIVER,)):
        self._drivers = {(driver.driver_id, driver.registry_version): driver for driver in drivers}

    def require(self, driver_id: str, registry_version: str) -> ManagedDriver:
        driver = self._drivers.get((driver_id, registry_version))
        if driver is None:
            raise ManagedDriverError("JDBC driver is not present in the managed registry.")
        return driver
