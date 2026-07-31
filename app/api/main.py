import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.api.config import settings
from app.api.routes import admin, api_keys, ask, auth, catalog, health, jobs, users
from app.api.services.artifact_manager import ArtifactManager
from app.api.services.llm_service import LLMService


def validate_auth_configuration() -> None:
    if settings.auth_disabled and settings.app_env not in {"dev", "test"}:
        raise RuntimeError("AUTH_DISABLED is allowed only in dev/test environments.")
    if not settings.auth_disabled and len(settings.jwt_secret or "") < 32:
        raise RuntimeError("JWT_SECRET with at least 32 characters is required.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    mode = settings.inference_execution_mode.lower()
    if mode not in {"direct", "queued"}:
        raise RuntimeError("INFERENCE_EXECUTION_MODE must be direct or queued.")
    validate_auth_configuration()

    runtime = {
        "execution_mode": mode,
        "artifacts_ready": False,
        "rag_ready": False,
        "llm_name": settings.generation_model,
        "artifacts_dir": settings.artifacts_dir,
        "rag_service": None,
        "retriever": None,
        "llm_service": None,
        "database_engine": None,
        "repository": None,
        "queue": None,
        "queued_application": None,
        "auth_repository": None,
        "auth_service": None,
        "auth_redis": None,
        "catalog_repository": None,
        "catalog_service": None,
        "startup_error": None,
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

        if mode == "direct":
            from app.api.services.rag_service import RAGService
            from app.api.services.retriever_loader import RetrieverLoader

            manager = ArtifactManager(settings.artifacts_dir)
            manager.prepare()
            runtime["artifacts_ready"] = manager.has_required_artifacts()
            if runtime["artifacts_ready"]:
                retriever = RetrieverLoader(settings.artifacts_dir).load()
                llm_service = LLMService()
                runtime["retriever"] = retriever
                runtime["llm_service"] = llm_service
                runtime["rag_service"] = RAGService(retriever=retriever, llm_service=llm_service)
                runtime["rag_ready"] = True
            else:
                runtime["startup_error"] = "Required artifacts were not found."
        else:
            from app.inference.application import QueuedInferenceApplication
            from app.inference.redis_queue import RedisInferenceQueue

            queue = RedisInferenceQueue()
            runtime["queue"] = queue
            runtime["queued_application"] = QueuedInferenceApplication(repository, queue)
            await queue.ensure_group()
    except Exception:
        runtime["startup_error"] = (
            "Runtime initialization failed. Check local configuration and service logs."
        )

    app.state.runtime = runtime
    try:
        yield
    finally:
        llm_service = runtime.get("llm_service")
        if llm_service is not None:
            await llm_service.aclose()
        queue = runtime.get("queue")
        if queue is not None:
            await queue.close()
        auth_redis = runtime.get("auth_redis")
        if auth_redis is not None:
            await auth_redis.aclose()
        engine = runtime.get("database_engine")
        if engine is not None:
            engine.dispose()


app = FastAPI(
    title="RAG Based AI Assistant API",
    version="0.5.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    raw = request.headers.get("X-Correlation-Id")
    try:
        correlation_id = uuid.UUID(raw) if raw else uuid.uuid4()
    except ValueError:
        correlation_id = uuid.uuid4()
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = str(correlation_id)
    return response


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(ask.router)
app.include_router(jobs.router)
app.include_router(api_keys.router)
app.include_router(users.router)
app.include_router(catalog.router)
app.include_router(admin.router)
