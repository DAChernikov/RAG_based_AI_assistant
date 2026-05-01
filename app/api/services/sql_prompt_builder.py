from __future__ import annotations


class SQLPromptBuilder:
    @staticmethod
    def _format_schema_context(schema_docs: list[dict], max_chars: int) -> str:
        parts: list[str] = []
        used = 0

        for idx, doc in enumerate(schema_docs, start=1):
            title = doc.get("title") or doc.get("doc_id") or f"schema_doc_{idx}"
            text = (doc.get("text") or "").strip()
            block = f"[Schema source {idx}: {title}]\n{text}\n"

            if used + len(block) > max_chars:
                remaining = max_chars - used
                if remaining > 500:
                    parts.append(block[:remaining])
                break

            parts.append(block)
            used += len(block)

        return "\n".join(parts).strip()

    @classmethod
    def build_generate_prompt(
        cls,
        *,
        question: str,
        schema_docs: list[dict],
        dialect: str = "postgres",
        max_context_chars: int = 12000,
    ) -> str:
        schema_context = cls._format_schema_context(schema_docs, max_chars=max_context_chars)

        return f"""
You are a senior data analyst and SQL generation assistant.
Generate SQL for the user's analytical question.

Rules:
- SQL dialect: {dialect}.
- Use only tables and columns from the provided database schema context.
- Do not invent table names or column names.
- Prefer schema-qualified table names, for example rag_kg.orders.
- Generate only read-only SQL: SELECT or WITH ... SELECT.
- Do not generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE or CREATE.
- If the schema context is insufficient, explain what is missing and do not invent SQL.
- Keep the answer concise.

Return exactly this structure:
EXPLANATION:
<short explanation of the query logic>

SQL:
<single SQL query>

DATABASE SCHEMA CONTEXT:
{schema_context}

USER QUESTION:
{question}
""".strip()

    @classmethod
    def build_repair_prompt(
        cls,
        *,
        question: str,
        schema_docs: list[dict],
        previous_answer: str,
        validation_errors: list[str],
        dialect: str = "postgres",
        max_context_chars: int = 12000,
    ) -> str:
        schema_context = cls._format_schema_context(schema_docs, max_chars=max_context_chars)
        errors = "\n".join(f"- {err}" for err in validation_errors)

        return f"""
You generated SQL that failed validation.
Repair the SQL using only the provided database schema context.

Rules:
- SQL dialect: {dialect}.
- Use only tables and columns from the schema context.
- Return only read-only SQL: SELECT or WITH ... SELECT.
- Do not invent missing tables or columns.
- If the schema is insufficient, explain what is missing.

Validation errors:
{errors}

Return exactly this structure:
EXPLANATION:
<short explanation of the repaired query>

SQL:
<single repaired SQL query>

DATABASE SCHEMA CONTEXT:
{schema_context}

USER QUESTION:
{question}

PREVIOUS ANSWER:
{previous_answer}
""".strip()
