"""本人確定の対象外2件を管理者が確認済みにする。除外解除は扱わない。"""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from src.api.routes.sync_review import verify_review_token
from src.sync_operations.exclusions import list_exclusions, acknowledge_exclusion
from src.sync_review.domain import ReviewConflict, ReviewForbidden

router = APIRouter(dependencies=[Depends(verify_review_token)])


class Request(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    action: Literal['list', 'acknowledge']
    external_id: str = Field(default='', max_length=100)


@router.post('/api/sync-exclusions')
def operation(body: Request):
    try:
        if body.action == 'list': return list_exclusions(body.actor_id)
        return acknowledge_exclusion(body.actor_id, body.external_id)
    except ReviewForbidden as exc:
        raise HTTPException(403, str(exc)) from None
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from None
