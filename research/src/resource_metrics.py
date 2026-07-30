from __future__ import annotations

import json
import platform
import random
from datetime import UTC, datetime
from pathlib import Path


def set_seed(seed: int = 42) -> None:
    random.seed(seed)


def build_manifest(
    *,
    experiment: str,
    seed: int,
    models: list[str],
    metrics: dict,
    peak_rss_bytes: int,
    index_size_bytes: int,
) -> dict:
    return {
        "experiment": experiment,
        "created_at": datetime.now(UTC).isoformat(),
        "seed": seed,
        "models": models,
        "metrics": metrics,
        "peak_rss_bytes": peak_rss_bytes,
        "index_size_bytes": index_size_bytes,
        "python": platform.python_version(),
    }


def save_manifest(path: str | Path, manifest: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
