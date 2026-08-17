# Reproducible product evaluation

The committed dataset is synthetic and contains no secrets. The deterministic CI contour compares
dense-like token cosine, sparse token overlap and RRF hybrid fusion with a fixed seed:

```bash
poetry run python -m research.src.product_evaluation
```

The report contains Recall@K, MRR, nDCG, latency and routing contracts. Release gates require
hybrid Recall@5 >= 0.70 and nDCG@5 >= 0.60. Real BGE-M3, Ollama latency/memory and optional
reranker experiments are opt-in because they require preloaded self-hosted model endpoints:

```bash
RUN_REAL_MODEL_EVAL=1 MODEL_API_BASE_URL=http://127.0.0.1:11434/v1 \
EMBEDDING_API_BASE_URL=http://127.0.0.1:8001/v1 make evaluation-real
```

Each real-model report must record endpoint model/version, prompt version, hardware, peak memory,
Recall@K, MRR, nDCG, citation accuracy, SQL validity, groundedness and latency percentiles. It must
not include prompts, retrieved private content or credentials.
