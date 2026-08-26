from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

JOB_CONTRACT_VERSION: Literal["1.0"] = "1.0"
EVENT_CONTRACT_VERSION: Literal["1.0"] = "1.0"


class UnsupportedContractVersion(ValueError):
    pass


class InferenceJobContract(BaseModel):
    contract_version: Literal["1.0"] = JOB_CONTRACT_VERSION
    job_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    knowledge_base_id: uuid.UUID | None = None
    question: str = Field(min_length=1)
    requested_mode: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)
    max_new_tokens: int | None = Field(default=None, ge=1, le=4096)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)


class QueuedPayload(BaseModel):
    status: Literal["queued"] = "queued"


class StartedPayload(BaseModel):
    attempt: int
    worker_id: str


class MetaPayload(BaseModel):
    mode: str
    confidence: dict | None = None
    retrieved: list[dict] = Field(default_factory=list)


class TokenPayload(BaseModel):
    text: str


class CompletedPayload(BaseModel):
    answer: str
    mode: str


class FailedPayload(BaseModel):
    error_code: str
    message: str


class RetryingPayload(BaseModel):
    attempt: int
    max_attempts: int
    error_code: str


class _EventBase(BaseModel):
    event_contract_version: Literal["1.0"] = EVENT_CONTRACT_VERSION
    event_id: str
    sequence: int = Field(ge=1)
    job_id: uuid.UUID
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: uuid.UUID


class QueuedEvent(_EventBase):
    event_type: Literal["queued"]
    payload: QueuedPayload


class StartedEvent(_EventBase):
    event_type: Literal["started"]
    payload: StartedPayload


class MetaEvent(_EventBase):
    event_type: Literal["meta"]
    payload: MetaPayload


class TokenEvent(_EventBase):
    event_type: Literal["token"]
    payload: TokenPayload


class CompletedEvent(_EventBase):
    event_type: Literal["completed"]
    payload: CompletedPayload


class FailedEvent(_EventBase):
    event_type: Literal["failed"]
    payload: FailedPayload


class RetryingEvent(_EventBase):
    event_type: Literal["retrying"]
    payload: RetryingPayload


InferenceEvent = Annotated[
    QueuedEvent
    | StartedEvent
    | MetaEvent
    | TokenEvent
    | CompletedEvent
    | FailedEvent
    | RetryingEvent,
    Field(discriminator="event_type"),
]
EVENT_ADAPTER: TypeAdapter[InferenceEvent] = TypeAdapter(InferenceEvent)


def parse_job_contract(raw: str | bytes) -> InferenceJobContract:
    try:
        return InferenceJobContract.model_validate_json(raw)
    except ValidationError as exc:
        try:
            version = __import__("json").loads(raw).get("contract_version")
        except Exception:
            version = None
        if version != JOB_CONTRACT_VERSION:
            raise UnsupportedContractVersion(
                f"Unsupported inference job contract version: {version!r}"
            ) from exc
        raise


def parse_event_contract(raw: str | bytes) -> InferenceEvent:
    try:
        return EVENT_ADAPTER.validate_json(raw)
    except ValidationError as exc:
        try:
            version = __import__("json").loads(raw).get("event_contract_version")
        except Exception:
            version = None
        if version != EVENT_CONTRACT_VERSION:
            raise UnsupportedContractVersion(
                f"Unsupported inference event contract version: {version!r}"
            ) from exc
        raise
