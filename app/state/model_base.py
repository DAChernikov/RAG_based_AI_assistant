from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class SourceType(str, enum.Enum):
    WEBSITE = "website"
    GIT = "git"
    JDBC = "jdbc"


class SourceVersionStatus(str, enum.Enum):
    DISCOVERED = "discovered"
    INGESTING = "ingesting"
    STAGED = "staged"
    VALIDATING = "validating"
    READY = "ready"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class IngestionRunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IndexVersionStatus(str, enum.Enum):
    CREATED = "created"
    INDEXING = "indexing"
    VALIDATING = "validating"
    READY = "ready"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class IndexingRunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScheduleAttemptStatus(str, enum.Enum):
    PENDING = "pending"
    ENQUEUED = "enqueued"
    FAILED = "failed"


class JDBCDriverRegistryEntry(Base):
    __tablename__ = "jdbc_driver_registry"
    __table_args__ = (
        UniqueConstraint("driver_id", "registry_version", name="uq_jdbc_driver_registry_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    driver_id: Mapped[str] = mapped_column(String(100), nullable=False)
    registry_version: Mapped[str] = mapped_column(String(20), nullable=False)
    dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    adapter: Mapped[str] = mapped_column(String(100), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(50), nullable=False)
    manifest_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_properties: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
