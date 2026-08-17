# Evaluation report template

- Release/commit:
- Dataset/checksum and fixed seed:
- Hardware/model endpoints and versions:
- Dense / sparse / hybrid Recall@K, MRR, nDCG:
- Routing multi-label precision/recall:
- Groundedness and citation accuracy:
- SQL AST/schema validity and safe EXPLAIN rate:
- p50/p95 latency and peak memory:
- Failure/retry/reclaim observations:
- Threshold result and regressions:
- Reviewer/date:

Run the committed deterministic contour with `make evaluation`. Real BGE-M3/Qwen measurement is opt-in and its endpoint/hardware metadata must be recorded without credentials.
