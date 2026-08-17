from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel


class IndexingJobContract(BaseModel):
    contract_version: Literal["1.0"] = "1.0"
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    index_version_id: uuid.UUID
    correlation_id: uuid.UUID
    auto_activate: bool = False
