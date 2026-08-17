import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from app.api.config import settings
from app.api.routes import (
    admin,
    api_keys,
    ask,
    auth,
    catalog,
    health,
    indexing,
    jobs,
    knowledge,
    operations,
    users,
)
from app.observability import configure_tracing, log_event, tracer

_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


def validate_auth_configuration() -> None:
    if settings.auth_disabled and settings.app_env not in {"dev", "test"}:
        raise RuntimeError("AUTH_DISABLED is allowed only in dev/test environments.")
    if not settings.auth_disabled and len(settings.jwt_secret or "") < 32:
        raise RuntimeError("JWT_SECRET with at least 32 characters is required.")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    validate_auth_configuration()
    configure_tracing(settings.otel_service_name, settings.otel_exporter_otlp_endpoint)

    runtime: dict[str, Any] = {
        "settings": settings,
        "llm_name": settings.generation_model,
        "database_engine": None,
        "repository": None,
        "queue": None,
        "queued_application": None,
        "auth_repository": None,
        "auth_service": None,
        "auth_redis": None,
        "catalog_repository": None,
        "catalog_service": None,
        "ingestion_queue": None,
        "startup_error": None,
        "inference_semaphore": asyncio.Semaphore(settings.inference_api_concurrency),
        "index_repository": None,
        "indexing_queue": None,
        "embedding_client": None,
        "operations_repository": None,
    }

    try:
        from app.state.auth_repository import AuthRepository
        from app.state.database import create_database_engine, create_session_factory
        from app.state.repositories import ApplicationRepository, AsyncApplicationRepository

        engine = create_database_engine()
        session_factory = create_session_factory(engine)
        sync_repository = ApplicationRepository(session_factory)
        repository = AsyncApplicationRepository(sync_repository)
        auth_repository = AuthRepository(session_factory)
        from app.catalog.repository import CatalogRepository
        from app.catalog.service import CatalogService

        catalog_repository = CatalogRepository(session_factory)
        runtime["database_engine"] = engine
        runtime["repository"] = repository
        runtime["auth_repository"] = auth_repository
        runtime["catalog_repository"] = catalog_repository
        runtime["catalog_service"] = CatalogService(catalog_repository)
        from app.operations.repository import OperationsRepository

        operations_repository = OperationsRepository(session_factory)
        runtime["operations_repository"] = operations_repository
        from app.embeddings.client import EmbeddingClient
        from app.indexing.redis_queue import RedisIndexingQueue
        from app.indexing.repository import IndexRepository

        runtime["index_repository"] = IndexRepository(session_factory)
        embedding_client = EmbeddingClient(
            settings.embedding_api_base_url,
            settings.embedding_model,
            token=settings.embedding_api_token,
            timeout=settings.embedding_request_timeout,
            expected_version=settings.embedding_model_version,
            expected_dimensions=settings.embedding_dimensions,
        )
        runtime["embedding_client"] = embedding_client
        from app.operations.runtime_registry import RegistryEmbeddingGateway, RuntimeRegistry
        from app.retrieval.hybrid import HybridRetrievalRepository, HybridRetriever

        runtime["hybrid_retriever"] = HybridRetriever(
            HybridRetrievalRepository(session_factory),
            RegistryEmbeddingGateway(RuntimeRegistry(operations_repository)),
        )
        indexing_queue = RedisIndexingQueue()
        await indexing_queue.ensure_group()
        runtime["indexing_queue"] = indexing_queue
        from app.ingestion.redis_queue import RedisIngestionQueue

        ingestion_queue = RedisIngestionQueue()
        await ingestion_queue.ensure_group()
        runtime["ingestion_queue"] = ingestion_queue
        if not settings.auth_disabled:
            from redis.asyncio import Redis

            from app.auth.rate_limit import RedisLoginRateLimiter
            from app.auth.service import AuthService

            auth_redis = Redis.from_url(settings.redis_url, decode_responses=True)
            runtime["auth_redis"] = auth_redis
            runtime["auth_service"] = AuthService(
                auth_repository,
                RedisLoginRateLimiter(
                    auth_redis,
                    settings.login_rate_limit_attempts,
                    settings.login_rate_limit_window_sec,
                    settings.login_rate_limit_prefix,
                ),
            )

        from app.inference.application import QueuedInferenceApplication
        from app.inference.redis_queue import RedisInferenceQueue

        queue = RedisInferenceQueue()
        runtime["queue"] = queue
        runtime["queued_application"] = QueuedInferenceApplication(repository, queue)
        await queue.ensure_group()
    except Exception as exc:
        runtime["startup_error"] = (
            "Runtime initialization failed. Check local configuration and service logs."
        )
        log_event("api_startup_failed", error_type=type(exc).__name__)
        raise RuntimeError(runtime["startup_error"]) from exc

    app.state.runtime = runtime
    try:
        yield
    finally:
        queue = runtime.get("queue")
        if queue is not None:
            await queue.close()
        ingestion_queue = runtime.get("ingestion_queue")
        if ingestion_queue is not None:
            await ingestion_queue.close()
        auth_redis = runtime.get("auth_redis")
        if auth_redis is not None:
            await auth_redis.aclose()
        indexing_queue = runtime.get("indexing_queue")
        if indexing_queue is not None:
            await indexing_queue.close()
        embedding_client = runtime.get("embedding_client")
        if embedding_client is not None:
            await embedding_client.close()
        engine = runtime.get("database_engine")
        if engine is not None:
            engine.dispose()


