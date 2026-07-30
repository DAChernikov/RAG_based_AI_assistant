from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

logger = logging.getLogger("rag.events")
COUNTERS: Counter[str] = Counter()


def metric(name: str, amount: int = 1) -> None:
    COUNTERS[name] += amount


def log_event(event_type: str, **fields: Any) -> None:
    allowed = {
        "correlation_id",
        "job_id",
        "conversation_id",
        "worker_id",
        "duration_ms",
        "retry_number",
        "final_status",
        "error_code",
    }
    payload = {"event_type": event_type}
    payload.update({key: value for key, value in fields.items() if key in allowed})
    logger.info(json.dumps(payload, default=str, separators=(",", ":")))
