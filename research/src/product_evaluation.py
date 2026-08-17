from __future__ import annotations

import argparse
import json
import math
import re
import time
from collections import Counter
from pathlib import Path

from app.api.services.router_service import RouterService
from research.src.datasets import load_jsonl, split_queries
from research.src.evaluation import aggregate_metrics


def tokens(text: str) -> Counter[str]:
    return Counter(re.findall(r"[\w.]+", text.casefold()))


def cosine(left: Counter[str], right: Counter[str]) -> float:
    numerator = sum(value * right[key] for key, value in left.items())
    denominator = math.sqrt(sum(value * value for value in left.values())) * math.sqrt(
        sum(value * value for value in right.values())
    )
    return numerator / denominator if denominator else 0.0


def rank(documents: list[dict], query: str, mode: str) -> list[str]:
    query_tokens = tokens(query)
    dense = sorted(
        documents,
        key=lambda row: (-cosine(query_tokens, tokens(row["text"])), row["id"]),
    )
    sparse = sorted(
        documents,
        key=lambda row: (-sum((tokens(row["text"]) & query_tokens).values()), row["id"]),
    )
    if mode == "dense":
        return [row["id"] for row in dense]
    if mode == "sparse":
        return [row["id"] for row in sparse]
    scores: Counter[str] = Counter()
    for rows in (dense, sparse):
        for position, row in enumerate(rows, 1):
            scores[row["id"]] += 1 / (60 + position)
    return sorted(scores, key=lambda item: (-scores[item], item))


def run(dataset: Path, seed: int = 42, k: int = 5) -> dict:
    records = load_jsonl(dataset)
    documents, queries = split_queries(records)
    report = {"seed": seed, "dataset": dataset.name, "retrieval": {}}
    for mode in ("dense", "sparse", "hybrid"):
        rows = []
        for query in queries:
            started = time.perf_counter()
            ranked = rank(documents, query["query"], mode)
            rows.append(
                {
                    "ranked": ranked,
                    "labels": query["labels"],
                    "latency_ms": (time.perf_counter() - started) * 1000,
                }
            )
        report["retrieval"][mode] = aggregate_metrics(rows, k)
    route_checks = []
    import uuid

    for query in queries:
        plan = RouterService().plan(query["query"], uuid.UUID(int=1))
        route_checks.append(
            {
                "query_id": query["id"],
                "targets": plan.retrieval_targets,
                "requires_sql": plan.requires_sql,
            }
        )
    report["routing"] = route_checks
    report["quality_gate"] = {
        "hybrid_recall_at_5": report["retrieval"]["hybrid"]["recall@5"] >= 0.70,
        "hybrid_ndcg_at_5": report["retrieval"]["hybrid"]["ndcg@5"] >= 0.60,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", type=Path, default=Path("research/datasets/retrieval_smoke.jsonl")
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.dataset)
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if not all(report["quality_gate"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
