from __future__ import annotations

import math
from collections.abc import Iterable


def _relevant_set(labels: dict[str, float]) -> set[str]:
    return {doc_id for doc_id, relevance in labels.items() if relevance > 0}


def recall_at_k(ranked: list[str], labels: dict[str, float], k: int) -> float:
    relevant = _relevant_set(labels)
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def reciprocal_rank_at_k(ranked: list[str], labels: dict[str, float], k: int) -> float:
    relevant = _relevant_set(labels)
    for rank, doc_id in enumerate(ranked[:k], start=1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked: list[str], labels: dict[str, float], k: int) -> float:
    gains = [labels.get(doc_id, 0.0) for doc_id in ranked[:k]]
    dcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    ideal = sorted(labels.values(), reverse=True)[:k]
    idcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(ideal, 1))
    return dcg / idcg if idcg else 0.0


def percentile(values: Iterable[float], percentile_value: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(percentile_value * len(ordered)) - 1))
    return float(ordered[index])


def aggregate_metrics(rows: list[dict], k: int) -> dict[str, float]:
    if not rows:
        return {
            f"recall@{k}": 0.0,
            f"mrr@{k}": 0.0,
            f"ndcg@{k}": 0.0,
            "latency_p50_ms": 0.0,
            "latency_p95_ms": 0.0,
        }
    return {
        f"recall@{k}": sum(recall_at_k(row["ranked"], row["labels"], k) for row in rows)
        / len(rows),
        f"mrr@{k}": sum(reciprocal_rank_at_k(row["ranked"], row["labels"], k) for row in rows)
        / len(rows),
        f"ndcg@{k}": sum(ndcg_at_k(row["ranked"], row["labels"], k) for row in rows) / len(rows),
        "latency_p50_ms": percentile((row["latency_ms"] for row in rows), 0.50),
        "latency_p95_ms": percentile((row["latency_ms"] for row in rows), 0.95),
    }
