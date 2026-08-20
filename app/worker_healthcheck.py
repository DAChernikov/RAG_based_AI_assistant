from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from redis.asyncio import Redis

from app.api.config import settings


def _heartbeat_path(role: str) -> Path:
    return Path(settings.worker_health_dir) / f"rag-{role}.heartbeat"


def record_local_heartbeat(role: str) -> None:
    _heartbeat_path(role).write_text(str(time.time()), encoding="ascii")


def _check_local_heartbeat(role: str) -> None:
    path = _heartbeat_path(role)
    if not path.exists() or time.time() - path.stat().st_mtime > settings.worker_stale_after_sec:
        raise RuntimeError("Worker event-loop heartbeat is stale.")


def _identity(role: str) -> tuple[str | None, str | None]:
    values = {
        "inference": (settings.inference_heartbeat_prefix, settings.worker_id),
        "ingestion": (settings.ingestion_heartbeat_prefix, settings.ingestion_worker_id),
        "indexing": (settings.indexing_heartbeat_prefix, settings.indexing_worker_id),
        "scheduler": (None, None),
        "bot": (None, None),
    }
    if role not in values:
        raise ValueError("Unknown worker role.")
    return values[role]


async def check(role: str, readiness: bool) -> None:
    _identity(role)
    _check_local_heartbeat(role)
    if not readiness:
        return
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await asyncio.wait_for(redis.ping(), timeout=2)
        prefix, worker_id = _identity(role)
        if readiness and prefix and not await redis.exists(f"{prefix}:{worker_id}"):
            raise RuntimeError("Worker heartbeat is not registered.")
    finally:
        await redis.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker liveness/readiness probe")
    parser.add_argument("role", choices=("inference", "ingestion", "indexing", "scheduler", "bot"))
    parser.add_argument("--readiness", action="store_true")
    args = parser.parse_args()
    asyncio.run(check(args.role, args.readiness))


if __name__ == "__main__":
    main()
