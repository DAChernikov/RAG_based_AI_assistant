from pathlib import Path

import joblib
import numpy as np
from sentence_transformers import SentenceTransformer


class RetrieverLoader:
    def __init__(self, artifacts_dir: str = "artifacts"):
        self.artifacts_dir = Path(artifacts_dir)
        self.model = None
        self.corpus = None
        self.corpus_emb = None

    def load(self):
        self.model = SentenceTransformer(str(self.artifacts_dir / "retriever_model"))
        self.corpus_emb = np.load(self.artifacts_dir / "corpus_emb.npy")
        self.corpus = joblib.load(self.artifacts_dir / "corpus.joblib")

        if self.corpus_emb.ndim != 2:
            raise ValueError(f"Expected 2D embeddings, got shape={self.corpus_emb.shape}")

        if len(self.corpus) != self.corpus_emb.shape[0]:
            raise ValueError(
                f"Corpus size mismatch: len(corpus)={len(self.corpus)} "
                f"vs embeddings={self.corpus_emb.shape[0]}"
            )

        return self

    def encode(self, text: str):
        return self.model.encode([text], normalize_embeddings=True, show_progress_bar=False)[0]

    @staticmethod
    def _dedupe_key(doc: dict) -> str:
        doc_id = (doc.get("doc_id") or "").strip().lower()
        title = (doc.get("title") or "").strip().lower()
        source = (doc.get("source") or "").strip().lower()
        text = ((doc.get("text") or "")[:160]).strip().lower()
        return f"{source}|{doc_id}|{title}|{text}"

    def search(
        self,
        query: str,
        top_k: int = 5,
        preferred_sources: list[str] | None = None,
        source_boosts: dict[str, float] | None = None,
        source_filter: list[str] | None = None,
    ) -> list[dict]:
        query_emb = self.encode(query)
        raw_scores = self.corpus_emb @ query_emb
        scores = raw_scores.copy()

        preferred_sources = set(preferred_sources or [])
        source_filter_set = set(source_filter or [])
        source_boosts = source_boosts or {}

        candidate_idx: list[int] = []
        for idx, doc in enumerate(self.corpus):
            source = doc.get("source", "unknown")
            if source_filter_set and source not in source_filter_set:
                continue

            candidate_idx.append(idx)

            if source in preferred_sources:
                scores[idx] += 0.08
            scores[idx] += source_boosts.get(source, 0.0)

        if not candidate_idx:
            return []

        ranked_idx = sorted(candidate_idx, key=lambda idx: scores[idx], reverse=True)

        results: list[dict] = []
        seen: set[str] = set()

        for idx in ranked_idx:
            doc = self.corpus[idx]
            key = self._dedupe_key(doc)
            if key in seen:
                continue
            seen.add(key)

            results.append(
                {
                    "doc_id": doc.get("doc_id", str(idx)),
                    "source": doc.get("source", "unknown"),
                    "title": doc.get("title"),
                    "text": doc.get("text"),
                    "metadata": doc.get("metadata"),
                    "score": float(scores[idx]),
                    "raw_score": float(raw_scores[idx]),
                }
            )

            if len(results) >= top_k:
                break

        return results
