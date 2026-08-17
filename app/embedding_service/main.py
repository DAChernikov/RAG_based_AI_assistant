from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbeddingSettings(BaseSettings):
    embedding_model: str = "BAAI/bge-m3"
    embedding_model_version: str = "bge-m3/1"
    embedding_dimensions: int = 1024
    embedding_device: str = "cpu"
    embedding_max_batch: int = 32
    embedding_max_text_chars: int = 16_000
    embedding_concurrency: int = 2
    embedding_max_queue: int = 64
    embedding_request_timeout_sec: float = 120.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


class EmbeddingBackend(Protocol):
    async def encode(self, texts: list[str]) -> list[list[float]]: ...

    async def close(self) -> None: ...


class LocalBGEBackend:
    def __init__(self, settings: EmbeddingSettings):
        self.settings = settings
        self._model: Any = None
        self._load_lock = asyncio.Lock()

    async def _load(self):
        if self._model is None:
            async with self._load_lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    self._model = await asyncio.to_thread(
                        SentenceTransformer,
                        self.settings.embedding_model,
                        device=self.settings.embedding_device,
                    )
        return self._model

    async def encode(self, texts: list[str]) -> list[list[float]]:
        model = await self._load()
        vectors = await asyncio.to_thread(
            model.encode,
            texts,
            batch_size=self.settings.embedding_max_batch,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return [vector.astype(float).tolist() for vector in vectors]

    async def close(self) -> None:
        self._model = None


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]
    encoding_format: str = "float"


class EmbeddingDatum(BaseModel):
    object: str = "embedding"
    embedding: list[float]
    index: int


class EmbeddingResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    object: str = "list"
    data: list[EmbeddingDatum]
    model: str
    model_version: str
    contract_version: str = "1.0"
    usage: dict[str, int]


def create_app(
    backend: EmbeddingBackend | None = None, settings: EmbeddingSettings | None = None
) -> FastAPI:
    config = settings or EmbeddingSettings()
    runtime_backend = backend or LocalBGEBackend(config)
    semaphore = asyncio.Semaphore(config.embedding_concurrency)
    admission = asyncio.Semaphore(config.embedding_concurrency + config.embedding_max_queue)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await runtime_backend.close()

    application = FastAPI(title="Self-hosted embedding service", version="1.0.0", lifespan=lifespan)

    @application.get("/health")
    async def health():
        return {"status": "ok"}

    @application.get("/ready")
    async def ready():
        return {
            "status": "ready",
            "model_ready": getattr(runtime_backend, "_model", None) is not None,
            "model": config.embedding_model,
            "model_version": config.embedding_model_version,
            "lazy_loaded": getattr(runtime_backend, "_model", None) is None,
        }

    @application.post("/v1/embeddings", response_model=EmbeddingResponse)
    async def embeddings(payload: EmbeddingRequest):
        if payload.model != config.embedding_model:
            raise HTTPException(status_code=404, detail="Embedding model is not configured.")
        texts = [payload.input] if isinstance(payload.input, str) else payload.input
        if not texts or len(texts) > config.embedding_max_batch:
            raise HTTPException(status_code=422, detail="Embedding batch size is invalid.")
        if payload.encoding_format != "float" or any(
            not text or len(text) > config.embedding_max_text_chars for text in texts
        ):
            raise HTTPException(status_code=422, detail="Embedding input is invalid.")
        try:
            try:
                await asyncio.wait_for(admission.acquire(), timeout=0.05)
            except TimeoutError as exc:
                raise HTTPException(status_code=429, detail="Embedding queue is full.") from exc
            try:
                async with semaphore:
                    vectors = await asyncio.wait_for(
                        runtime_backend.encode(texts), config.embedding_request_timeout_sec
                    )
            finally:
                admission.release()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Embedding model is unavailable.") from exc
        if any(len(vector) != config.embedding_dimensions for vector in vectors):
            raise HTTPException(status_code=503, detail="Embedding dimension validation failed.")
        return EmbeddingResponse(
            data=[
                EmbeddingDatum(embedding=vector, index=index)
                for index, vector in enumerate(vectors)
            ],
            model=config.embedding_model,
            model_version=config.embedding_model_version,
            usage={
                "prompt_tokens": sum(len(text) for text in texts),
                "total_tokens": sum(len(text) for text in texts),
            },
        )

    return application


app = create_app()
