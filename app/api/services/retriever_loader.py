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

    def search(self, query: str, top_k: int = 5):
        query_emb = self.encode(query)
        scores = self.corpus_emb @ query_emb
        top_idx = np.argsort(scores)[::-1][:top_k]

        results = []
        for idx in top_idx:
            doc = self.corpus[idx]
            results.append(
                {
                    "doc_id": doc.get("doc_id", str(idx)),
                    "source": doc.get("source", "unknown"),
                    "title": doc.get("title"),
                    "text": doc.get("text"),
                    "score": float(scores[idx]),
                }
            )

        return results
