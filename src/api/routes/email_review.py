"""担当本人のメール照合。利用者IDは認証済みの管理画面から受け取る。"""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from src.api.routes.sync_review import verify_review_token
from src.email_review.domain import Conflict, Forbidden
from src.email_review.repository import Repository
from src.email_review.service import Service, enabled
from src.slack_crm.storage import search_projects

router = APIRouter(dependencies=[Depends(verify_review_token)])


class Decision(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=0)
    action: Literal["confirm", "hold", "exclude", "resume", "reconcile"]
    company_id: UUID | None = None
    contact_id: UUID | None = None
    target_id: UUID | None = None
    project_id: UUID | None = None


@router.get('/api/email-review')
def listing(actor_id: str, state: str = "pending", offset: int = Query(default=0, ge=0, le=100000)):
    if not enabled():
        return {"enabled": False, "rows": []}
    try:
        return {"enabled": True, "rows": Repository().list(actor_id, state=state, offset=offset)}
    except Forbidden as exc:
        raise HTTPException(403, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get('/api/email-review/options')
def options(actor_id: str, id: str, kind: Literal["company", "project"], query: str = Query(max_length=80)):
    if not enabled():
        raise HTTPException(503, "メール照合は準備中です")
    repo = Repository()
    try:
        repo.get(id, actor_id)
        rows = repo.masters(query=query) if kind == "company" else [
            {"id": item["id"], "name": item.get("案件名") or "名称なし"} for item in search_projects(query)]
        return {"rows": rows}
    except Forbidden as exc:
        raise HTTPException(403, str(exc)) from None


@router.post('/api/email-review/decision')
def decision(body: Decision):
    if not enabled():
        raise HTTPException(503, "メール照合は準備中です")
    try:
        selection = {key: str(getattr(body, key)) if getattr(body, key) else None
                     for key in ("company_id", "contact_id", "target_id", "project_id")}
        return Service().decision(body.id, body.actor_id, body.revision, body.action, selection)
    except Forbidden as exc:
        raise HTTPException(403, str(exc)) from None
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
