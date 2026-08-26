from __future__ import annotations

import asyncio
import uuid

from app.api.config import settings
from app.api.services.prompt_builder import PromptBuilder
from app.api.services.router_service import RouterService
from app.api.services.sql_service import SQLService
from app.concurrency import api_blocking_io
from app.operations.repository import OperationsRepository
from app.operations.runtime_registry import (
    RegistryEmbeddingGateway,
    RuntimeConfigurationError,
    RuntimeRegistry,
    render_prompt,
)
from app.retrieval.contracts import RetrievalFilters
from app.retrieval.hybrid import HybridRetrievalRepository, HybridRetriever
from app.secrets.store import EncryptedDatabaseSecretStore, load_master_key
from app.sql.explain import SQLExplainContext


class InferenceProcessor:
    def __init__(self, session_factory):
        self.registry = RuntimeRegistry(
            OperationsRepository(session_factory),
            EncryptedDatabaseSecretStore(session_factory, load_master_key()),
        )
        self.embeddings = RegistryEmbeddingGateway(self.registry)
        self.retriever = HybridRetriever(
            HybridRetrievalRepository(session_factory), self.embeddings
        )
        self.sql_explain = SQLExplainContext(session_factory)

    async def readiness(self) -> dict:
        try:
            definition = await api_blocking_io.call(
                self.registry.model, uuid.UUID(int=0), "generation"
            )
            llm = self.registry.generation_client(definition)
            try:
                model = await llm.readiness()
            finally:
                await llm.aclose()
        except Exception:
            model = {"ready": False, "status": "not_configured"}
        embedding = await self.embeddings.readiness()
        return {
            "retriever_ready": embedding.get("model_ready", False),
            "embedding": embedding,
            "model": model,
        }

    async def execute(self, contract, emit) -> dict:
        generation = await api_blocking_io.call(
            self.registry.model, contract.tenant_id, "generation"
        )
        plan = RouterService().plan(
            contract.question,
            contract.knowledge_base_id,
            contract.requested_mode,
        )
        prompt_name = (
            "sql-generation"
            if plan.requires_sql
            else ("grounded-answer" if plan.retrieval_targets else "general-answer")
        )
        prompt_definition = await api_blocking_io.call(
            self.registry.prompt, contract.tenant_id, prompt_name
        )
        llm_service = self.registry.generation_client(generation)
        reranker = None
        try:
            reranker_model = await api_blocking_io.call(
                self.registry.model, contract.tenant_id, "reranker"
            )
            reranker = self.registry.reranker_client(reranker_model)
        except RuntimeConfigurationError:
            pass
        retriever = HybridRetriever(self.retriever.repository, self.embeddings, reranker)
        branches = []
        for target in plan.retrieval_targets:
            source_types = {
                "documentation": ["website"],
                "code": ["git"],
                "database_schema": ["jdbc"],
            }[target]
            branches.append(
                retriever.search(
                    contract.tenant_id,
                    contract.knowledge_base_id,
                    contract.question,
                    top_k=contract.top_k or settings.top_k,
                    filters=RetrievalFilters(source_types=source_types),
                )
            )
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
            else (
                "rag_code"
                if "code" in plan.retrieval_targets
                else ("rag_docs" if plan.retrieval_targets else "model")
            )
        )
        await emit(
            "meta",
            {
                "mode": mode,
                "confidence": {"router": plan.confidence},
                "retrieved": retrieved,
            },
        )
        try:
            if plan.requires_sql:
                result = await SQLService(
                    retriever,
                    llm_service,
                    explain_parameters=lambda: self.sql_explain.parameters(
                        contract.tenant_id, retrieved
                    ),
                    prompt_template=prompt_definition,
                ).ask(
                    question=contract.question,
                    top_k=contract.top_k or settings.sql_top_k,
                    max_new_tokens=contract.max_new_tokens or settings.sql_max_new_tokens,
                    tenant_id=contract.tenant_id,
                    knowledge_base_id=contract.knowledge_base_id,
                    retrieved=retrieved,
                )
            else:
                context = PromptBuilder._build_context(retrieved, settings.model_max_context_chars)
                prompt = render_prompt(
                    prompt_definition,
                    question=contract.question,
                    context=context,
                    mode=mode,
                )
                chunks = []
                async for chunk in llm_service.stream_generate(
                    prompt=prompt,
                    max_new_tokens=contract.max_new_tokens or settings.doc_max_new_tokens,
                ):
                    chunks.append(chunk)
                    await emit("token", {"text": chunk})
                result = {
                    "answer": "".join(chunks),
                    "mode": mode,
                    "retrieved": retrieved,
                    "prompt_version": f"{prompt_definition.name}/{prompt_definition.version}",
                }
        finally:
            if reranker is not None:
                await reranker.close()
            await llm_service.aclose()
        result["model_name"] = f"{generation.model_id}@{generation.version}"
        result["model_definition_id"] = generation.definition_id
        result["prompt_template_id"] = prompt_definition.template_id
        result["route_plan"] = plan.model_dump(mode="json")
        if plan.requires_sql:
            await emit("token", {"text": result["answer"]})
        return result

    async def close(self) -> None:
        await self.embeddings.close()
