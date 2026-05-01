from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.api.config import settings
from app.api.services.rag_service import RAGService
from app.api.services.sql_prompt_builder import SQLPromptBuilder

try:
    import psycopg
except Exception:  # pragma: no cover - optional dependency/runtime env
    psycopg = None


@dataclass
class SQLValidationResult:
    is_valid: bool
    errors: list[str]
    warnings: list[str]
    sql: str | None
    used_tables: list[str]
    allowed_tables: list[str]
    used_columns: list[str]
    allowed_columns: list[str]
    explain_checked: bool = False
    explain_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "sql": self.sql,
            "used_tables": self.used_tables,
            "allowed_tables": self.allowed_tables,
            "used_columns": self.used_columns,
            "allowed_columns": self.allowed_columns,
            "explain_checked": self.explain_checked,
            "explain_error": self.explain_error,
        }


class SQLService:
    FORBIDDEN_KEYWORDS = (
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "truncate",
        "create",
        "grant",
        "revoke",
        "merge",
        "copy",
        "call",
    )

    SQL_ALIAS_STOPWORDS = {
        "on",
        "where",
        "join",
        "left",
        "right",
        "inner",
        "outer",
        "full",
        "cross",
        "group",
        "order",
        "limit",
        "having",
        "union",
    }

    TABLE_PATTERN = re.compile(
        r"\b(?:from|join)\s+([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)?)"
        r"(?:\s+(?:as\s+)?([a-zA-Z_][\w]*))?",
        flags=re.IGNORECASE,
    )
    COLUMN_REF_PATTERN = re.compile(r"\b([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)\b")
    CODE_FENCE_PATTERN = re.compile(r"```(?:sql)?\s*(.*?)```", flags=re.IGNORECASE | re.DOTALL)

    def __init__(self, retriever, llm_service):
        self.retriever = retriever
        self.llm_service = llm_service

    @staticmethod
    def _build_confidence(
        retrieved: list[dict], top_k: int, validation: SQLValidationResult
    ) -> dict:
        base = RAGService._build_confidence(retrieved, top_k=top_k) or {}
        base["validation"] = validation.to_dict()
        base["sql_top_k"] = top_k
        base["source_filter"] = [settings.sql_schema_source]
        return base

    @staticmethod
    def _compact_retrieved(retrieved: list[dict]) -> list[dict]:
        return [
            {
                "doc_id": r.get("doc_id", "unknown"),
                "source": r.get("source", "unknown"),
                "score": float(r.get("score", 0.0)),
                "title": r.get("title"),
            }
            for r in retrieved
        ]

    @staticmethod
    def _strip_trailing_semicolon(sql_text: str) -> str:
        return sql_text.strip().rstrip(";").strip()

    @classmethod
    def extract_sql(cls, answer: str) -> str | None:
        if not answer:
            return None

        fenced = cls.CODE_FENCE_PATTERN.findall(answer)
        if fenced:
            return cls._strip_trailing_semicolon(fenced[-1])

        marker = re.search(r"\bSQL\s*:\s*", answer, flags=re.IGNORECASE)
        if marker:
            candidate = answer[marker.end() :].strip()
            candidate = re.split(
                r"\n\s*(?:EXPLANATION|VALIDATION|SOURCES)\s*:", candidate, flags=re.I
            )[0]
            return cls._strip_trailing_semicolon(candidate)

        match = re.search(r"\b(select|with)\b", answer, flags=re.IGNORECASE)
        if match:
            return cls._strip_trailing_semicolon(answer[match.start() :])

        return None

    @staticmethod
    def _normalize_identifier(name: str) -> str:
        return name.strip().strip('"').lower()

    @classmethod
    def extract_table_refs(cls, sql_text: str | None) -> tuple[list[str], dict[str, str]]:
        if not sql_text:
            return [], {}

        tables: list[str] = []
        aliases: dict[str, str] = {}

        for match in cls.TABLE_PATTERN.finditer(sql_text):
            table = cls._normalize_identifier(match.group(1))
            alias = match.group(2)

            if table not in tables:
                tables.append(table)

            short_table = table.split(".")[-1]
            aliases[short_table] = table
            aliases[table] = table

            if alias:
                normalized_alias = cls._normalize_identifier(alias)
                if normalized_alias not in cls.SQL_ALIAS_STOPWORDS:
                    aliases[normalized_alias] = table

        return tables, aliases

    @classmethod
    def extract_used_tables(cls, sql_text: str | None) -> list[str]:
        tables, _ = cls.extract_table_refs(sql_text)
        return tables

    @staticmethod
    def _table_metadata_from_docs(schema_docs: list[dict]) -> dict[str, set[str]]:
        table_to_columns: dict[str, set[str]] = {}

        for doc in schema_docs:
            title = (doc.get("title") or "").strip().lower()
            metadata = doc.get("metadata") or {}
            schema = str(metadata.get("schema") or "").strip().lower()
            table = str(metadata.get("table") or "").strip().lower()
            columns = {str(col).strip().lower() for col in metadata.get("columns") or []}

            candidates = set()
            if title:
                candidates.add(title)
                candidates.add(title.split(".")[-1])
            if schema and table:
                candidates.add(f"{schema}.{table}")
                candidates.add(table)

            for candidate in candidates:
                if candidate:
                    table_to_columns[candidate] = set(columns)

        return table_to_columns

    @staticmethod
    def _allowed_tables_from_docs(schema_docs: list[dict]) -> list[str]:
        allowed: list[str] = []

        for doc in schema_docs:
            title = (doc.get("title") or "").strip()
            metadata = doc.get("metadata") or {}
            schema = metadata.get("schema")
            table = metadata.get("table")

            candidates = []
            if title:
                candidates.append(title)
            if schema and table:
                candidates.append(f"{schema}.{table}")
                candidates.append(str(table))

            for candidate in candidates:
                normalized = candidate.strip().strip('"').lower()
                if normalized and normalized not in allowed:
                    allowed.append(normalized)

        return allowed

    @staticmethod
    def _allowed_columns_from_docs(schema_docs: list[dict]) -> list[str]:
        allowed: list[str] = []

        for doc in schema_docs:
            metadata = doc.get("metadata") or {}
            schema = str(metadata.get("schema") or "").strip().lower()
            table = str(metadata.get("table") or "").strip().lower()
            columns = metadata.get("columns") or []

            for column in columns:
                column = str(column).strip().lower()
                if not column:
                    continue
                candidates = [f"{table}.{column}"] if table else []
                if schema and table:
                    candidates.append(f"{schema}.{table}.{column}")
                for candidate in candidates:
                    if candidate not in allowed:
                        allowed.append(candidate)

        return allowed

    @classmethod
    def extract_used_columns(
        cls, sql_text: str | None, alias_to_table: dict[str, str]
    ) -> list[str]:
        if not sql_text:
            return []

        used: list[str] = []
        for qualifier, column in cls.COLUMN_REF_PATTERN.findall(sql_text):
            qualifier = cls._normalize_identifier(qualifier)
            column = cls._normalize_identifier(column)
            table = alias_to_table.get(qualifier, qualifier)

            # Skip schema.table references in FROM/JOIN. They are table refs, not column refs.
            if f"{qualifier}.{column}" in alias_to_table:
                continue

            full_name = f"{table}.{column}"
            if full_name not in used:
                used.append(full_name)

        return used

    @staticmethod
    def _has_forbidden_keyword(sql_text: str) -> str | None:
        lowered = re.sub(r"\s+", " ", sql_text.lower())
        for keyword in SQLService.FORBIDDEN_KEYWORDS:
            if re.search(rf"\b{re.escape(keyword)}\b", lowered):
                return keyword
        return None

    async def _postgres_explain(self, sql_text: str) -> tuple[bool, str | None]:
        if not settings.sql_enable_explain_validation:
            return False, None

        if psycopg is None:
            return False, "psycopg is not installed; EXPLAIN validation skipped."

        if not all(
            [
                settings.postgres_host,
                settings.postgres_db,
                settings.postgres_user,
                settings.postgres_password,
            ]
        ):
            return False, "Postgres connection settings are incomplete; EXPLAIN validation skipped."

        try:
            conn_kwargs = {
                "host": settings.postgres_host,
                "port": settings.postgres_port,
                "dbname": settings.postgres_db,
                "user": settings.postgres_user,
                "password": settings.postgres_password,
                "sslmode": settings.postgres_sslmode,
                "connect_timeout": int(settings.sql_explain_timeout_sec),
            }
            with psycopg.connect(**conn_kwargs) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"EXPLAIN {sql_text}")
                    cur.fetchall()
            return True, None
        except Exception as exc:  # pragma: no cover - depends on external DB
            return True, str(exc)

    async def validate_sql(self, answer: str, schema_docs: list[dict]) -> SQLValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        sql_text = self.extract_sql(answer)
        allowed_tables = self._allowed_tables_from_docs(schema_docs)
        allowed_columns = self._allowed_columns_from_docs(schema_docs)
        table_to_columns = self._table_metadata_from_docs(schema_docs)
        used_tables, alias_to_table = self.extract_table_refs(sql_text)
        used_columns = self.extract_used_columns(sql_text, alias_to_table)

        if not sql_text:
            errors.append("No SQL query was found in the LLM answer.")
            return SQLValidationResult(
                False,
                errors,
                warnings,
                None,
                [],
                allowed_tables,
                [],
                allowed_columns,
            )

        first_token = sql_text.lstrip().split(maxsplit=1)[0].lower() if sql_text.strip() else ""
        if first_token not in {"select", "with"}:
            errors.append("SQL must start with SELECT or WITH.")

        forbidden = self._has_forbidden_keyword(sql_text)
        if forbidden:
            errors.append(f"Forbidden SQL keyword used: {forbidden.upper()}.")

        if not used_tables:
            warnings.append("No FROM/JOIN table reference was detected.")

        allowed_unqualified = {table.split(".")[-1] for table in allowed_tables}
        allowed_all = set(allowed_tables) | allowed_unqualified

        unknown_tables = [table for table in used_tables if table not in allowed_all]
        if unknown_tables:
            errors.append(
                "SQL references tables that are not present in retrieved schema context: "
                + ", ".join(unknown_tables)
            )

        invalid_columns: list[str] = []
        for used_column in used_columns:
            table, column = used_column.rsplit(".", 1)
            columns = table_to_columns.get(table) or table_to_columns.get(table.split(".")[-1])
            if columns is not None and column not in columns:
                invalid_columns.append(used_column)

        if invalid_columns:
            errors.append(
                "SQL references columns that are not present in retrieved schema context: "
                + ", ".join(invalid_columns)
            )

        explain_checked = False
        explain_error = None
        if not errors and settings.sql_enable_explain_validation:
            explain_checked, explain_error = await self._postgres_explain(sql_text)
            if explain_error and "skipped" not in explain_error.lower():
                errors.append(f"Postgres EXPLAIN failed: {explain_error}")

        return SQLValidationResult(
            is_valid=not errors,
            errors=errors,
            warnings=warnings,
            sql=sql_text,
            used_tables=used_tables,
            allowed_tables=allowed_tables,
            used_columns=used_columns,
            allowed_columns=allowed_columns,
            explain_checked=explain_checked,
            explain_error=explain_error,
        )

    async def _generate_once(
        self, question: str, schema_docs: list[dict], max_new_tokens: int
    ) -> str:
        prompt = SQLPromptBuilder.build_generate_prompt(
            question=question,
            schema_docs=schema_docs,
            dialect=settings.sql_dialect,
            max_context_chars=settings.llm_max_context_chars,
        )
        return await self.llm_service.generate(prompt=prompt, max_new_tokens=max_new_tokens)

    async def _repair_once(
        self,
        *,
        question: str,
        schema_docs: list[dict],
        previous_answer: str,
        validation_errors: list[str],
        max_new_tokens: int,
    ) -> str:
        prompt = SQLPromptBuilder.build_repair_prompt(
            question=question,
            schema_docs=schema_docs,
            previous_answer=previous_answer,
            validation_errors=validation_errors,
            dialect=settings.sql_dialect,
            max_context_chars=settings.llm_max_context_chars,
        )
        return await self.llm_service.generate(prompt=prompt, max_new_tokens=max_new_tokens)

    async def ask(
        self,
        *,
        question: str,
        top_k: int | None = None,
        max_new_tokens: int | None = None,
    ) -> dict:
        effective_top_k = top_k or settings.sql_top_k
        effective_max_tokens = max_new_tokens or settings.max_new_tokens

        retrieved = self.retriever.search(
            question,
            top_k=effective_top_k,
            source_filter=[settings.sql_schema_source],
        )

        if not retrieved:
            validation = SQLValidationResult(
                is_valid=False,
                errors=["No database schema documents were retrieved."],
                warnings=[],
                sql=None,
                used_tables=[],
                allowed_tables=[],
                used_columns=[],
                allowed_columns=[],
            )
            return {
                "question": question,
                "answer": "No database schema documents were found for SQL generation.",
                "mode": "sql",
                "confidence": self._build_confidence([], effective_top_k, validation),
                "retrieved": [],
            }

        if not self.llm_service.is_configured():
            answer = "LLM is not configured. SQL generation is unavailable."
            validation = SQLValidationResult(
                is_valid=False,
                errors=["LLM is not configured."],
                warnings=[],
                sql=None,
                used_tables=[],
                allowed_tables=self._allowed_tables_from_docs(retrieved),
                used_columns=[],
                allowed_columns=self._allowed_columns_from_docs(retrieved),
            )
        else:
            answer = await self._generate_once(question, retrieved, effective_max_tokens)
            validation = await self.validate_sql(answer, retrieved)

            attempts = 0
            while (
                not validation.is_valid
                and attempts < settings.sql_max_repair_attempts
                and self.llm_service.is_configured()
            ):
                attempts += 1
                answer = await self._repair_once(
                    question=question,
                    schema_docs=retrieved,
                    previous_answer=answer,
                    validation_errors=validation.errors,
                    max_new_tokens=effective_max_tokens,
                )
                validation = await self.validate_sql(answer, retrieved)
                validation.warnings.append(f"Repair attempts used: {attempts}")

        confidence = self._build_confidence(retrieved, effective_top_k, validation)

        return {
            "question": question,
            "answer": answer,
            "mode": "sql",
            "confidence": confidence,
            "retrieved": self._compact_retrieved(retrieved),
        }
