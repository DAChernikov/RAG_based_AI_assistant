from __future__ import annotations

import json
from pathlib import Path


def load_jsonl(path: str | Path) -> list[dict]:
    records: list[dict] = []
    with Path(path).open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if "id" not in record:
                raise ValueError(f"Record on line {line_number} has no id.")
            records.append(record)
    return records


def split_queries(records: list[dict]) -> tuple[list[dict], list[dict]]:
    documents = [record for record in records if record.get("record_type") == "document"]
    queries = [record for record in records if record.get("record_type") == "query"]
    return documents, queries
