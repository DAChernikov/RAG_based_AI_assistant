import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="User question")
    top_k: int | None = Field(default=None, ge=1, le=50)
    max_new_tokens: int | None = Field(default=None, ge=1, le=4096)
    mode: str | None = Field(default=None, description="Routing mode: rag or sql")
    conversation_id: uuid.UUID | None = None


class RetrievedDocument(BaseModel):
    doc_id: str
    source: str
    score: float
    title: str | None = None


class AskResponse(BaseModel):
    question: str
    answer: str
    mode: str
    confidence: dict | None = None
    retrieved: list[RetrievedDocument] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    app_env: str


class ReadyResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    status: str
    artifacts_ready: bool
    rag_ready: bool
    startup_error: str | None = None
    execution_mode: str = "direct"
    model_ready: bool | None = None
    model_status: str | None = None
    database_ready: bool | None = None
    redis_ready: bool | None = None
    worker_ready: bool | None = None
    worker_heartbeat_age_sec: float | None = None


class InferenceJobAccepted(BaseModel):
    job_id: uuid.UUID
    status: str
    conversation_id: uuid.UUID
    correlation_id: uuid.UUID
    status_url: str
    events_url: str
    reused: bool = False


class InferenceJobStatusResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    conversation_id: uuid.UUID
    contract_version: str
    attempt_count: int
    max_attempts: int
    queued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    error: dict | None = None
    answer: dict | None = None
    sources: list[dict] = Field(default_factory=list)
