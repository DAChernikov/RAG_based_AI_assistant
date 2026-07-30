from __future__ import annotations

from app.api.config import settings
from app.api.services.artifact_manager import ArtifactManager
from app.api.services.llm_service import LLMService
from app.api.services.rag_service import RAGService
from app.api.services.router_service import RouterService
from app.api.services.sql_service import SQLService


class InferenceProcessor:
    def __init__(self):
        self.retriever = None
        self.llm_service = None

    def ensure_loaded(self) -> None:
        if self.retriever is not None:
            return
        from app.api.services.retriever_loader import RetrieverLoader

        manager = ArtifactManager(settings.artifacts_dir)
        manager.prepare()
        self.retriever = RetrieverLoader(settings.artifacts_dir).load()
        self.llm_service = LLMService()

    async def readiness(self) -> dict:
        self.ensure_loaded()
        model = await self.llm_service.readiness()
        return {"retriever_ready": self.retriever is not None, "model": model}

    async def execute(self, contract, emit) -> dict:
        self.ensure_loaded()
        mode = RouterService().route(
            contract.question, contract.requested_mode or settings.default_mode
        )
        if mode == "sql":
            result = await SQLService(self.retriever, self.llm_service).ask(
                question=contract.question,
                top_k=contract.top_k or settings.sql_top_k,
                max_new_tokens=contract.max_new_tokens or settings.sql_max_new_tokens,
            )
            await emit(
                "meta",
                {
                    "mode": result["mode"],
                    "confidence": result.get("confidence"),
                    "retrieved": result.get("retrieved", []),
                },
            )
            await emit("token", {"text": result["answer"]})
            return result

        rag = RAGService(self.retriever, self.llm_service)
        meta, stream = await rag.stream_answer(
            question=contract.question,
            top_k=contract.top_k or settings.top_k,
            max_new_tokens=contract.max_new_tokens
            or (
                settings.code_max_new_tokens if mode == "rag_code" else settings.doc_max_new_tokens
            ),
            mode=mode,
        )
        await emit(
            "meta",
            {
                "mode": meta["mode"],
                "confidence": meta.get("confidence"),
                "retrieved": meta.get("retrieved", []),
            },
        )
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
            await emit("token", {"text": chunk})
        return {
            "question": contract.question,
            "answer": "".join(chunks),
            "mode": mode,
            "confidence": meta.get("confidence"),
            "retrieved": meta.get("retrieved", []),
        }

    async def close(self) -> None:
        if self.llm_service is not None:
            await self.llm_service.aclose()
