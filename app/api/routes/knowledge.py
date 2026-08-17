from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_runtime_state, require_scope
from app.api.routes.catalog import _knowledge_base_response
from app.auth.security import Principal

router = APIRouter(prefix="/v1", tags=["knowledge"])


@router.get("/knowledge-bases")
async def available_knowledge_bases(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require_scope("inference:read")),
    runtime=Depends(get_runtime_state),
):
    rows = await runtime["catalog_service"].call(
        runtime["catalog_repository"].list_knowledge_bases, principal.tenant_id
    )
    return [
        _knowledge_base_response(row) for row in rows[offset : offset + limit] if row.is_enabled
    ]
