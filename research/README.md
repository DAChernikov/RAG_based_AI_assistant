# Reproducible retrieval research

Research is isolated from production runtime. Install it explicitly:

```bash
poetry install --with worker,research,dev
```

The notebooks are thin orchestration layers over `research/src`. They never modify production
artifacts, download models on import, or upload results. Heavy model loading and training are
opt-in notebook cells. Use fixed seeds and write result manifests under ignored
`research/results/`.

- `00_retrieval_baseline.ipynb`: deterministic lexical smoke baseline.
- `01_embedding_model_comparison.ipynb`: opt-in comparison of the current embedding model and
  `BAAI/bge-m3`; no winner is declared without measurements.
- `02_retriever_finetuning.ipynb`: opt-in train/validation/test workflow with
  `RUN_TRAINING = False` by default.

MPS is preferred when available, with CPU fallback. CI tests only reusable metric/dataset
modules and notebook structure; it does not execute heavy cells.
