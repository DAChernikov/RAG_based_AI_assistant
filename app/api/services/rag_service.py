from typing import AsyncIterator

from app.api.services.prompt_builder import PromptBuilder


class RAGService:
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
        }

    @staticmethod
    def _fallback_answer(retrieved: list[dict]) -> str:
        if not retrieved:
            return "No relevant documents found."

        title = retrieved[0].get("title") or retrieved[0].get("doc_id") or "document"
        text = (retrieved[0].get("text") or "").strip().replace("\n", " ")
        return f"[{title}] {text[:900]}"

    async def ask(
        self,
        *,
        question: str,
        top_k: int = 5,
        max_new_tokens: int = 220,
        mode: str = "rag",
    ) -> dict:
        retrieved = self.retriever.search(question, top_k=top_k)
        confidence = self._build_confidence(retrieved, top_k=top_k)

        if not retrieved:
            answer = "No relevant documents found."
        elif self.llm_service.is_configured():
            prompt = PromptBuilder.build(question, retrieved)
            answer = await self.llm_service.generate(
                prompt=prompt,
                max_new_tokens=max_new_tokens,
            )
            if not answer:
                answer = self._fallback_answer(retrieved)
        else:
            answer = self._fallback_answer(retrieved)

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
        max_new_tokens: int = 220,
        mode: str = "rag",
    ) -> tuple[dict, AsyncIterator[str]]:
        retrieved = self.retriever.search(question, top_k=top_k)
        confidence = self._build_confidence(retrieved, top_k=top_k)

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
                yield self._fallback_answer(retrieved)

            return meta, fallback_stream()

        prompt = PromptBuilder.build(question, retrieved)
        return meta, self.llm_service.stream_generate(
            prompt=prompt,
            max_new_tokens=max_new_tokens,
        )
