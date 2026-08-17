from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.retrieval.contracts import RetrievalFilters


class RoutePlan(BaseModel):
    contract_version: Literal["1.0"] = "1.0"
    knowledge_base_id: uuid.UUID
    intents: list[str]
    retrieval_targets: list[Literal["documentation", "code", "database_schema"]]
    source_filters: RetrievalFilters = Field(default_factory=RetrievalFilters)
    requires_sql: bool = False
    confidence: float = Field(ge=0, le=1)
    reasons: list[str]


class RouterService:
    SQL_HINTS = {
        "sql",
        "select",
        "query",
        "table",
        "schema",
        "join",
        "revenue",
        "orders",
        "aggregation",
    }
    CODE_HINTS = {
        "python",
        "code",
        "function",
        "class",
        "repository",
        "api",
        "implementation",
        "пример кода",
    }

    def plan(
        self,
        question: str,
        knowledge_base_id: uuid.UUID,
        requested_mode: str | None = None,
    ) -> RoutePlan:
        normalized = re.sub(r"[^\w]+", " ", question.casefold())
        tokens = set(normalized.split())
        sql = requested_mode == "sql" or bool(tokens & self.SQL_HINTS)
        code = requested_mode == "rag_code" or bool(tokens & self.CODE_HINTS)
        targets: list[Literal["documentation", "code", "database_schema"]] = ["documentation"]
        intents = ["answer"]
        reasons = ["documentation is the safe default retrieval target"]
        if code:
            targets.append("code")
            intents.append("code_explanation")
            reasons.append("code-related terms detected")
        if sql:
            targets.append("database_schema")
            intents.append("sql_generation")
            reasons.append("SQL or data-analysis terms detected")
        return RoutePlan(
            knowledge_base_id=knowledge_base_id,
            intents=list(dict.fromkeys(intents)),
            retrieval_targets=list(dict.fromkeys(targets)),
            requires_sql=sql,
            confidence=0.9 if requested_mode else (0.82 if sql or code else 0.65),
            reasons=reasons,
        )
