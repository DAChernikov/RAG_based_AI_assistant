class SQLService:
    def ask(self, question: str) -> dict:
        return {
            "answer": (
                "SQL mode is not implemented yet in this MVP. " f"Received question: {question}"
            ),
            "mode": "sql",
            "confidence": None,
            "retrieved": [],
        }
