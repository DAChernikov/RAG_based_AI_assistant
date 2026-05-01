import sys
from pathlib import Path

import joblib
import numpy as np
import sentence_transformers
from sentence_transformers import SentenceTransformer

# Compatibility for artifacts saved with newer sentence-transformers versions.
try:
    import sentence_transformers.base  # type: ignore  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.modules["sentence_transformers.base"] = sentence_transformers


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
        source_boosts = source_boosts or {}
        source_filter_set = set(source_filter or [])

        if preferred_sources or source_boosts:
            for idx, doc in enumerate(self.corpus):
                source = doc.get("source", "unknown")
                if source in preferred_sources:
                    scores[idx] += 0.08
                scores[idx] += source_boosts.get(source, 0.0)

        if source_filter_set:
            candidate_idx = [
                idx
                for idx, doc in enumerate(self.corpus)
                if doc.get("source", "unknown") in source_filter_set
            ]
            ranked_idx = sorted(candidate_idx, key=lambda idx: scores[idx], reverse=True)
        else:
            ranked_idx = np.argsort(scores)[::-1]

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
                    "score": float(scores[idx]),
                    "raw_score": float(raw_scores[idx]),
                }
            )

            if len(results) >= top_k:
                break

        return results
