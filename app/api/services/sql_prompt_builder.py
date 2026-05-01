from app.api.config import settings
from app.api.services.prompt_builder import PromptBuilder


class SQLPromptBuilder:
    @staticmethod
    def build(
        *,
        question: str,
        retrieved: list[dict],
        validation_errors: list[str] | None = None,
        previous_answer: str | None = None,
    ) -> str:
        context = PromptBuilder._build_context(retrieved, settings.llm_max_context_chars)
        errors_block = ""

        if validation_errors:
            errors = "\n".join(f"- {error}" for error in validation_errors)
            errors_block = (
                "\nThe previous answer did not pass validation.\n"
                "Fix the SQL using only the provided schema.\n"
                f"Validation errors:\n{errors}\n"
            )

        previous_block = ""
        if previous_answer:
            previous_block = f"\nPrevious answer:\n{previous_answer}\n"

        return (
            "You are a PostgreSQL SQL generation assistant.\n"
            "Use only the provided database schema context.\n"
            "Do not invent tables or columns.\n"
            "Use schema-qualified table names, for example rag_kg.orders.\n"
            "Generate only read-only SELECT or WITH queries.\n"
            "Do not generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE or CREATE.\n"
            "If the schema context is insufficient, clearly say what is missing.\n"
            "Return the answer in exactly this format:\n"
            "EXPLANATION:\n"
            "one or two short sentences\n"
            "SQL:\n"
            "the PostgreSQL query without markdown fences\n"
            f"{errors_block}"
            f"{previous_block}\n"
            f"QUESTION:\n{question}\n\n"
            f"DATABASE SCHEMA CONTEXT:\n{context}\n\n"
            "ANSWER:"
        )
