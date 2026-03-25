class RouterService:
    SQL_HINTS = (
        "sql",
        "query",
        "select",
        "join",
        "report",
        "table",
        "schema",
        "aggregation",
        "group by",
    )

    def route(self, question: str, requested_mode: str | None = None) -> str:
        if requested_mode in {"rag", "sql"}:
            return requested_mode

        q = question.lower()
        if any(token in q for token in self.SQL_HINTS):
            return "sql"
        return "rag"
