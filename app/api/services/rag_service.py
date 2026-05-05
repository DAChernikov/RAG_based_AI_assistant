from __future__ import annotations

from typing import AsyncIterator

from app.api.config import settings
from app.api.services.prompt_builder import PromptBuilder


class RAGService:
    """RAG-сервис для поиска ответов по документации/шаблонам кода в корпусе retriever.
    Рассматривается только docs и code часть, SQL вынесен в отдельный сервис.
    """

    UNHELPFUL_MARKERS = (
        "the provided context does not contain",
        "the context does not contain",
        "not enough information",
        "insufficient information",
        "not enough context",
        "cannot determine from the context",
    )

    def __init__(self, retriever, llm_service):
        self.retriever = retriever
        self.llm_service = llm_service

    @staticmethod
    def _build_confidence(retrieved: list[dict], top_k: int) -> dict | None:
        if not retrieved:
            return None

        top1 = float(retrieved[0]["score"])
        top2 = float(retrieved[1]["score"]) if len(retrieved) > 1 else 0.0

        return {
            "top_k": top_k,
            "top1_score": round(top1, 6),
            "gap12": round(top1 - top2, 6),
            "top1_source": retrieved[0].get("source"),
            "top1_title": retrieved[0].get("title") or retrieved[0].get("doc_id"),
        }

    @staticmethod
    def _snippet(text: str, limit: int = 900) -> str:
        return (text or "").strip().replace("\n", " ")[:limit]

    def _looks_unhelpful(self, answer: str) -> bool:
        if not answer or len(answer.strip()) < 25:
            return True

        lower = answer.lower()
        return any(marker in lower for marker in self.UNHELPFUL_MARKERS)

    def _source_boosts_for_docs(self, question: str) -> dict[str, float]:
        """Подсказки для retriver связанные с вопросами по документации.
        Придает больше веса в сторону docs части корпуса
        """

        q = question.lower()
        boosts = {"codesearchnet": -0.08}

        if "spark" in q or "pyspark" in q:
            boosts["spark_docs"] = 0.15

        if "trino" in q:
            boosts["trino_docs"] = 0.15

        if "hive" in q or "metastore" in q:
            boosts["hive_docs"] = 0.15

        return boosts

    def _retrieve(self, question: str, mode: str, top_k: int | None) -> list[dict]:
        if mode == "rag_code":
            effective_top_k = top_k or settings.code_top_k
            return self.retriever.search(
                question,
                top_k=effective_top_k,
                preferred_sources=["codesearchnet"],
                source_boosts={"codesearchnet": 0.18},
            )

        effective_top_k = top_k or settings.doc_top_k
        return self.retriever.search(
            question,
            top_k=effective_top_k,
            source_boosts=self._source_boosts_for_docs(question),
        )

    def _fallback_answer(self, retrieved: list[dict], mode: str) -> str:
        if not retrieved:
            return "No relevant documents found."

        top = retrieved[0]
        title = top.get("title") or top.get("doc_id") or "document"
        text = top.get("text") or ""

        if mode == "rag_code":
            return (
                f"I found a relevant code pattern in {title}. "
                f"The safest grounded fallback is this snippet: {self._snippet(text, limit=1100)}"
            )

        return f"[{title}] {self._snippet(text, limit=1000)}"

    async def ask(
        self,
        *,
        question: str,
        top_k: int = 5,
        max_new_tokens: int = 320,
        mode: str = "rag_docs",
    ) -> dict:
        retrieved = self._retrieve(question, mode=mode, top_k=top_k)
        confidence = self._build_confidence(retrieved, top_k=len(retrieved))

        if not retrieved:
            answer = "No relevant documents found."
        elif self.llm_service.is_configured():
            prompt = PromptBuilder.build(question, retrieved, mode=mode)
            answer = await self.llm_service.generate(
                prompt=prompt,
                max_new_tokens=max_new_tokens,
            )

            top1 = float(retrieved[0]["score"])
            if self._looks_unhelpful(answer):
                if mode == "rag_code" and top1 >= settings.min_confident_code_score:
                    answer = self._fallback_answer(retrieved, mode=mode)
                elif mode == "rag_docs" and top1 >= settings.min_confident_doc_score:
                    answer = self._fallback_answer(retrieved, mode=mode)
                else:
                    answer = "The provided context does not contain enough relevant information."
        else:
            answer = self._fallback_answer(retrieved, mode=mode)

        return {
            "question": question,
            "answer": answer,
            "mode": mode,
            "confidence": confidence,
            "retrieved": [
                {
                    "doc_id": r["doc_id"],
                    "source": r["source"],
                    "score": r["score"],
                    "title": r.get("title"),
                }
                for r in retrieved
            ],
        }

    async def stream_answer(
        self,
        *,
        question: str,
        top_k: int = 5,
        max_new_tokens: int = 320,
        mode: str = "rag_docs",
    ) -> tuple[dict, AsyncIterator[str]]:
        retrieved = self._retrieve(question, mode=mode, top_k=top_k)
        confidence = self._build_confidence(retrieved, top_k=len(retrieved))

        meta = {
            "question": question,
            "mode": mode,
            "confidence": confidence,
            "retrieved": [
                {
                    "doc_id": r["doc_id"],
                    "source": r["source"],
                    "score": r["score"],
                    "title": r.get("title"),
                }
                for r in retrieved
            ],
        }

        if not retrieved:

            async def empty_stream():
                yield "No relevant documents found."

            return meta, empty_stream()

        if not self.llm_service.is_configured():

            async def fallback_stream():
                yield self._fallback_answer(retrieved, mode=mode)

            return meta, fallback_stream()

        prompt = PromptBuilder.build(question, retrieved, mode=mode)
        return meta, self.llm_service.stream_generate(
            prompt=prompt,
            max_new_tokens=max_new_tokens,
        )
