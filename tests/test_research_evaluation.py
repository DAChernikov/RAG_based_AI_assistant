from research.src.evaluation import aggregate_metrics, ndcg_at_k, recall_at_k, reciprocal_rank_at_k


def test_retrieval_metrics():
    labels = {"a": 2, "b": 1}
    ranked = ["x", "a", "b"]

    assert recall_at_k(ranked, labels, 2) == 0.5
    assert reciprocal_rank_at_k(ranked, labels, 3) == 0.5
    assert 0 < ndcg_at_k(ranked, labels, 3) < 1


def test_aggregate_metrics_includes_latency_percentiles():
    rows = [
        {"ranked": ["a"], "labels": {"a": 1}, "latency_ms": 10},
        {"ranked": ["b"], "labels": {"b": 1}, "latency_ms": 30},
    ]

    metrics = aggregate_metrics(rows, k=1)

    assert metrics["recall@1"] == 1
    assert metrics["mrr@1"] == 1
    assert metrics["ndcg@1"] == 1
    assert metrics["latency_p50_ms"] == 10
    assert metrics["latency_p95_ms"] == 30
