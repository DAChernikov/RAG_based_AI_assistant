from __future__ import annotations

from app.api.config import settings
from app.api.services.llm_service import LLMService
from app.api.services.prompt_builder import PromptBuilder
from app.api.services.router_service import RouterService
from app.api.services.sql_service import SQLService
from app.embeddings.client import EmbeddingClient
from app.retrieval.contracts import RetrievalFilters
from app.retrieval.hybrid import HybridRetrievalRepository, HybridRetriever
from app.retrieval.reranker import RerankerClient
from app.sql.explain import SQLExplainContext


class InferenceProcessor:
    def __init__(self, session_factory):
        self.embeddings = EmbeddingClient(
            settings.embedding_api_base_url,
            settings.embedding_model,
            token=settings.embedding_api_token,
            timeout=settings.embedding_request_timeout,
        )
        self.reranker = (
            RerankerClient(
                settings.reranker_api_base_url,
                settings.reranker_api_token,
                settings.reranker_request_timeout,
            )
            if settings.reranker_api_base_url
            else None
        )
        self.retriever = HybridRetriever(
            HybridRetrievalRepository(session_factory), self.embeddings, self.reranker
        )
        self.llm_service = LLMService()
        self.sql_explain = SQLExplainContext(session_factory)

    async def readiness(self) -> dict:
        model = await self.llm_service.readiness()
        embedding = await self.embeddings.readiness()
        return {
            "retriever_ready": embedding.get("model_ready", False),
            "embedding": embedding,
            "model": model,
        }

    async def execute(self, contract, emit) -> dict:
        plan = RouterService().plan(
            contract.question,
            contract.knowledge_base_id,
            contract.requested_mode,
        )
        branches = []
        for target in plan.retrieval_targets:
            source_types = {
                "documentation": ["website"],
                "code": ["git"],
                "database_schema": ["jdbc"],
            }[target]
            branches.append(
                self.retriever.search(
                    contract.tenant_id,
                    contract.knowledge_base_id,
                    contract.question,
                    top_k=contract.top_k or settings.top_k,
                    filters=RetrievalFilters(source_types=source_types),
                )
            )
        import asyncio

        branch_rows = await asyncio.gather(*branches)
        by_id: dict[str, dict] = {}
        for row in (item for branch in branch_rows for item in branch):
            previous = by_id.get(row["doc_id"])
            if previous is None or row["score"] > previous["score"]:
                by_id[row["doc_id"]] = row
        retrieved = sorted(by_id.values(), key=lambda item: (-item["score"], item["doc_id"]))[
            : contract.top_k or settings.top_k
        ]
        mode = (
            "sql"
            if plan.requires_sql
            else ("rag_code" if "code" in plan.retrieval_targets else "rag_docs")
        )
        await emit(
            "meta",
            {
                "mode": mode,
                "confidence": {"router": plan.confidence},
                "retrieved": retrieved,
            },
        )
        if plan.requires_sql:
            result = await SQLService(
                self.retriever,
                self.llm_service,
                explain_parameters=lambda: self.sql_explain.parameters(
                    contract.tenant_id, retrieved
                ),
            ).ask(
                question=contract.question,
                top_k=contract.top_k or settings.sql_top_k,
                max_new_tokens=contract.max_new_tokens or settings.sql_max_new_tokens,
                tenant_id=contract.tenant_id,
                knowledge_base_id=contract.knowledge_base_id,
                retrieved=retrieved,
            )
        else:
            prompt = PromptBuilder.build(contract.question, retrieved, mode)
            chunks = []
            async for chunk in self.llm_service.stream_generate(
                prompt=prompt,
                max_new_tokens=contract.max_new_tokens or settings.doc_max_new_tokens,
            ):
                chunks.append(chunk)
                await emit("token", {"text": chunk})
            result = {
                "answer": "".join(chunks),
                "mode": mode,
                "retrieved": retrieved,
                "prompt_version": "grounded-answer/1.0",
            }
        result["route_plan"] = plan.model_dump(mode="json")
        if plan.requires_sql:
            await emit("token", {"text": result["answer"]})
        return result

    async def close(self) -> None:
        await self.embeddings.close()
        if self.reranker:
            await self.reranker.close()
        await self.llm_service.aclose()
