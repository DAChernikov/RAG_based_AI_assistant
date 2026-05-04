import re


class RouterService:
    SQL_HINTS = (
        "sql",
        "query",
        "select ",
        " join ",
        " group by",
        "order by",
        "table ",
        "schema ",
        "aggregation",
        "write sql",
        "give me sql",
        "give me a sql",
        "sql command",
        "revenue by",
        "sales by",
        "orders by",
        "refund rate",
        "refund amount",
        "campaign revenue",
        "marketing channel",
        "store type",
        "sales channel",
        "customer segment",
        "product category",
        "quantity sold",
        "support tickets by",
        "purchase events by",
        "convert daily order revenue",
        "eur to usd",
    )

    CODE_HINTS = (
        "python",
        "dict",
        "json",
        "list",
        "function",
        "code",
        "snippet",
        "example",
        "nested value",
        "missing keys",
        "dict.get",
        "pandas dataframe",
    )

    @staticmethod
    def _normalize(question: str) -> str:
        return re.sub(r"\s+", " ", question.lower()).strip()

    def route(self, question: str, requested_mode: str | None = None) -> str:
        if requested_mode == "sql":
            return "sql"

        if requested_mode in {"rag_docs", "rag_code"}:
            return requested_mode

        q = self._normalize(question)

        if any(token in q for token in self.SQL_HINTS):
            return "sql"

        if any(token in q for token in self.CODE_HINTS):
            return "rag_code"

        return "rag_docs"
