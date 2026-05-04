from app.api.config import settings


class PromptBuilder:
    @staticmethod
    def build(question: str, retrieved: list[dict], mode: str) -> str:
        if mode == "rag_code":
            return PromptBuilder._build_code_prompt(question, retrieved)
        return PromptBuilder._build_docs_prompt(question, retrieved)

    @staticmethod
    def _build_context(retrieved: list[dict], max_chars: int) -> str:
        blocks: list[str] = []
        used = 0

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
                f"content:\n{text}\n"
            )

            if used + len(block) > max_chars:
                break

            blocks.append(block)
            used += len(block)

        return "\n\n".join(blocks)

    @staticmethod
    def _build_docs_prompt(question: str, retrieved: list[dict]) -> str:
        context = PromptBuilder._build_context(retrieved, settings.llm_max_context_chars)

        return (
            "You are a technical RAG assistant.\n"
            "Answer only from the provided context.\n"
            "Give a complete but concise answer. Do not stop mid-sentence.\n"
            "If the context is relevant but incomplete, give the safest grounded answer.\n"
            "Only say that the context is insufficient if the retrieved sources "
            "are clearly unrelated.\n"
            "If the user asks 'how to', provide short steps or a minimal example.\n"
            "If you include code, commands, SQL, JSON, YAML, or config, wrap it in fenced "
            "markdown code blocks with the correct language, for example ```python.\n\n"
            f"QUESTION:\n{question}\n\n"
            f"CONTEXT:\n{context}\n\n"
            "ANSWER:"
        )

    @staticmethod
    def _build_code_prompt(question: str, retrieved: list[dict]) -> str:
        context = PromptBuilder._build_context(retrieved, settings.llm_max_context_chars)

        return (
            "You are a Python and data-engineering code assistant.\n"
            "The retrieved context may contain code snippets with little or no prose.\n"
            "Infer the answer from code, API names, function names, and surrounding text.\n"
            "Give a complete answer and do not stop mid-sentence.\n"
            "If snippets are relevant, explain the pattern briefly and give one minimal example.\n"
            "Wrap every code example in a fenced markdown code block with the correct language, "
            "for example ```python.\n"
            "Do not say the context is missing if there is obviously relevant code.\n\n"
            f"QUESTION:\n{question}\n\n"
            f"CONTEXT:\n{context}\n\n"
            "ANSWER:"
        )
