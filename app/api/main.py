from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.config import settings
from app.api.routes import admin, ask, health, jobs
from app.api.services.artifact_manager import ArtifactManager
from app.api.services.llm_service import LLMService


@asynccontextmanager
async def lifespan(app: FastAPI):
    mode = settings.inference_execution_mode.lower()
    if mode not in {"direct", "queued"}:
        raise RuntimeError("INFERENCE_EXECUTION_MODE must be direct or queued.")

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
        "startup_error": None,
    }

    try:
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
            from app.state.database import create_database_engine, create_session_factory
            from app.state.repositories import ApplicationRepository

            engine = create_database_engine()
            repository = ApplicationRepository(create_session_factory(engine))
            queue = RedisInferenceQueue()
            runtime["database_engine"] = engine
            runtime["repository"] = repository
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
        engine = runtime.get("database_engine")
        if engine is not None:
            engine.dispose()


app = FastAPI(
    title="RAG Based AI Assistant API",
    version="0.4.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(ask.router)
app.include_router(jobs.router)
app.include_router(admin.router)
