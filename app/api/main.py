from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.config import settings
from app.api.routes import admin, ask, health
from app.api.services.artifact_manager import ArtifactManager
from app.api.services.llm_service import LLMService
from app.api.services.rag_service import RAGService
from app.api.services.retriever_loader import RetrieverLoader


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = {
        "artifacts_ready": False,
        "rag_ready": False,
        "llm_name": settings.llm_model,
        "artifacts_dir": settings.artifacts_dir,
        "rag_service": None,
        "startup_error": None,
    }

    manager = ArtifactManager(settings.artifacts_dir)

    try:
        manager.prepare()
        runtime["artifacts_ready"] = manager.has_required_artifacts()

        if runtime["artifacts_ready"]:
            retriever = RetrieverLoader(settings.artifacts_dir).load()
            llm_service = LLMService()
            runtime["rag_service"] = RAGService(retriever=retriever, llm_service=llm_service)
            runtime["rag_ready"] = True
        else:
            runtime["startup_error"] = "Required artifacts were not found."
    except Exception as exc:
        runtime["startup_error"] = str(exc)

    app.state.runtime = runtime
    yield


app = FastAPI(
    title="RAG Based AI Assistant API",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(ask.router)
app.include_router(admin.router)
