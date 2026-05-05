from __future__ import annotations


class SQLPromptBuilder:
    @staticmethod
    def _format_schema_context(schema_docs: list[dict], max_chars: int) -> str:
        parts: list[str] = []
        used_chars = 0

        for idx, doc in enumerate(schema_docs, start=1):
            title = doc.get("title") or doc.get("doc_id") or f"schema_doc_{idx}"
            text = (doc.get("text") or "").strip()

            if not text:
                continue

            block = f"[Schema {idx}: {title}]\n{text}\n"

            if used_chars + len(block) > max_chars:
                remaining = max_chars - used_chars
                if remaining > 500:
                    parts.append(block[:remaining])
                break

            parts.append(block)
            used_chars += len(block)

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
        schema_context = cls._format_schema_context(
            schema_docs,
            max_chars=max_context_chars,
        )

        return f"""You are helping an analyst write a {dialect} query.
            Use only the schema shown below. Table and column names must come from this schema.
            Prefer schema-qualified table names such as rag_kg.orders.
            Generate a read-only query only: SELECT or WITH ... SELECT.
            Always include the SQL query. Do not answer by referring to a previous query.
            Keep the explanation to one short sentence.
            Return the answer in this format:
            EXPLANATION:
            <short explanation>
            SQL:
            ```sql
            <query>
            ```
            Schema:
            {schema_context}
            Question:
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
        schema_context = cls._format_schema_context(
            schema_docs,
            max_chars=max_context_chars,
        )
        errors = "\n".join(f"- {error}" for error in validation_errors)

        return f"""The previous answer did not pass SQL validation. 
            Rewrite it as a valid {dialect} query.
            Use only the provided schema and keep the query read-only.
            Do not add tables or columns that are not present in the schema.
            Always include the corrected SQL query.
            Validation errors:
            {errors}
            Return the answer in this format:
            EXPLANATION:
            <short explanation>
            SQL:
            ```sql
            <query>
            ```
            Schema:
            {schema_context}
            Question:
            {question}
            Previous answer:
            {previous_answer}
            """.strip()

    @classmethod
    def build_sql_only_prompt(
        cls,
        *,
        question: str,
        schema_docs: list[dict],
        dialect: str = "postgres",
        max_context_chars: int = 12000,
    ) -> str:
        schema_context = cls._format_schema_context(
            schema_docs,
            max_chars=max_context_chars,
        )

        return f"""Write one read-only {dialect} query for the question.
            Use only the tables and columns from the schema below.
            Prefer schema-qualified table names.
            Return only the SQL query, without markdown or explanation.
            Schema:
            {schema_context}
            Question:
            {question}
            """.strip()
