from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="User question")
    top_k: int | None = Field(default=None, ge=1, le=50)
    max_new_tokens: int | None = Field(default=None, ge=1, le=4096)
    mode: str | None = Field(default=None, description="Routing mode: rag or sql")


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
    status: str
    artifacts_ready: bool
    rag_ready: bool
    startup_error: str | None = None
