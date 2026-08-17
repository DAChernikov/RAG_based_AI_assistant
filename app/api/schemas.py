import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="User question")
    top_k: int | None = Field(default=None, ge=1, le=50)
    max_new_tokens: int | None = Field(default=None, ge=1, le=4096)
    mode: str | None = Field(default=None, description="Routing mode: rag or sql")
    conversation_id: uuid.UUID | None = None
    knowledge_base_id: uuid.UUID | None = None


class RetrievedDocument(BaseModel):
    doc_id: str
    source: str
    score: float
    title: str | None = None
    uri: str | None = None
    metadata: dict = Field(default_factory=dict)


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
    status: str
    execution_mode: str = "queued"
    components: dict[str, str] = Field(default_factory=dict)


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
