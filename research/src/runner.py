from __future__ import annotations

import time

from research.src.evaluation import aggregate_metrics


def evaluate_retriever(retriever, queries: list[dict], *, k: int = 5) -> tuple[dict, list[dict]]:
    rows = []
    for query in queries:
        started = time.perf_counter()
        results = retriever.search(query["query"], top_k=k)
        latency_ms = (time.perf_counter() - started) * 1000
        rows.append(
            {
                "ranked": [result["doc_id"] for result in results],
                "labels": query["labels"],
                "latency_ms": latency_ms,
            }
        )
    return aggregate_metrics(rows, k=k), rows
