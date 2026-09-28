"""確認した保留を、本人の管理権限と表示版を照合して再開する。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.dependencies import wiring_dependency
from src.api.routes.sync_review import verify_review_token
from src.sync_engine.record_sync_lock import RecordSyncBusy
from src.sync_operations.product_holds import list_product_holds, resume_product_hold
from src.sync_review.domain import ReviewConflict, ReviewForbidden

router = APIRouter(dependencies=[Depends(verify_review_token)])


class ListRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    offset: int = Field(default=0, ge=0, le=500000)


class ResumeRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    expected_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


@router.post('/api/sync-operations/product-holds/list')
def list_holds(body: ListRequest):
    try:
        return list_product_holds(body.actor_id, offset=body.offset)
    except ReviewForbidden as exc:
        raise HTTPException(403, str(exc)) from None


@router.post('/api/sync-operations/product-holds/resume')
def resume(body: ResumeRequest, wiring=Depends(wiring_dependency)):
    try:
        return resume_product_hold(actor_id=body.actor_id, project_id=body.project_id,
            expected_hash=body.expected_hash, store=wiring.dispatcher._dispatcher._store)
    except ReviewForbidden as exc:
        raise HTTPException(403, str(exc)) from None
    except (ReviewConflict, RecordSyncBusy) as exc:
        raise HTTPException(409, str(exc)) from None
