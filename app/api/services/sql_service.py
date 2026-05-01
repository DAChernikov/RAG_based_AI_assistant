import re
from dataclasses import dataclass

from app.api.config import settings
from app.api.services.rag_service import RAGService
from app.api.services.sql_prompt_builder import SQLPromptBuilder


@dataclass
class SQLValidationResult:
    is_valid: bool
    errors: list[str]
    sql: str | None
    used_tables: list[str]


class SQLService:
    DANGEROUS_SQL = re.compile(
        r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|merge)\b",
        re.IGNORECASE,
    )
    TABLE_PATTERN = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)?)", re.I)

    def __init__(self, retriever=None, llm_service=None):
        self.retriever = retriever
        self.llm_service = llm_service

    @staticmethod
    def _schema_doc_table_name(doc: dict) -> str | None:
        metadata = doc.get("metadata") or {}
        table = metadata.get("table")
        schema = metadata.get("schema")
        if schema and table:
            return f"{schema}.{table}"

        title = doc.get("title") or ""
        match = re.search(r"([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)", title)
        if match:
            return match.group(1)

        doc_id = doc.get("doc_id") or ""
        match = re.search(r"([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)", doc_id)
        if match:
            return match.group(1)

        return None

    @classmethod
    def _extract_sql(cls, answer: str) -> str | None:
        if not answer:
            return None

        text = answer.strip()
        text = text.replace("```sql", "").replace("```", "")

        match = re.search(r"\bSQL\s*:\s*(.+)$", text, flags=re.I | re.S)
        if match:
            sql = match.group(1).strip()
        else:
            select_match = re.search(r"\b(with|select)\b.+", text, flags=re.I | re.S)
            if not select_match:
                return None
            sql = select_match.group(0).strip()

        sql = sql.strip()
        if not sql:
            return None

        return sql

    @classmethod
    def _used_tables(cls, sql: str) -> list[str]:
        tables: list[str] = []
        for match in cls.TABLE_PATTERN.finditer(sql or ""):
            table = match.group(1).strip().lower()
            tables.append(table)
        return sorted(set(tables))

    def _validate_sql(self, answer: str, retrieved: list[dict]) -> SQLValidationResult:
        sql = self._extract_sql(answer)
        errors: list[str] = []

        if not sql:
            return SQLValidationResult(
                is_valid=False,
                errors=["No SQL query was found in the answer."],
                sql=None,
                used_tables=[],
            )

        if self.DANGEROUS_SQL.search(sql):
            errors.append("Only read-only SELECT/WITH queries are allowed.")

        if not re.match(r"^\s*(select|with)\b", sql, flags=re.I):
            errors.append("SQL must start with SELECT or WITH.")

        used_tables = self._used_tables(sql)
        allowed_full_tables = {
            table.lower()
            for table in (self._schema_doc_table_name(doc) for doc in retrieved)
            if table
        }
        allowed_short_tables = {table.split(".")[-1] for table in allowed_full_tables}

        for table in used_tables:
            short_table = table.split(".")[-1]
            if table not in allowed_full_tables and short_table not in allowed_short_tables:
                errors.append(f"Table '{table}' is not present in retrieved schema context.")

        return SQLValidationResult(
            is_valid=not errors,
            errors=errors,
            sql=sql,
            used_tables=used_tables,
        )

    def _fallback_answer(self, question: str, retrieved: list[dict]) -> str:
        schema_titles = ", ".join(
            doc.get("title") or doc.get("doc_id", "unknown") for doc in retrieved[:5]
        )
        return (
            "EXPLANATION:\n"
            "The SQL generator could not call the LLM, but relevant schema context was found.\n"
            "SQL:\n"
            f"-- Question: {question}\n"
            f"-- Relevant schema objects: {schema_titles}\n"
            "-- Configure LLM_API_KEY to generate a PostgreSQL query."
        )

    async def ask(
        self,
        question: str,
        top_k: int | None = None,
        max_new_tokens: int | None = None,
    ) -> dict:
        if self.retriever is None:
            return {
                "answer": "SQL service is not initialized: retriever is missing.",
                "mode": "sql",
                "confidence": None,
                "retrieved": [],
            }

        effective_top_k = top_k or settings.sql_top_k
        retrieved = self.retriever.search(
            question,
            top_k=effective_top_k,
            source_filter=["database_schema"],
        )
        confidence = RAGService._build_confidence(retrieved, top_k=len(retrieved))

        if not retrieved:
            return {
                "answer": "No database schema context was found for this SQL question.",
                "mode": "sql",
                "confidence": confidence,
                "retrieved": [],
            }

        if not self.llm_service or not self.llm_service.is_configured():
            answer = self._fallback_answer(question, retrieved)
            validation = self._validate_sql(answer, retrieved)
        else:
            prompt = SQLPromptBuilder.build(question=question, retrieved=retrieved)
            answer = await self.llm_service.generate(
                prompt=prompt,
                max_new_tokens=max_new_tokens or settings.max_new_tokens,
            )
            validation = self._validate_sql(answer, retrieved)

            repair_attempts = 0
            while not validation.is_valid and repair_attempts < settings.sql_max_repair_attempts:
                repair_attempts += 1
                repair_prompt = SQLPromptBuilder.build(
                    question=question,
                    retrieved=retrieved,
                    validation_errors=validation.errors,
                    previous_answer=answer,
                )
                answer = await self.llm_service.generate(
                    prompt=repair_prompt,
                    max_new_tokens=max_new_tokens or settings.max_new_tokens,
                )
                validation = self._validate_sql(answer, retrieved)

        confidence = confidence or {}
        confidence["validation"] = {
            "is_valid": validation.is_valid,
            "errors": validation.errors,
            "used_tables": validation.used_tables,
        }

        return {
            "question": question,
            "answer": answer,
            "mode": "sql",
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
