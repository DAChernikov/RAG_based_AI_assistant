from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import psycopg
import sqlglot
from sqlglot import exp

from app.api.config import settings
from app.api.services.sql_prompt_builder import SQLPromptBuilder
from app.concurrency import api_blocking_io
from app.operations.runtime_registry import ResolvedPrompt, render_prompt


@dataclass
class SQLValidationResult:
    is_valid: bool
    sql: str | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    used_tables: list[str] = field(default_factory=list)
    used_columns: list[str] = field(default_factory=list)
    explain_valid: bool | None = None
    explain_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "sql": self.sql,
            "errors": self.errors,
            "warnings": self.warnings,
            "used_tables": self.used_tables,
            "used_columns": self.used_columns,
            "explain_valid": self.explain_valid,
            "explain_error": self.explain_error,
            "validator": "sqlglot/postgres/1.0",
        }


ExplainParameters = Callable[[], Awaitable[dict[str, Any]]]


class SQLService:
    """PostgreSQL-only, read-only SQL generation and AST/schema validation."""

    MAX_AST_NODES = 500
    DEFAULT_ROW_LIMIT = 500
    FORBIDDEN_NODES = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Create,
        exp.Drop,
        exp.Alter,
        exp.Command,
        exp.Copy,
        exp.Transaction,
        exp.Merge,
    )

    def __init__(
        self,
        retriever,
        llm_service,
        explain_parameters: ExplainParameters | None = None,
        prompt_template: ResolvedPrompt | None = None,
    ):
        self.retriever = retriever
        self.llm_service = llm_service
        self.explain_parameters = explain_parameters
        self.prompt_template = prompt_template

    @staticmethod
    def extract_sql(answer: str) -> str | None:
        fenced = re.search(r"```(?:sql)?\s*(.*?)```", answer, re.I | re.S)
        if fenced:
            return fenced.group(1).strip().rstrip(";")
        marker = re.search(r"(?:^|\n)SQL\s*:\s*(.*)", answer, re.I | re.S)
        if marker:
            return marker.group(1).strip().rstrip(";")
        candidate = answer.strip().rstrip(";")
        return candidate if candidate else None

    @staticmethod
    def _schema(docs: list[dict]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for doc in docs:
            metadata = doc.get("metadata") or {}
            schema = metadata.get("schema")
            table = metadata.get("table") or metadata.get("relation")
            if not table:
                title = doc.get("title", "")
                if "." in title:
                    schema, table = title.rsplit(".", 1)
            if not table:
                continue
            key = f"{schema}.{table}" if schema else table
            columns = set(metadata.get("columns") or [])
            if not columns:
                columns.update(
                    re.findall(r"^-\s*([A-Za-z_][\w$]*)\s*\(", doc.get("text", ""), re.M)
                )
                try:
                    import json

                    payload = json.loads(doc.get("text", ""))
                    if isinstance(payload, list):
                        columns.update(
                            item.get("column_name")
                            for item in payload
                            if isinstance(item, dict) and item.get("column_name")
                        )
                except (ValueError, TypeError):
                    pass
            result[key.casefold()] = {column.casefold() for column in columns}
        return result

    async def validate_sql(self, answer: str, schema_docs: list[dict]) -> SQLValidationResult:
        sql = self.extract_sql(answer)
        if not sql:
            return SQLValidationResult(False, None, ["SQL statement was not found."])
        try:
            statements = sqlglot.parse(sql, read="postgres")
        except sqlglot.errors.ParseError:
            return SQLValidationResult(False, sql, ["SQL syntax is invalid."])
        if len(statements) != 1:
            return SQLValidationResult(False, sql, ["Exactly one SQL statement is allowed."])
        tree = statements[0]
        errors = []
        if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            errors.append("Only a read-only SELECT or WITH ... SELECT is allowed.")
        if any(tree.find(node) is not None for node in self.FORBIDDEN_NODES):
            errors.append("Forbidden write, DDL, COPY, CALL, or transaction operation detected.")
        if sum(1 for _ in tree.walk()) > self.MAX_AST_NODES:
            errors.append("SQL query exceeds the maximum AST complexity.")

        schema = self._schema(schema_docs)
        cte_names = {cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE)}
        aliases: dict[str, str] = {}
        used_tables = []
        for table in tree.find_all(exp.Table):
            if table.name.casefold() in cte_names:
                continue
            qualified = f"{table.db}.{table.name}" if table.db else table.name
            key = qualified.casefold()
            matches = [name for name in schema if name == key or name.endswith(f".{key}")]
            if len(matches) != 1:
                errors.append(f"Table is not present in the active schema index: {qualified}")
                continue
            canonical = matches[0]
            aliases[(table.alias_or_name or table.name).casefold()] = canonical
            used_tables.append(canonical)

        used_columns = []
        for column in tree.find_all(exp.Column):
            name = column.name.casefold()
            if column.table:
                table_key = aliases.get(column.table.casefold())
                if table_key and schema.get(table_key) and name not in schema[table_key]:
                    errors.append(f"Column is not present in indexed schema: {column.sql()}")
            elif schema and not any(name in columns for columns in schema.values()):
                # Select aliases and function outputs are allowed when they are defined in-query.
                if not any(alias.alias.casefold() == name for alias in tree.find_all(exp.Alias)):
                    errors.append(f"Column is not present in indexed schema: {column.name}")
            used_columns.append(column.sql())

        if tree.args.get("limit") is None and isinstance(
            tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)
        ):
            tree = tree.limit(self.DEFAULT_ROW_LIMIT)
            sql = tree.sql(dialect="postgres")
        explain_valid = None
        explain_error = None
        if not errors and settings.sql_enable_explain_validation:
            if self.explain_parameters is None:
                errors.append("Safe EXPLAIN credential context is not configured.")
            else:
                explain_valid, explain_error = await self._explain(sql)
                if not explain_valid:
                    errors.append("PostgreSQL rejected the query during safe EXPLAIN.")
        return SQLValidationResult(
            not errors,
            sql,
            errors,
            used_tables=sorted(set(used_tables)),
            used_columns=sorted(set(used_columns)),
            explain_valid=explain_valid,
            explain_error=explain_error,
        )

    async def _explain(self, sql: str) -> tuple[bool, str | None]:
        parameters = await self.explain_parameters()

        def execute():
            options = dict(parameters)
            options["options"] = (
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={int(settings.sql_explain_timeout_sec * 1000)} "
                "-c lock_timeout=1000"
            )
            with psycopg.connect(**options) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(f"EXPLAIN (FORMAT JSON) {sql}")

        try:
            # `sql` is emitted from a single, read-only SQLGlot AST validated above.
            await api_blocking_io.call(execute)
            return True, None
        except psycopg.Error:
            return False, "Database rejected SQL during read-only EXPLAIN."

    async def ask(
        self,
        *,
        question: str,
        top_k: int = 10,
        max_new_tokens: int = 900,
        tenant_id=None,
        knowledge_base_id=None,
        retrieved: list[dict] | None = None,
    ) -> dict:
        docs = retrieved
        if docs is None:
            docs = self.retriever.search(
                question, top_k=top_k, source_filter=[settings.sql_schema_source]
            )
            if hasattr(docs, "__await__"):
                docs = await docs
        if self.prompt_template is not None:
            prompt = render_prompt(
                self.prompt_template,
                question=question,
                context=SQLPromptBuilder._format_schema_context(
                    docs, settings.model_max_context_chars
                ),
                mode="sql",
            )
        else:
            prompt = SQLPromptBuilder.build_generate_prompt(
                question=question,
                schema_docs=docs,
                dialect="postgres",
                max_context_chars=settings.model_max_context_chars,
            )
        answer = await self.llm_service.generate(
            prompt=prompt,
            max_new_tokens=max_new_tokens,
            temperature=settings.sql_temperature,
        )
        validation = await self.validate_sql(answer, docs)
        for _attempt in range(settings.sql_max_repair_attempts):
            if validation.is_valid:
                break
            repair_prompt = (
                f"Repair this PostgreSQL query using only the supplied schema. "
                f"Validation errors: {validation.errors}.\n{prompt}\nPrevious answer:\n{answer}"
            )
            answer = await self.llm_service.generate(
                prompt=repair_prompt,
                max_new_tokens=max_new_tokens,
                temperature=0.0,
            )
            validation = await self.validate_sql(answer, docs)
        final_sql = validation.sql or self.extract_sql(answer) or ""
        return {
            "answer": final_sql,
            "mode": "sql",
            "confidence": {"validation": validation.to_dict()},
            "retrieved": docs,
            "prompt_version": (
                f"{self.prompt_template.name}/{self.prompt_template.version}"
                if self.prompt_template
                else "sql-generation/legacy"
            ),
        }