app = FastAPI(
    title="RAG Based AI Assistant API",
    version="0.5.0",
    lifespan=lifespan,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
    )


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    raw = request.headers.get("X-Correlation-Id")
    try:
        correlation_id = uuid.UUID(raw) if raw else uuid.uuid4()
    except ValueError:
        correlation_id = uuid.uuid4()
    request.state.correlation_id = correlation_id
    content_length = request.headers.get("content-length")
    try:
        body_too_large = bool(
            content_length and int(content_length) > settings.max_request_body_bytes
        )
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length."})
    if body_too_large:
        return JSONResponse(status_code=413, content={"detail": "Request body is too large."})
    category = None
    limit = 0
    if request.method == "POST" and (
        request.url.path.startswith("/ask") or request.url.path.startswith("/v1/inference-jobs")
    ):
        category, limit = "inference", settings.inference_rate_limit_per_minute
    elif request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith(
        "/v1/admin"
    ):
        category, limit = "admin", settings.admin_mutation_rate_limit_per_minute
    runtime = getattr(request.app.state, "runtime", {})
    redis = runtime.get("auth_redis")
    if category and redis is not None:
        credential = request.headers.get("authorization") or request.headers.get("x-api-key", "")
        fingerprint = hashlib.sha256(credential.encode()).hexdigest()[:24]
        try:
            count = await redis.eval(
                _RATE_LIMIT_SCRIPT, 1, f"rag:rate:{category}:{fingerprint}", "60"
            )
        except Exception as exc:
            log_event("rate_limit_unavailable", error_type=type(exc).__name__)
            return JSONResponse(status_code=503, content={"detail": "Rate limiter unavailable."})
        if int(count) > limit:
            return JSONResponse(
                status_code=429,
                content={"detail": "Request rate limit exceeded."},
                headers={"Retry-After": "60"},
            )
    with tracer().start_as_current_span(
        "http.request",
        attributes={"http.request.method": request.method, "url.path": request.url.path},
    ):
        response = await call_next(request)
    response.headers["X-Correlation-Id"] = str(correlation_id)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(ask.router)
app.include_router(jobs.router)
app.include_router(knowledge.router)
app.include_router(api_keys.router)
app.include_router(users.router)
app.include_router(catalog.router)
app.include_router(indexing.router)
app.include_router(operations.router)
app.include_router(admin.router)
app.mount("/metrics", make_asgi_app())
