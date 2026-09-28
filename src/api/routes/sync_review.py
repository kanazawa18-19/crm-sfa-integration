"""管理画面から認証されたマネージャーの個別判断を受け付ける。"""
from typing import Literal
import os
import hmac
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from src.api.dependencies import wiring_dependency
from src.sync_review.domain import ReviewForbidden, ReviewConflict
from src.sync_review.journal import FieldReviewJournal
from src.sync_review.writer import ApprovedFieldWriter
from src.sync_engine.record_sync_lock import RecordSyncBusy, acquire_record_sync_lock

router = APIRouter()


class DecisionRequest(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1, max_length=100)
    action: Literal['confirm', 'approve', 'restore', 'keep_blank', 'resume', 'recheck']
    revision: int = Field(ge=0)
    restore_from: str | None = None


def verify_review_token(authorization: str | None = Header(default=None)):
    expected = os.environ.get('DASHBOARD_API_TOKEN')
    if not expected or authorization is None or not hmac.compare_digest(authorization, 'Bearer ' + expected):
        raise HTTPException(401, 'unauthorized')


@router.post('/api/sync-review/decision', dependencies=[Depends(verify_review_token)])
def decision(body: DecisionRequest, wiring=Depends(wiring_dependency)):
    journal = FieldReviewJournal()
    dispatcher = wiring.dispatcher._dispatcher
    try:
        row = journal.get(body.id)
        if row is None:
            raise HTTPException(404, '対象がありません')
        with acquire_record_sync_lock(dispatcher._store, row['dbKey'], row['notionKey']):
            new_snapshot = None
            if body.action == 'recheck':
                from src.db_schema.registry import get_schema
                mapping = dispatcher._store.get(row['notionKey'])
                if mapping is None or mapping.db_key != row['dbKey']:
                    raise ReviewConflict('対象の対応を読み直してください')
                new_snapshot = dispatcher._field_review.snapshot(mapping,
                    get_schema(row['dbKey']).get_property(row['propertyName']))
            row = journal.decision(body.id, actor_id=body.actor_id, action=body.action,
                                   revision=body.revision, restore_from=body.restore_from,
                                   new_snapshot=new_snapshot)
        if row['state'] in {'approved', 'restore_requested'}:
            row = dispatcher._field_review.execute(body.id, store=dispatcher._store,
                                                    writer=ApprovedFieldWriter(dispatcher))
        return {'id': row['id'], 'state': row['state'], 'revision': row['revision']}
    except ReviewForbidden as exc:
        raise HTTPException(403, str(exc)) from None
    except (ReviewConflict, RecordSyncBusy) as exc:
        raise HTTPException(409, str(exc)) from None
