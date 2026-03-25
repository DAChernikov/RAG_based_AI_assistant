from app.api.config import settings


class PromptBuilder:
    @staticmethod
    def build(question: str, retrieved: list[dict]) -> str:
        blocks: list[str] = []
        total = 0
        limit = settings.llm_max_context_chars

        for idx, item in enumerate(retrieved, start=1):
            title = item.get("title") or item.get("doc_id") or f"doc_{idx}"
            source = item.get("source", "unknown")
            score = item.get("score", 0.0)
            text = (item.get("text") or "").strip()

            block = (
                f"[SOURCE {idx}]\n"
                f"title: {title}\n"
                f"source: {source}\n"
                f"score: {score:.4f}\n"
                f"text:\n{text}\n"
            )

            if total + len(block) > limit:
                break

            blocks.append(block)
            total += len(block)

        context = "\n\n".join(blocks)

        return (
            "Use the following retrieved context to answer the question.\n"
            "Cite the answer implicitly from the context, but do not invent facts.\n\n"
            f"QUESTION:\n{question}\n\n"
            f"CONTEXT:\n{context}\n\n"
            "ANSWER:"
        )
